#!/usr/bin/env python3
"""claude/term-guard.sh のテスト。

フックの契約 (stdin の JSON → deny の JSON、あるいは無出力) をサブプロセス経由で
検証する。ルールは TERM_GUARD_RULES_DIR で一時ディレクトリに差し替え、push 系は
一時 git リポジトリを作って照合する。gh は実行しない。

    python3 claude/tests/term_guard_test.py
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

SCRIPT = Path(__file__).resolve().parent.parent / "term-guard.sh"

RULES = """\
# テスト用の守り: 新世界リポジトリの公開面に oldname を出さない
repo neworld-org/*
path {path}
term oldname
term old-secret-[0-9]+
"""


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.rules_dir = self.root / "term-guard.d"
        self.rules_dir.mkdir()
        self.newrepo = self.root / "newrepo"
        self.newrepo.mkdir()
        (self.rules_dir / "test.rules").write_text(
            RULES.format(path=self.newrepo)
        )

    def run_hook(self, command, cwd=None, rules_dir=None):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd if cwd is not None else self.root),
        }
        env = clean_env(
            TERM_GUARD_RULES_DIR=str(
                rules_dir if rules_dir is not None else self.rules_dir
            )
        )
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def assert_allowed(self, command, **kwargs):
        result = self.run_hook(command, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", f"素通しのはずが止めた: {command}")

    def assert_denied(self, command, **kwargs):
        result = self.run_hook(command, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "", f"止めるはずが素通しした: {command}")
        payload = json.loads(result.stdout)
        decision = payload["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny", result.stdout)
        return payload["hookSpecificOutput"]["permissionDecisionReason"]

    # --- git リポジトリの道具 ----------------------------------------------

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", *args],
            cwd=cwd or self.newrepo,
            check=True,
            capture_output=True,
            env=clean_env(
                GIT_AUTHOR_NAME="t",
                GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@example.com",
            ),
        )

    def make_repo(self, cwd=None):
        self.git("init", "-q", "-b", "main", cwd=cwd)

    def commit_file(self, name, content, message="chore: add file", cwd=None):
        base = cwd or self.newrepo
        (base / name).write_text(content)
        self.git("add", name, cwd=cwd)
        self.git("commit", "-q", "-m", message, cwd=cwd)


class GhTestCase(HookTestCase):
    """gh 投稿コマンドの照合。"""

    def test_title_with_term_is_denied(self):
        self.assert_denied(
            'gh issue create -R neworld-org/app --title "oldname の残骸" --body "x"'
        )

    def test_clean_post_is_allowed(self):
        self.assert_allowed(
            'gh issue create -R neworld-org/app --title "きれいな題" --body "きれいな本文"'
        )

    def test_match_is_case_insensitive(self):
        self.assert_denied('gh pr comment 1 -R neworld-org/app --body "see OldName"')

    def test_other_repo_is_allowed(self):
        # 宛先が守りの対象でなければ、語が入っていても発火しない
        self.assert_allowed(
            'gh issue create -R somewhere/else --title "oldname の話" --body "x"'
        )

    def test_body_file_content_is_denied(self):
        body = self.root / "body.md"
        body.write_text("## 経緯\n\noldname から持ってきた。\n")
        self.assert_denied(f'gh issue comment 1 -R neworld-org/app -F "{body}"')

    def test_body_file_path_is_not_matched(self):
        # パス表記は公開されない。ファイル名に語が入っていても中身がきれいなら通す
        body = self.root / "oldname-notes.md"
        body.write_text("きれいな本文。\n")
        self.assert_allowed(f'gh issue comment 1 -R neworld-org/app -F "{body}"')

    def test_heredoc_body_is_denied(self):
        self.assert_denied(
            'gh issue create -R neworld-org/app --title t --body "$(cat <<\'EOF\'\n'
            "oldname を参照\nEOF\n)\""
        )

    def test_second_term_pattern_works(self):
        self.assert_denied(
            'gh issue create -R neworld-org/app --title t --body "key: old-secret-42"'
        )

    def test_pr_review_with_body_is_denied(self):
        self.assert_denied('gh pr review 1 -R neworld-org/app --body "oldname では…"')

    def test_pr_edit_is_denied(self):
        self.assert_denied('gh pr edit 1 -R neworld-org/app --body "oldname"')

    def test_reading_commands_are_allowed(self):
        # 投稿ではない gh は語が入っていても対象外 (検索等で旧語を使うのは正当)
        self.assert_allowed("gh issue list -R neworld-org/app --search oldname")
        self.assert_allowed("gh issue view 1 -R neworld-org/app")

    def test_origin_of_cwd_decides_when_no_repo_flag(self):
        self.make_repo()
        self.git("remote", "add", "origin", "git@github.com:neworld-org/app.git")
        self.assert_denied('gh issue comment 1 --body "oldname"', cwd=self.newrepo)

    def test_explicit_old_repo_from_new_cwd_is_allowed(self):
        # 新世界の作業ディレクトリから旧世界リポジトリ (記録の置き場) へ書くのは正当。
        # 宛先が判定できるときは cwd の path では発火しない
        self.assert_denied('gh issue comment 1 -R neworld-org/app --body "oldname"',
                           cwd=self.newrepo)
        self.assert_allowed('gh issue comment 1 -R somewhere/oldworld --body "oldname"',
                            cwd=self.newrepo)

    # --- 照合対象は「実際に外へ出るもの」だけ (issue #128) -----------------

    def test_scratchpad_path_in_chained_command_is_allowed(self):
        # 本文を書き出す先のパスに語が入っているだけでは止めない。公開されるのは
        # 本文で、リダイレクト先のパス表記ではない
        body = self.root / "oldname-dir" / "body.md"
        self.assert_allowed(
            f"cat > {body} <<'EOF'\n"
            "## 経緯\n\nきれいな本文だけが入っている。\n"
            "EOF\n"
            'gh issue create --repo neworld-org/app --title "きれいな題" '
            f"--body-file {body}"
        )

    def test_heredoc_body_is_denied_before_the_file_exists(self):
        # 同じ形で本文に語があるときは止める。この時点でファイルはまだ無く
        # (--body-file の中身は読めない)、heredoc 本文だけが手掛かり
        body = self.root / "clean-dir" / "body.md"
        self.assert_denied(
            f"cat > {body} <<'EOF'\n"
            "oldname から持ってきた記録。\n"
            "EOF\n"
            f"gh issue create --repo neworld-org/app --title t --body-file {body}"
        )

    def test_cd_argument_is_not_matched(self):
        # 連結された別コマンド (cd) の引数は公開面ではない
        self.assert_allowed(
            f"cd {self.root}/oldname-x && "
            'gh issue create -R neworld-org/app --title "きれいな題" --body "きれいな本文"'
        )

    def test_redirect_target_is_not_matched(self):
        self.assert_allowed(
            "gh issue create -R neworld-org/app --title t --body clean "
            f"> {self.root}/oldname-out.txt"
        )

    def test_pipe_inside_quoted_body_is_matched(self):
        # markdown テーブルの | でセグメントを切ると表の行が照合から落ちる (漏れ)
        self.assert_denied(
            'gh issue create -R neworld-org/app --title t --body "| 症状 | 状況 |\n'
            "| --- | --- |\n"
            '| oldname 由来 | 残 |"'
        )

    def test_and_inside_quoted_body_is_matched(self):
        self.assert_denied('gh issue comment 1 -R neworld-org/app --body "A && oldname"')

    def test_label_argument_is_matched(self):
        # 本文以外の引数も gh へ渡る = 公開面。照合対象から外さない
        self.assert_denied("gh issue edit 1 -R neworld-org/app --add-label oldname")

    def test_repo_quoted_in_body_is_not_the_target(self):
        # 本文に引用した --repo は宛先の材料にしない (宛先は gh のオプションと remote だけ)
        self.assert_allowed(
            'gh issue create --title t --body "$(cat <<\'EOF\'\n'
            "再現: gh issue create --repo neworld-org/app --body …\n"
            "oldname を含む本文\n"
            "EOF\n"
            ')" -R somewhere/else'
        )

    def test_here_string_body_is_matched(self):
        # ヒア文字列は stdin へ渡る本文。リダイレクト先のパスと違って公開される
        self.assert_denied(
            'gh issue comment 1 -R neworld-org/app --body-file - <<< "oldname を参照"'
        )

    def test_here_string_written_to_a_file_is_matched(self):
        # 本文を作る側のセグメントに現れる形でも、中身は本文なので照合する
        body = self.root / "clean-dir" / "body.md"
        self.assert_denied(
            f'cat <<< "oldname を参照" > {body} && '
            f"gh issue create -R neworld-org/app --title t --body-file {body}"
        )

    def test_trailing_comment_is_not_matched(self):
        # シェルの行末コメントは gh へ渡らない = 公開面ではない
        self.assert_allowed(
            "gh issue create -R neworld-org/app --title t --body clean  # oldname のメモ"
        )

    def test_hash_inside_a_word_is_matched(self):
        # 語の途中の # はコメントの開始ではない (シェルも本文の一部として渡す)
        self.assert_denied(
            "gh issue create -R neworld-org/app --title t --body ref#oldname"
        )

    def test_hit_location_names_heredoc(self):
        reason = self.assert_denied(
            'gh issue create -R neworld-org/app --title t --body "$(cat <<\'EOF\'\n'
            "oldname を参照\n"
            "EOF\n"
            ')"'
        )
        self.assertIn("heredoc", reason, "heredoc 本文で止めたことが示されない")


class PushTestCase(HookTestCase):
    """git push の照合 (push 範囲のコミット + 連結された commit の差分)。"""

    def test_unpushed_commit_content_is_denied(self):
        self.make_repo()
        self.commit_file("a.txt", "based on oldname\n")
        self.assert_denied("git push origin main", cwd=self.newrepo)

    def test_unpushed_commit_message_is_denied(self):
        self.make_repo()
        self.commit_file("a.txt", "clean\n", message="feat: port from oldname")
        self.assert_denied("git push origin main", cwd=self.newrepo)

    def test_clean_commits_are_allowed(self):
        self.make_repo()
        self.commit_file("a.txt", "clean\n")
        self.assert_allowed("git push origin main", cwd=self.newrepo)

    def test_remote_url_decides_target(self):
        # path 圏外でも、push 先リモートの slug が守りの対象なら発火する
        other = self.root / "elsewhere"
        other.mkdir()
        self.make_repo(cwd=other)
        self.git("remote", "add", "origin", "https://github.com/neworld-org/app.git",
                 cwd=other)
        self.commit_file("a.txt", "oldname\n", cwd=other)
        self.assert_denied("git push origin main", cwd=other)

    def test_other_remote_is_allowed(self):
        # push 先が守りの対象でなければ、語が入っていても発火しない
        other = self.root / "elsewhere2"
        other.mkdir()
        self.make_repo(cwd=other)
        self.git("remote", "add", "origin", "git@github.com:somewhere/else.git",
                 cwd=other)
        self.commit_file("a.txt", "oldname\n", cwd=other)
        self.assert_allowed("git push origin main", cwd=other)

    def test_chained_commit_sees_working_tree(self):
        self.make_repo()
        self.commit_file("a.txt", "clean\n")
        (self.newrepo / "a.txt").write_text("now with oldname\n")
        self.assert_denied('git add -A && git commit -m "chore: update" && git push',
                           cwd=self.newrepo)

    def test_plain_push_ignores_dirty_working_tree(self):
        # commit を伴わない push は、push 範囲に無い作業中ファイルでは止めない
        self.make_repo()
        self.commit_file("a.txt", "clean\n")
        (self.newrepo / "b.txt").write_text("draft: oldname\n")
        self.assert_allowed("git push origin main", cwd=self.newrepo)

    def test_reason_names_rule_and_pattern(self):
        self.make_repo()
        self.commit_file("a.txt", "oldname\n")
        reason = self.assert_denied("git push origin main", cwd=self.newrepo)
        self.assertIn("test.rules", reason, "どのルールに当たったかが示されない")
        self.assertIn("oldname", reason, "どのパターンに当たったかが示されない")


class ScopeTestCase(HookTestCase):
    """発火条件の外側 (ルール未設置・対象外コマンド) は無音。"""

    def test_no_rules_dir_is_silent(self):
        empty = self.root / "empty.d"
        self.assert_allowed(
            'gh issue create -R neworld-org/app --title "oldname" --body x',
            rules_dir=empty,
        )

    def test_empty_rules_dir_is_silent(self):
        empty = self.root / "empty2.d"
        empty.mkdir()
        self.assert_allowed(
            'gh issue create -R neworld-org/app --title "oldname" --body x',
            rules_dir=empty,
        )

    def test_unrelated_commands_are_allowed(self):
        self.make_repo()
        self.commit_file("a.txt", "oldname\n")
        self.assert_allowed("git status", cwd=self.newrepo)
        self.assert_allowed("git log --oneline", cwd=self.newrepo)
        self.assert_allowed("ls -la")

    def test_empty_command_is_allowed(self):
        self.assert_allowed("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
