#!/usr/bin/env python3
"""claude/plan-record.sh のテスト。

フックの契約 (stdin の JSON → 記録ファイル + stderr の指示) をサブプロセス経由で
検証する。投稿先の解決と投稿済み判定は gh に依存するので、PATH の先頭に偽の gh を
置いて振る舞いを環境変数で決める。

    python3 claude/tests/plan_record_test.py
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "plan-record.sh"

# 引数から用件だけを見分ける最小の gh。番号を返す問い合わせと、コメント本文を返す
# 問い合わせの二つしか使われない。前者は state による絞り込みまで見るので、末尾の
# -q に来る jq クエリを本物と同じように適用する
FAKE_GH = """#!/bin/sh
kind=$1
query=.
prev=
for arg in "$@"; do
  [ "$prev" = "-q" ] && query=$arg
  prev=$arg
done
case "$*" in
  *comments*) printf '%s\\n' "${FAKE_GH_COMMENTS:-}"; exit 0 ;;
esac
json=
case "$kind" in
  # FAKE_GH_PR は「open な PR がその番号」の近道。state を変えたいときは
  # FAKE_GH_PR_JSON へ {"number":n,"state":"MERGED"} のように直接置く
  pr)
    json=${FAKE_GH_PR_JSON:-}
    [ -n "$json" ] || [ -z "${FAKE_GH_PR:-}" ] || json='{"number":'$FAKE_GH_PR',"state":"OPEN"}'
    ;;
  issue)
    json=${FAKE_GH_ISSUE_JSON:-}
    [ -n "$json" ] || [ -z "${FAKE_GH_ISSUE:-}" ] || json='{"number":'$FAKE_GH_ISSUE'}'
    ;;
