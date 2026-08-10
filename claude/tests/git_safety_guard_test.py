#!/usr/bin/env python3
"""claude/git-safety-guard.sh のテスト。

フックは stdin の JSON を読んで走り切る作りなので、実際の契約
(stdin の JSON → ask / deny の JSON、あるいは無出力) をサブプロセス経由で検証する。
秘密ファイルの検査はステージの中身を見るため、テスト用のリポジトリを用意して実際に
git add したうえで走らせる。

    python3 claude/tests/git_safety_guard_test.py
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "git-safety-guard.sh"


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.workdir.name)
        self.addCleanup(self.workdir.cleanup)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def stage(self, *names):
        """ファイルを作ってステージする (.gitignore が漏れている状態の再現)。"""
        for name in names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("dummy\n")
        subprocess.run(
            ["git", "add", "-f", *names], cwd=self.repo, check=True
        )

    def run_hook(self, command):
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            cwd=self.repo,
            timeout=30,
        )

    def assert_allowed(self, result):
        """素通し (無出力)。判定は settings.json の permissions へ戻る。"""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "素通しのはずが出力があった")

    def assert_auto_approved(self, result):
        """allow で打ち切る判定。素通しと違い、確認プロンプトそのものが出ない。"""
        return self.assert_decision(result, "allow")

    def assert_decision(self, result, expected):
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], expected)
        return output["permissionDecisionReason"]


class DestructiveCommandTest(HookTestCase):
    """作業ツリーや履歴を捨てる操作は、実行前にユーザーへ返す。"""

    def test_reset_hard_asks(self):
        reason = self.assert_decision(self.run_hook("git reset --hard HEAD~1"), "ask")
        self.assertIn("復元できない", reason)

    def test_clean_force_asks(self):
        self.assert_decision(self.run_hook("git clean -fd"), "ask")

    def test_checkout_discard_asks(self):
        self.assert_decision(self.run_hook("git checkout -- src/main.py"), "ask")
        self.assert_decision(self.run_hook("git checkout ."), "ask")

    def test_restore_worktree_asks(self):
        self.assert_decision(self.run_hook("git restore src/main.py"), "ask")

    def test_restore_staged_only_passes(self):
        """ステージから外すだけなら作業ツリーは失われない。"""
        self.assert_allowed(self.run_hook("git restore --staged src/main.py"))

    def test_branch_force_delete_asks(self):
        self.assert_decision(self.run_hook("git branch -D feature/x"), "ask")

    def test_force_push_asks(self):
        self.assert_decision(self.run_hook("git push --force origin main"), "ask")
        self.assert_decision(self.run_hook("git push -f"), "ask")

    def test_stash_drop_asks(self):
        self.assert_decision(self.run_hook("git stash drop"), "ask")

    def test_command_after_another_is_caught(self):
        self.assert_decision(
            self.run_hook("git log -1 && git reset --hard HEAD~1"), "ask"
        )

    def test_git_with_global_option_is_caught(self):
        self.assert_decision(self.run_hook("git -C /tmp/repo reset --hard"), "ask")


class ReversibleOperationTest(HookTestCase):
    """その場で可逆と確認できた操作は、ユーザーを呼ばない。

    ブランチ掃除は allow で打ち切る (permissions には読み取り専用のコマンドしか
    載せられないので、素通しにすると結局そこで確認プロンプトが出る)。それ以外は
    従来どおり素通しで permissions の判断へ戻す。
    """

    GONE_ALIAS = (
        "!git fetch -pq && git for-each-ref "
        "--format='%(upstream:track) %(refname:short)' refs/heads "
        "| grep -F '[gone] ' | cut -d' ' -f2"
    )
    CLEAN_ALIAS = '!git gone | while read -r branch; do git branch -D "$branch"; done'

    def commit(self, message="c"):
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", message],
            cwd=self.repo, check=True,
        )

    def with_remote(self):
        """origin を持つ作業リポジトリにする (push 済みの main を作る)。

        origin は**作業リポジトリの外**に置く。中に作ると追跡外ディレクトリとして
        git status に現れ、「作業ツリーが clean か」の判定が狂う。
        """
        remote_dir = tempfile.TemporaryDirectory()
        self.addCleanup(remote_dir.cleanup)
        self.origin = Path(remote_dir.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(self.origin)], cwd=self.repo, check=True
        )
        self.commit("init")
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "HEAD:main"], cwd=self.repo, check=True
        )
        subprocess.run(
            ["git", "branch", "-q", "--set-upstream-to=origin/main"],
            cwd=self.repo, check=True,
        )

    def make_gone_branch(self, name):
        """push 済みのブランチを作り、リモート側を消して [gone] 状態にする。"""
        subprocess.run(["git", "checkout", "-q", "-b", name], cwd=self.repo, check=True)
        self.commit(f"on {name}")
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", name], cwd=self.repo, check=True
        )
        subprocess.run(["git", "checkout", "-q", "-"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-C", str(self.origin), "update-ref", "-d", f"refs/heads/{name}"],
            check=True,
        )
        subprocess.run(["git", "fetch", "-pq"], cwd=self.repo, check=True)

    def set_alias(self, name, definition):
        subprocess.run(
            ["git", "config", f"alias.{name}", definition], cwd=self.repo, check=True
        )

    # --- git branch -D ---------------------------------------------------

    def test_deleting_a_gone_branch_is_auto_approved(self):
        """upstream が消えたブランチ = squash merge 済み。内容は main にあり可逆。"""
        self.with_remote()
        self.make_gone_branch("feature/done")
        reason = self.assert_auto_approved(self.run_hook("git branch -D feature/done"))
        self.assertIn("確認は不要", reason)

    def test_safe_delete_is_auto_approved(self):
        """-d は git 自身がマージ済みかを確かめる。未マージなら git が断るので確認は要らない。"""
        self.with_remote()
        subprocess.run(["git", "branch", "feature/x"], cwd=self.repo, check=True)
        self.assert_auto_approved(self.run_hook("git branch -d feature/x"))

    def test_delete_with_another_command_is_not_auto_approved(self):
        """allow はコマンド全体に効くので、削除以外が混ざったら allow は返さない。

        素通しに落ちるだけで、判定は permissions へ戻る (危険側には倒れない)。
        """
        self.with_remote()
        self.make_gone_branch("feature/done")
        self.assert_allowed(
            self.run_hook("git branch -D feature/done && rm -rf build")
        )

    def test_deleting_a_live_branch_asks(self):
        """リモートが生きているブランチは、役目を終えたと言えない。"""
        self.with_remote()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/wip"], cwd=self.repo, check=True
        )
        self.commit("wip")
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "feature/wip"], cwd=self.repo, check=True
        )
        subprocess.run(["git", "checkout", "-q", "-"], cwd=self.repo, check=True)
        self.assert_decision(self.run_hook("git branch -D feature/wip"), "ask")

    def test_deleting_a_contained_local_only_branch_is_auto_approved(self):
        """push 前でも、tip が origin/main にあるならコミットは remote から取り戻せる。

        worktree セッションが残す push 前のブランチはこの形で溜まる (upstream を
        持たないので [gone] にはなりえない)。
        """
        self.with_remote()
        subprocess.run(["git", "branch", "feature/local"], cwd=self.repo, check=True)
        self.assert_auto_approved(self.run_hook("git branch -D feature/local"))

    def test_deleting_a_local_only_branch_with_own_commits_asks(self):
        """push もしておらず origin/main にも無いコミットは、消すと本当に失われる。"""
        self.with_remote()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/unpushed"], cwd=self.repo, check=True
        )
        self.commit("only here")
        subprocess.run(["git", "checkout", "-q", "-"], cwd=self.repo, check=True)
        self.assert_decision(self.run_hook("git branch -D feature/unpushed"), "ask")

    def test_mixed_targets_ask(self):
        """1 つでも確認できない対象が混ざれば、全体を ask にする。"""
        self.with_remote()
        self.make_gone_branch("feature/done")
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/unpushed"], cwd=self.repo, check=True
        )
        self.commit("only here")
        subprocess.run(["git", "checkout", "-q", "-"], cwd=self.repo, check=True)
        self.assert_decision(
            self.run_hook("git branch -D feature/done feature/unpushed"), "ask"
        )

    def test_live_branch_without_own_commits_asks(self):
        """push 済みのブランチは、内容が origin/main に入っていても ask のまま。

        「内容が既定ブランチにある」は push 前のブランチにしか当てない指標。
        push 済み = 共有済みで、まだ差分が無いだけの*作業中*のブランチと区別が
        つかないため、これを当てると生きた PR のブランチまで黙って消せてしまう。
        """
        self.with_remote()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/fresh"], cwd=self.repo, check=True
        )
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "feature/fresh"],
            cwd=self.repo, check=True,
        )
        subprocess.run(["git", "checkout", "-q", "-"], cwd=self.repo, check=True)
        self.assert_decision(self.run_hook("git branch -D feature/fresh"), "ask")

    def test_local_only_branch_without_remote_asks(self):
        """リモートを持たないリポジトリでは、取り戻せる先が無い。"""
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "init"],
            cwd=self.repo, check=True,
        )
        subprocess.run(["git", "branch", "feature/local"], cwd=self.repo, check=True)
        self.assert_decision(self.run_hook("git branch -D feature/local"), "ask")

    def test_unknown_branch_asks(self):
        self.with_remote()
        self.assert_decision(self.run_hook("git branch -D feature/nope"), "ask")

    def test_redirection_after_the_branch_name_is_not_a_target(self):
        """`git branch -D x 2>&1 | tail -1` の後続はブランチ名ではない。

        リダイレクトやパイプまで対象として読むと、実在しない名前の判定不能で
        ask に落ち、掃除のたびにユーザーを呼ぶことになる。

        削除以外が混ざる形なので allow ではなく素通し (permissions の判断へ戻る)。
        """
        self.with_remote()
        self.make_gone_branch("feature/done")
        self.assert_allowed(self.run_hook("git branch -D feature/done 2>&1 | tail -1"))

    def test_squash_merged_branch_asks_until_pruned(self):
        """squash merge 済みでも、リモート追跡が生きている間は ask のまま。

        「内容が main に入っているか」だけでは、まだ作業中のブランチ (main に無い
        変更をまだ持っていないだけ) と区別できない。掃除は `git fetch -p` を通して
        [gone] にしてから — それで確認なしに抜けられる。
        """
        self.with_remote()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/squashed"], cwd=self.repo, check=True
        )
        (self.repo / "f.txt").write_text("done\n")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True)
        self.commit("work")
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "feature/squashed"],
            cwd=self.repo, check=True,
        )
        # main へ squash 相当で取り込む (fetch -p はまだしない)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "merge", "-q", "--squash", "feature/squashed"], cwd=self.repo, check=True
        )
        self.commit("squashed work")
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.repo, check=True)
        self.assert_decision(self.run_hook("git branch -D feature/squashed"), "ask")

        # マージ後にリモート側が畳まれ、fetch -p が届けば確認は要らなくなる
        subprocess.run(
            ["git", "-C", str(self.origin), "update-ref", "-d", "refs/heads/feature/squashed"],
            check=True,
        )
        subprocess.run(["git", "fetch", "-pq"], cwd=self.repo, check=True)
        self.assert_auto_approved(self.run_hook("git branch -D feature/squashed"))

    def test_ask_reason_points_at_fetch_prune(self):
        """確認を求めるときは、確認なしで通す道 (fetch -p) を示す。"""
        self.with_remote()
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/wip"], cwd=self.repo, check=True
        )
        self.commit("wip")
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "feature/wip"], cwd=self.repo, check=True
        )
        subprocess.run(["git", "checkout", "-q", "-"], cwd=self.repo, check=True)
        reason = self.assert_decision(self.run_hook("git branch -D feature/wip"), "ask")
        self.assertIn("fetch -p", reason)

    # --- エイリアス経由 ---------------------------------------------------

    def test_gone_clean_alias_is_auto_approved(self):
        """gone なブランチだけを消すと定義から確認できるエイリアスは確認なしで通す。"""
        self.with_remote()
        self.set_alias("gone", self.GONE_ALIAS)
        self.set_alias("gone-clean", self.CLEAN_ALIAS)
        self.assert_auto_approved(self.run_hook("git gone-clean"))

    def test_tampered_alias_asks(self):
        """定義を書き換えて gone 以外も消せるようにしたら、確認は成立しない。"""
        self.with_remote()
        self.set_alias("gone", self.GONE_ALIAS)
        self.set_alias(
            "gone-clean", '!git branch | while read -r b; do git branch -D "$b"; done'
        )
        self.assert_decision(self.run_hook("git gone-clean"), "ask")

    def test_alias_hiding_a_destructive_command_asks(self):
        """エイリアスに包んでも検査を素通りできない (展開してから見る)。"""
        self.set_alias("nuke", "!git reset --hard HEAD~1")
        self.assert_decision(self.run_hook("git nuke"), "ask")

    def test_gone_alias_without_gone_check_asks(self):
        """`git gone` 自体が [gone] 判定でなければ、それに依存する掃除も信用できない。"""
        self.with_remote()
        self.set_alias("gone", "!git branch --format='%(refname:short)'")
        self.set_alias("gone-clean", self.CLEAN_ALIAS)
        self.assert_decision(self.run_hook("git gone-clean"), "ask")

    # --- git reset --hard -------------------------------------------------

    def test_reset_hard_on_clean_pushed_tree_passes(self):
        """捨てる変更が無く、HEAD がリモートにあるなら取り戻せる。"""
        self.with_remote()
        self.assert_allowed(self.run_hook("git reset --hard HEAD"))

    def test_reset_hard_with_local_changes_asks(self):
        """未コミットの変更があれば、それは他のどこにも無い。"""
        self.with_remote()
        (self.repo / "work.txt").write_text("in progress\n")
        self.assert_decision(self.run_hook("git reset --hard HEAD"), "ask")

    def test_reset_hard_on_unpushed_commit_asks(self):
        """リモートに無いコミットは reflog 頼みになる。"""
        self.with_remote()
        self.commit("not pushed yet")
        self.assert_decision(self.run_hook("git reset --hard HEAD~1"), "ask")


class SafeCommandTest(HookTestCase):
    """日常の操作は止めない。"""

    def test_ordinary_commands_pass(self):
        for command in (
            "ls -la",
            "git status --short",
            "git log --oneline -5",
            "git add -A",
            "git push origin main",
            "git reset HEAD~1",  # --hard でなければ作業ツリーは残る
            "git branch --list",
            "git stash",
        ):
            with self.subTest(command):
                self.assert_allowed(self.run_hook(command))


class SecretFileTest(HookTestCase):
    """秘密情報が入りうるファイルはコミットさせない。"""

    def test_staged_dotenv_is_denied(self):
        self.stage(".env")
        reason = self.assert_decision(self.run_hook('git commit -m "x"'), "deny")
        self.assertIn(".env", reason)
        self.assertIn(".gitignore", reason)

    def test_staged_private_key_is_denied(self):
        self.stage(".ssh/id_ed25519")
        self.assert_decision(self.run_hook('git commit -m "x"'), "deny")

    def test_staged_certificate_is_denied(self):
        self.stage("certs/server.pem")
        self.assert_decision(self.run_hook('git commit -m "x"'), "deny")

    def test_public_key_is_allowed(self):
        self.stage(".ssh/id_ed25519.pub")
        self.assert_allowed(self.run_hook('git commit -m "x"'))

    def test_dotenv_template_is_allowed(self):
        self.stage(".env.example")
        self.assert_allowed(self.run_hook('git commit -m "x"'))

    def test_ordinary_file_is_allowed(self):
        self.stage("src/main.py")
        self.assert_allowed(self.run_hook('git commit -m "x"'))

    def test_adding_a_secret_by_name_is_denied(self):
        """ステージ前でも、コマンドに書かれていれば止める。"""
        reason = self.assert_decision(self.run_hook("git add .env"), "deny")
        self.assertIn(".env", reason)

    def test_reason_offers_an_alternative(self):
        self.stage(".env")
        reason = self.assert_decision(self.run_hook('git commit -m "x"'), "deny")
        self.assertIn("ダミー値", reason)
        self.assertIn("restore --staged", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
