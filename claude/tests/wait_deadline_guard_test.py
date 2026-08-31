#!/usr/bin/env python3
"""claude/wait-deadline-guard.sh のテスト。

フックの契約 (stdin の JSON → deny の JSON、あるいは無出力) をサブプロセス経由で
検証する。判定材料はコマンド文字列だけなので、外のコマンドは一切実行しない。

    python3 claude/tests/wait_deadline_guard_test.py
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "wait-deadline-guard.sh"

# setup#136 で実際に 1 時間 7 分走り続けたコマンド (番号と JQ を短くしただけ)
INCIDENT = (
    'until [ "$(gh pr view 644 --json statusCheckRollup '
    "--jq '[.statusCheckRollup[]|select((.status//\"COMPLETED\")!=\"COMPLETED\")]|length')\" "
    '= "0" ]; do sleep 20; done; gh pr view 644 --json mergeStateStatus'
)


class HookTestCase(unittest.TestCase):
    def run_hook(self, command, env=None):
        # 無効化スイッチはテストが明示したときだけ効かせる。呼び出し元のセッションが
        # 立てている値をそのまま渡すと、フックを黙らせた環境で流したときに全件が
        # 素通しになり、判定を何も見ていない緑ができる
        base = {k: v for k, v in os.environ.items() if k != "CLAUDE_WAIT_DEADLINE_GUARD"}
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            timeout=30,
            env={**base, **(env or {})},
        )

    def assert_allowed(self, command):
        result = self.run_hook(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", f"素通しのはずが止めた: {command}")

    def assert_denied(self, command):
        result = self.run_hook(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "", f"止めるはずが素通しした: {command}")
        payload = json.loads(result.stdout)
        decision = payload["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny", result.stdout)
        return payload["hookSpecificOutput"]["permissionDecisionReason"]

    # --- 期限の無い待ちは止める -------------------------------------------

    def test_踏んだコマンドそのものを止める(self):
        self.assert_denied(INCIDENT)

    def test_until_と_sleep_の待ちを止める(self):
        self.assert_denied("until curl -sf localhost:3000; do sleep 2; done")

    def test_while_true_の待ちを止める(self):
        self.assert_denied("while true; do gh run list; sleep 30; done")

    def test_小数の_sleep_も見る(self):
        self.assert_denied('until grep -q "Ready in" dev.log; do sleep 0.5; done')

    def test_パイプの後ろに現れる待ちも見る(self):
        self.assert_denied("echo start | tee log && until test -f done; do sleep 5; done")

    def test_引用符の中の待ちも見る(self):
        # 差し戻しの文面が勧める `bash -c '…'` の形。ここが抜けると、勧めた形から
        # timeout を落としただけのコマンドが検査の外に出る
        self.assert_denied("bash -c 'until test -f done; do sleep 20; done'")

    def test_二重引用符の中の待ちも見る(self):
        self.assert_denied('bash -c "while true; do sleep 30; done"')

    # --- 期限があれば素通し -----------------------------------------------

    def test_timeout_で包んであれば素通し(self):
        self.assert_allowed("timeout 900 bash -c 'until test -f done; do sleep 20; done'")

    def test_gtimeout_でも素通し(self):
        # macOS に timeout(1) は無い。GNU coreutils を入れると gtimeout の名前で入る
        self.assert_allowed("gtimeout 900 bash -c 'until test -f done; do sleep 20; done'")

    def test_timeout_のオプション付きでも素通し(self):
        self.assert_allowed(
            "timeout --signal=KILL 60 bash -c 'while true; do sleep 1; done'"
        )

    def test_SECONDS_を条件に混ぜてあれば素通し(self):
        self.assert_allowed(
            'end=$((SECONDS + 900)); until test -f done || [ "$SECONDS" -ge "$end" ]; '
            "do sleep 20; done"
        )

    def test_算術評価の_SECONDS_でも素通し(self):
        self.assert_allowed("while ((SECONDS < 300)); do sleep 5; done")

    # --- ポーリングでないものは見ない --------------------------------------

    def test_ループの無いコマンドは素通し(self):
        self.assert_allowed("npm run dev")

    def test_sleep_だけなら素通し(self):
        self.assert_allowed("sleep 5; gh pr view 644")

    def test_回数の決まった繰り返しは素通し(self):
        # for は対象外。回数が書いてあるので必ず終わる
        self.assert_allowed("for i in {1..10}; do curl -s localhost; sleep 1; done")

    def test_sleep_を含まないループは素通し(self):
        self.assert_allowed("while IFS= read -r line; do echo \"$line\"; done < list.txt")

    def test_変数名に_until_が入るだけなら素通し(self):
        self.assert_allowed('until_at=$(date); echo "$until_at"; sleep 3')

    # --- 差し戻しの文面 ----------------------------------------------------

    def test_理由に打ち直せる形が両方載る(self):
        reason = self.assert_denied(INCIDENT)
        self.assertIn("timeout 900 bash -c", reason, "timeout の形が理由文に無い")
        self.assertIn("SECONDS + 900", reason, "SECONDS の形が理由文に無い")

    def test_理由が_SECONDS_の形を先に出す(self):
        # macOS に timeout(1) は無いので、先に出た形をそのまま打つと command not found
        # になる。推奨の順序そのものが、この環境で動くかどうかを決める
        reason = self.assert_denied(INCIDENT)
        self.assertLess(
            reason.index("SECONDS + 900"),
            reason.index("timeout 900"),
            "macOS で動かない timeout の形が先に出ている",
        )

    def test_理由が終端状態を全部見るよう促す(self):
        reason = self.assert_denied(INCIDENT)
        self.assertIn("終端状態", reason)

    def test_理由が実害の出どころを名乗る(self):
        reason = self.assert_denied(INCIDENT)
        self.assertIn("setup#136", reason)

    # --- 無効化スイッチ ----------------------------------------------------

    def test_スイッチで黙る(self):
        result = self.run_hook(INCIDENT, env={"CLAUDE_WAIT_DEADLINE_GUARD": "0"})
        self.assertEqual(result.stdout.strip(), "")

    def test_0_以外は無効化の意思表示ではない(self):
        result = self.run_hook(INCIDENT, env={"CLAUDE_WAIT_DEADLINE_GUARD": "1"})
        self.assertNotEqual(result.stdout.strip(), "")

    # --- 契約 --------------------------------------------------------------

    def test_コマンドが無い入力でも落ちない(self):
        result = subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