esac
[ -n "$json" ] || exit 1
printf '%s' "$json" | jq -r "$query"
"""

MERGED_PR = '{"number":108,"state":"MERGED"}'
CLOSED_PR = '{"number":108,"state":"CLOSED"}'


class PlanRecordTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        root = Path(self.workdir.name)

        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "feat/plan-123")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        (self.repo / "README.md").write_text("hi\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

        bindir = root / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.bindir = bindir

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def env(self, **overrides):
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        for key, value in overrides.items():
            env[key] = value
        return env

    def run_hook(self, mode, payload=None, stdin="", **env):
        return subprocess.run(
            [str(SCRIPT), mode],
            input=json.dumps(payload) if payload is not None else stdin,
            capture_output=True,
            text=True,
            cwd=str(self.repo),
            env=self.env(**env),
            timeout=30,
        )

    def capture(self, plan, **env):
        return self.run_hook(
            "capture",
            {
                "tool_name": "ExitPlanMode",
                "cwd": str(self.repo),
                "session_id": "abcd1234-ef56-7890",
                "tool_input": {"plan": plan},
            },
            **env,
        )

    def capture_payload(self, **overrides):
        """tool_input / tool_response を差し替えて capture を叩く。

        プラン本文の置き場は Claude Code のバージョンで動くので、素の payload を
        組めるようにしておく (self.capture は旧来の tool_input.plan 形式の近道)。
        """
        env = {k: v for k, v in overrides.items() if k.isupper()}
        payload = {
            "tool_name": "ExitPlanMode",
            "cwd": str(self.repo),
            "session_id": "abcd1234-ef56-7890",
            **{k: v for k, v in overrides.items() if not k.isupper()},
        }
        return self.run_hook("capture", payload, **env)

    def guard(self, **env):
        return self.run_hook("guard", {"cwd": str(self.repo)}, **env)

    def records(self):
        return sorted((self.repo / ".git" / "claude-plan-records").glob("*.md"))

    # --- サニタイズ ---------------------------------------------------------

    def test_sanitize_collapses_repo_and_home_paths(self):
        body = (
            "触るのは /Users/so/Repos/proj/.claude/worktrees/wt/Sources/App.swift。\n"
            "他の人の /home/alice/notes.md も引用した。\n"
        )
        out = subprocess.run(
            [str(SCRIPT), "sanitize", "/Users/so/Repos/proj/.claude/worktrees/wt"],
            input=body, capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("触るのは Sources/App.swift", out)
        self.assertIn("~/notes.md", out)
        self.assertNotIn("/Users/so", out)
        self.assertNotIn("alice", out)

    def test_sanitize_survives_regex_metacharacters_in_path(self):
        # worktree のディレクトリ名に括弧やドットが入っても sed が壊れないこと
        root = "/Users/so/Repos/proj (old)/.claude/wt+1"
        out = subprocess.run(
            [str(SCRIPT), "sanitize", root],
            input=f"{root}/Sources/App.swift を直す\n",
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Sources/App.swift を直す", out.stdout)

    # --- 検査 ---------------------------------------------------------------

    def test_scan_flags_secrets_and_leaves_prose_alone(self):
        body = (
            "export GITHUB_TOKEN=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n"
            "token は環境変数で渡す (値は書かない)\n"
            "参照は $API_KEY と <your-token> のまま\n"
        )
        out = subprocess.run(
            [str(SCRIPT), "scan"], input=body, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("BLOCK", out)
        self.assertIn("行 1", out)
        # 2〜3 行目は説明文と伏せ字なので、秘密情報として拾ってはいけない
        self.assertNotIn("行 2", out)
        self.assertNotIn("行 3", out)

    def test_scan_warns_without_blocking(self):
        body = "連絡は alice@example.com。参照は op://Vault/item/credential\n"
        out = subprocess.run(
            [str(SCRIPT), "scan"], input=body, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("WARN", out)
        self.assertNotIn("BLOCK", out)

    def test_scan_ignores_github_noreply(self):
        out = subprocess.run(
            [str(SCRIPT), "scan"],
            input="Assisted-by: Claude <noreply@anthropic.com>\n",
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(out.strip(), "")

    # --- capture ------------------------------------------------------------

    def test_capture_writes_sanitized_record_and_instructs(self):
        result = self.capture(
            f"## 方針\n\n{self.repo}/README.md を直す。却下案: 全面書き換え。\n",
            FAKE_GH_PR="42",
        )
        self.assertEqual(result.returncode, 2, result.stderr)

        files = self.records()
        self.assertEqual(len(files), 1)
        body = files[0].read_text()
        self.assertIn("却下案: 全面書き換え", body)
        self.assertIn("README.md を直す", body)
        self.assertNotIn(str(self.repo), body)  # 絶対パスは畳まれている
        self.assertIn("<!-- plan-record: ", body)  # 投稿済み判定の目印

        self.assertIn("gh pr comment 42", result.stderr)

    def test_capture_falls_back_to_issue_named_by_the_plan(self):
        result = self.capture("Closes #77 のための計画。\n", FAKE_GH_ISSUE="77")
        self.assertIn("gh issue comment 77", result.stderr)

    def test_capture_refuses_when_the_plan_carries_a_secret(self):
        result = self.capture(
            "手順:\n1. export GITHUB_TOKEN=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n",
            FAKE_GH_PR="42",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.records(), [])  # 秘密を .git の中に置き去りにしない
        self.assertIn("秘密情報", result.stderr)
        # 検出した値そのものを再掲しない (hook の出力も記録に残るため)
        self.assertNotIn("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ", result.stderr)

    def test_capture_still_records_when_no_target_exists_yet(self):
        result = self.capture("PR も Issue もまだ無い状態の計画。\n")
        self.assertEqual(len(self.records()), 1)
        self.assertIn("まだありません", result.stderr)

    def test_capture_reports_warnings_for_human_judgement(self):
        result = self.capture("連絡先 alice@example.com を使う。\n", FAKE_GH_PR="42")
        self.assertIn("メールアドレス", result.stderr)
        self.assertEqual(len(self.records()), 1)  # 警告では止めない

    def test_capture_outside_git_is_silent(self):
        outside = Path(self.workdir.name) / "plain"
        outside.mkdir()
        result = subprocess.run(
            [str(SCRIPT), "capture"],
            input=json.dumps(
                {"cwd": str(outside), "session_id": "x", "tool_input": {"plan": "計画"}}
            ),
            capture_output=True, text=True, cwd=str(outside), env=self.env(), timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    # --- プラン本文の取り出し (置き場は Claude Code のバージョンで動く) --------

    def test_capture_reads_the_plan_from_the_tool_response(self):
        # 現行の ExitPlanMode はモデルが引数を取らず、本文は tool_response に返る。
        # tool_input だけを見ていたころは、ここで無言のまま記録が作られなかった
        result = self.capture_payload(
            tool_input={"_targetMode": "auto"},
            tool_response={"plan": "## 方針\n\n却下案: 全面書き換え。\n", "isAgent": False},
            FAKE_GH_PR="42",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertIn("却下案: 全面書き換え", self.records()[0].read_text())

    def test_capture_reads_the_plan_from_the_file_it_was_written_to(self):
        plan_file = Path(self.workdir.name) / "plan.md"
        plan_file.write_text("## 方針\n\nファイル越しに渡されたプラン。\n")
        result = self.capture_payload(
            tool_input={"_targetMode": "auto"},
            tool_response={"filePath": str(plan_file)},
            FAKE_GH_PR="42",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("ファイル越しに渡されたプラン", self.records()[0].read_text())

    def test_capture_speaks_up_when_the_plan_cannot_be_found(self):
        # ExitPlanMode が通った以上プランは必ずある。取り出せないなら異常なので、
        # 黙って諦めると guard も黙り、仕組みごと無言で無効化される (Issue #87)
        result = self.capture_payload(
            tool_input={"_targetMode": "auto"}, tool_response={}, FAKE_GH_PR="42"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.records(), [])
        self.assertIn("取り出せませんでした", result.stderr)
        # 次に直す人のために、受け取ったキーだけを見せる (値は出さない)
        self.assertIn("_targetMode", result.stderr)

    # --- 何もせず終わった理由の可視化 -----------------------------------------

    def test_debug_explains_why_nothing_happened(self):
        outside = Path(self.workdir.name) / "plain"
        outside.mkdir()
        result = subprocess.run(
            [str(SCRIPT), "capture"],
            input=json.dumps(
                {"cwd": str(outside), "session_id": "x", "tool_input": {"plan": "計画"}}
            ),
            capture_output=True, text=True, cwd=str(outside),
            env=self.env(CLAUDE_PLAN_RECORD_DEBUG="1"), timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("plan-record:", result.stderr)
        self.assertIn("git リポジトリの外", result.stderr)

    def test_debug_is_off_by_default(self):
        # 通常運転では黙っていること (全リポで動くフックなので喋ると邪魔になる)
        result = self.capture_payload(
            tool_input={"plan": "計画。\n"}, CLAUDE_PLAN_RECORD="0"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_debug_explains_being_disabled(self):
        result = self.capture_payload(
            tool_input={"plan": "計画。\n"},
            CLAUDE_PLAN_RECORD="0", CLAUDE_PLAN_RECORD_DEBUG="1",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("無効化されている", result.stderr)

    # --- 投稿先の解決 -------------------------------------------------------

    def test_capture_posts_to_a_merged_pull_request(self):
        # マージすると PR は MERGED になる。ここで見失うと、規約どおり PR へ
        # 投稿を済ませたセッションほど閉じた Issue へ催促される
        result = self.capture(
            "Closes #77 のための計画。\n", FAKE_GH_PR_JSON=MERGED_PR, FAKE_GH_ISSUE="77"
        )
        self.assertIn("gh pr comment 108", result.stderr)
        self.assertNotIn("issue comment", result.stderr)

    def test_capture_skips_an_abandoned_pull_request(self):
        # 放棄された PR (CLOSED) は器として死んでいるので、名乗った Issue へ回す
        result = self.capture(
            "Closes #77 のための計画。\n", FAKE_GH_PR_JSON=CLOSED_PR, FAKE_GH_ISSUE="77"
        )
        self.assertIn("gh issue comment 77", result.stderr)

    def test_guard_clears_a_record_posted_to_a_merged_pull_request(self):
        self.capture("計画。\n", FAKE_GH_PR_JSON=MERGED_PR)
        record_id = self.records()[0].read_text().split("plan-record: ")[1].split(" ")[0]

        result = self.guard(
            FAKE_GH_PR_JSON=MERGED_PR,
            FAKE_GH_COMMENTS=f"<!-- plan-record: {record_id} -->",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])

    def test_capture_ignores_a_hex_suffix_in_the_branch_name(self):
        # claude/<説明>-<6 桁 hex>。936 は無関係な Issue として実在しうるので、
        # 実在確認を通り抜けてプランが撃ち込まれる
        self.git("checkout", "-q", "-b", "claude/batch-issue-cleanup-c936e5")
        result = self.capture("名乗りの無い計画。\n", FAKE_GH_ISSUE="936")
        self.assertIn("まだありません", result.stderr)
        self.assertNotIn("936", result.stderr)

    def test_capture_ignores_digits_glued_to_letters_in_the_branch_name(self):
        # worktree の自動生成名。0127 は英字に挟まれたハッシュの断片
        self.git("checkout", "-q", "-b", "worktree-bridge-cse_0127aTN6krq7fqrr56rh6gbc")
        result = self.capture("名乗りの無い計画。\n", FAKE_GH_ISSUE="127")
        self.assertIn("まだありません", result.stderr)
        self.assertNotIn("127", result.stderr)

    def test_capture_still_reads_a_delimited_number_from_the_branch_name(self):
        # 区切りに接した数字は従来どおり拾う (推定の親切さを落とさない)
        for branch in ("issues-123", "fix/123-foo", "claude/fix-123-c936e5"):
            with self.subTest(branch=branch):
                self.git("checkout", "-q", "-b", branch)
                result = self.capture("名乗りの無い計画。\n", FAKE_GH_ISSUE="123")
                self.assertIn("gh issue comment 123", result.stderr)

    # --- guard --------------------------------------------------------------

    def test_guard_blocks_stop_while_the_plan_is_unposted(self):
        self.capture("計画。\n", FAKE_GH_PR="42")
        result = self.guard(FAKE_GH_PR="42")
        self.assertEqual(result.returncode, 2)
        self.assertIn("gh pr comment 42", result.stderr)

    def test_guard_clears_the_record_once_it_is_posted(self):
        self.capture("計画。\n", FAKE_GH_PR="42")
        record_id = self.records()[0].read_text().split("plan-record: ")[1].split(" ")[0]

        result = self.guard(
            FAKE_GH_PR="42", FAKE_GH_COMMENTS=f"<!-- plan-record: {record_id} -->"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])

    def test_guard_is_quiet_when_there_is_nowhere_to_post(self):
        self.capture("PR がまだ無い計画。\n")
        result = self.guard()  # PR も Issue も解決できない
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.records()), 1)  # 記録は残す (急かさないだけ)

    def test_guard_ignores_records_from_another_branch(self):
        self.capture("別ブランチで立てた計画。\n", FAKE_GH_PR="42")
        self.git("checkout", "-q", "-b", "other")
        result = self.guard(FAKE_GH_PR="42")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_gives_up_after_repeated_nags(self):
        self.capture("計画。\n", FAKE_GH_PR="42")
        for _ in range(3):
            self.assertEqual(self.guard(FAKE_GH_PR="42").returncode, 2)
        # 4 回目は諦めて人間の判断へ返す (無限に終われないセッションを作らない)
        self.assertEqual(self.guard(FAKE_GH_PR="42").returncode, 0)
        self.assertEqual(self.records(), [])

    def test_guard_shows_how_to_escape_a_wrong_target(self):
        # 投稿先の推定が外れたとき、差し戻しを読むだけで抜けられる必要がある
        self.capture("計画。\n", FAKE_GH_PR="42")
        result = self.guard(FAKE_GH_PR="42")
        self.assertEqual(result.returncode, 2)
        self.assertIn("投稿先が違う", result.stderr)

    def test_guard_can_be_disabled(self):
        self.capture("計画。\n", FAKE_GH_PR="42")
        result = self.guard(FAKE_GH_PR="42", CLAUDE_PLAN_RECORD="0")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
