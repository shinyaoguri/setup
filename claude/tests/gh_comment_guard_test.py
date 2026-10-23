#!/usr/bin/env python3
"""claude/gh-comment-guard.sh のテスト。

フックの契約 (stdin の JSON → deny の JSON、あるいは無出力) をサブプロセス経由で
検証する。gh は実行しないので、判定に要るのはコマンド文字列と --body-file が指す
ファイルだけ。

    python3 claude/tests/gh_comment_guard_test.py
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

SCRIPT = Path(__file__).resolve().parent.parent / "gh-comment-guard.sh"
CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

SIGNATURE_HEAD = "<sub>🤖 Assisted by [Claude Code](https://claude.com/claude-code)"
SIGNATURE = f"{SIGNATURE_HEAD}</sub>"


def signed(body):
    return f"{body}\n\n---\n{SIGNATURE}\n"


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

    def body_file(self, text, name="body.md"):
        path = self.root / name
        path.write_text(text)
        return path

    def run_hook(self, command, env=None, transcript=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        if transcript is not None:
            payload["transcript_path"] = str(transcript)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(**(env or {})),
        )

    def assert_allowed(self, command):
        result = self.run_hook(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", f"素通しのはずが止めた: {command}")

    def assert_denied(self, command, transcript=None):
        result = self.run_hook(command, transcript=transcript)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "", f"止めるはずが素通しした: {command}")
        payload = json.loads(result.stdout)
        decision = payload["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny", result.stdout)
        return payload["hookSpecificOutput"]["permissionDecisionReason"]

    # --- 署名が無いコメントは止める ---------------------------------------

    def test_body_without_signature_is_denied(self):
        self.assert_denied('gh issue comment 606 --body "検証しました"')

    def test_pr_comment_without_signature_is_denied(self):
        self.assert_denied('gh pr comment 690 --body "LGTM"')

    def test_body_file_without_signature_is_denied(self):
        path = self.body_file("## 所見\n\n本文です。\n")
        self.assert_denied(f'gh issue comment 606 -F "{path}"')

    def test_heredoc_without_signature_is_denied(self):
        self.assert_denied("gh issue comment 606 --body \"$(cat <<'EOF'\n本文\nEOF\n)\"")

    def test_global_flag_before_subcommand_is_denied(self):
        # gh -R owner/repo issue comment … のようにサブコマンドの手前に何か挟まる形
        self.assert_denied('gh -R shinyaoguri/metaphor issue comment 606 --body "x"')

    def test_pr_review_with_body_is_denied(self):
        self.assert_denied('gh pr review 690 --body "ここが気になります"')

    def test_reason_carries_the_signature_to_paste(self):
        reason = self.assert_denied('gh issue comment 606 --body "x"')
        self.assertIn(SIGNATURE, reason, "そのまま貼れる署名が理由文に無い")

    # --- 新しく作る本文 (#317) ---------------------------------------------
    # 以前は本文の作成を見ておらず、本文の署名は覚えていれば付くだけだった

    def test_creating_without_signature_is_denied(self):
        for command in (
            'gh issue create --title t --body "本文"',
            'gh pr create --title t --body "本文"',
            'gh pr revert 3 --body "戻す理由"',
            "gh -R shinyaoguri/metaphor issue create -t t -b x",
        ):
            with self.subTest(command=command):
                self.assert_denied(command)

    def test_creating_with_signature_is_allowed(self):
        path = self.body_file(signed("本文"))
        for command in (
            f'gh issue create --title t --body "{signed("本文")}"',
            f"gh pr create --title t --body-file {path}",
            f'gh pr revert 3 --body "{signed("戻す理由")}"',
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_model_named_signature_is_allowed(self):
        body = f"本文\n\n---\n{SIGNATURE_HEAD} (Opus 5.5)</sub>"
        self.assert_allowed(f'gh issue create --title t --body "{body}"')

    def test_creating_without_a_body_option_is_denied(self):
        # --fill もテンプレートも本文を作る。署名が無ければ止める
        self.assert_denied("gh pr create --fill")
        self.assert_denied("gh pr create --title t --template bug.md")

    def test_other_body_options_are_allowed(self):
        # 既存の本文の書き換えとマージコミットの本文は対象にしない
        self.assert_allowed('gh issue edit 3 --body "直した本文"')
        self.assert_allowed('gh pr merge 3 --squash --body "マージ本文"')
        self.assert_allowed("gh pr create --help")

    # --- 差し戻しの 1 行のモデルの名乗り (#317) ----------------------------

    def transcript(self, models, subagents=()):
        """セッションの記録を模して置く。ツールの結果に映った model の字面も 1 本混ぜる。"""
        path = self.root / "projects" / "-repo" / "sess.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "user", "message": {"content": '{"model":"gpt-fake"}'}})]
        lines += [json.dumps({"type": "assistant", "message": {"model": m}}) for m in models]
        path.write_text("\n".join(lines) + "\n")
        for i, sub in enumerate(subagents):
            agent = path.with_suffix("") / "subagents" / f"agent-{i}.jsonl"
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text(
                "\n".join(json.dumps({"type": "assistant", "message": {"model": m}}) for m in sub) + "\n"
            )
        return path

    def test_reason_names_the_models_of_the_session(self):
        transcript = self.transcript(
            ["claude-sonnet-5-5-20261001"] + ["claude-opus-5-5"] * 2 + ["<synthetic>"],
            subagents=[["claude-haiku-5-5"], ["claude-opus-5-5"]],
        )
        reason = self.assert_denied('gh issue create -t t -b x', transcript=transcript)
        # 主が最後に使ったモデルが先頭、Claude は繰り返さない、日付の接尾辞は落とす
        self.assertIn(f"{SIGNATURE_HEAD} (Opus 5.5, Sonnet 5.5, Haiku 5.5)</sub>", reason)
        self.assertNotIn("synthetic", reason)
        self.assertNotIn("gpt-fake", reason, "ツールの結果に映った model を拾っている")
        self.assertNotIn("Claude Opus", reason, "括弧の中で Claude を繰り返している")

    def test_latest_main_model_comes_first(self):
        transcript = self.transcript(["claude-haiku-5-5"] * 5 + ["claude-opus-5-5"])
        reason = self.assert_denied('gh pr comment 1 -b x', transcript=transcript)
        self.assertIn("(Opus 5.5, Haiku 5.5)</sub>", reason)

    def test_unknown_model_id_is_kept_as_is(self):
        transcript = self.transcript(["claude-3-5-sonnet-20241022"])
        reason = self.assert_denied('gh pr comment 1 -b x', transcript=transcript)
        self.assertIn("(claude-3-5-sonnet-20241022)</sub>", reason)

    def test_without_a_transcript_names_no_model(self):
        reason = self.assert_denied('gh pr comment 1 -b x', transcript=self.root / "none.jsonl")
        self.assertIn(SIGNATURE, reason)

    # --- 署名があれば素通し -----------------------------------------------

    def test_signed_body_is_allowed(self):
        self.assert_allowed(f'gh issue comment 606 --body "{signed("検証しました")}"')

    def test_signed_body_file_is_allowed(self):
        path = self.body_file(signed("## 所見\n\n本文です。"))
        self.assert_allowed(f'gh issue comment 606 -F "{path}"')

    def test_signed_body_file_long_flag_is_allowed(self):
        path = self.body_file(signed("本文"))
        self.assert_allowed(f"gh pr comment 690 --body-file {path}")

    # --- 投稿ではないものは素通し -----------------------------------------

    def test_reading_comments_is_allowed(self):
        self.assert_allowed("gh issue view 606 --comments")

    def test_listing_is_allowed(self):
        self.assert_allowed("gh issue list --state open")

    def test_pr_diff_is_allowed(self):
        self.assert_allowed("gh pr diff 690")

    def test_help_is_allowed(self):
        self.assert_allowed("gh issue comment --help")

    def test_pr_review_without_body_is_allowed(self):
        # 本文の無い承認には発言が無いので署名の出番も無い
        self.assert_allowed("gh pr review 690 --approve")

    def test_unrelated_command_is_allowed(self):
        self.assert_allowed("git status")

    def test_empty_command_is_allowed(self):
        self.assert_allowed("")

    # --- 判断材料が無いときは落ちない -------------------------------------

    def test_missing_body_file_is_denied_not_crashed(self):
        # 読めないパスは判断材料が無いだけ。署名も見つからないので deny 側に倒れる
        self.assert_denied(f'gh issue comment 606 -F "{self.root}/does-not-exist.md"')

    def test_missing_body_file_with_inline_signature_is_allowed(self):
        command = (
            f'gh issue comment 606 -F "{self.root}/does-not-exist.md" '
            f'# {SIGNATURE}'
        )
        self.assert_allowed(command)

    # --- 無効化スイッチ ----------------------------------------------------

    def test_disabled_by_env_lets_unsigned_body_through(self):
        # 署名を自前の機構で担保しているリポジトリが、二重に動くこちらを黙らせる口。
        # 向きは「リポ側が担保して個人側を黙らせる」で固定してある
        # (前例は CLAUDE_PLAN_RECORD=0)
        result = self.run_hook(
            'gh issue comment 606 --body "署名なし"',
            env={"CLAUDE_GH_COMMENT_GUARD": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "無効化したのに止めた")

    def test_other_values_keep_the_guard_on(self):
        # 既定は有効。0 以外は「無効化の意思表示ではない」と読む
        for value in ("1", "", "false", "no"):
            with self.subTest(value=value):
                result = self.run_hook(
                    'gh issue comment 606 --body "署名なし"',
                    env={"CLAUDE_GH_COMMENT_GUARD": value},
                )
                self.assertNotEqual(
                    result.stdout.strip(), "", f"{value!r} で黙ってしまった"
                )

    # --- 規約との一致 ------------------------------------------------------

    def test_signature_matches_claude_md(self):
        # 署名の正本は CLAUDE.md。フックの文字列とずれると、規約どおり書いたのに
        # 差し戻されるという最悪の噛み合わせになる。後ろのモデルの名乗りは
        # セッションごとに違うので、頭だけを突き合わせる (#317)
        self.assertIn(SIGNATURE_HEAD, CLAUDE_MD.read_text(), "CLAUDE.md の署名とずれている")
        self.assertIn(SIGNATURE_HEAD, SCRIPT.read_text(), "フックの署名とずれている")


if __name__ == "__main__":
    unittest.main()
