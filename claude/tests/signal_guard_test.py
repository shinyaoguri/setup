#!/usr/bin/env python3
"""claude/signal-guard.py のテスト。

フックの契約 (stdin の JSON → deny / ask の JSON、あるいは無出力) をサブプロセス経由で
検証する。プロセス表は SIGNAL_GUARD_PS で差し替え、実際のプロセスには何も送らない。

    python3 claude/tests/signal_guard_test.py
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

SCRIPT = Path(__file__).resolve().parent.parent / "signal-guard.py"

APP = "/Users/u/Library/Application Support/Claude/claude-code/2.1.270/claude.app/Contents/MacOS/claude"
SNAPSHOT = "/bin/zsh -c source /Users/u/.claude/shell-snapshots/snapshot-zsh-1-abc.sh 2>/dev/null || true && eval '…'"

# setup#150 の実測を写した表 (PID はそのまま、パスだけ伏せた)
#   88906 = このセッションの claude / 5666 = 別セッションの claude
TABLE = [
    (48499, 1, "Claude", "/Applications/Claude.app/Contents/MacOS/Claude"),
    (88905, 48499, "disclaimer", "disclaimer"),
    (88906, 88905, APP, APP + " --output-format stream-json"),
    (63174, 88906, "/bin/zsh", SNAPSHOT),  # いま打っている Bash のシェル
    (63200, 63174, "python3", "python3 signal-guard.py"),  # このフック (テストでは自分と見なす)
    (13701, 88906, "/bin/zsh", SNAPSHOT),  # 背面実行のシェル
    (13728, 13701, "/opt/homebrew/Cellar/mokume/0.7.1/libexec/mokume", "mokume watch"),
    (24585, 13728, "/tmp/sketch/.build/debug/my-sketch", "/tmp/sketch/.build/debug/my-sketch"),
    (88920, 88906, "/opt/homebrew/Cellar/mokume/0.7.1/libexec/mokume", "mokume mcp"),  # 自分の MCP
    (13925, 88906, "/bin/sleep", "sleep 900"),  # 背面実行で exec した自分のプロセス (シェルの印が消える)
    (5665, 48499, "disclaimer", "disclaimer"),
    (5666, 5665, APP, APP + " --output-format stream-json"),
    (5682, 5666, "/opt/homebrew/Cellar/mokume/0.7.1/libexec/mokume", "mokume mcp"),  # 止めてしまった
    (30652, 1, "/tmp/fresh/.build/debug/fresh", "/tmp/fresh/.build/debug/fresh"),  # 孤児
]
SELF = 63200

# setup#150 で実際に打ったコマンド
INCIDENT = (
    "P=$(ps -axo pid,comm | awk '$2 ~ /\\/mokume$/ || $2==\"mokume\"{print $1}' | head -1); "
    "echo P=$P; kill -INT $P"
)


class SignalGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8")
        for pid, ppid, comm, args in TABLE:
            cls.fixture.write(f"{pid}\t{ppid}\tMon Sep 14 17:12:54 2026\t{comm}\t{args}\n")
        cls.fixture.close()

    @classmethod
    def tearDownClass(cls):
        Path(cls.fixture.name).unlink()

    def run_hook(self, command, **env):
        values = {"SIGNAL_GUARD_PS": self.fixture.name, "SIGNAL_GUARD_SELF": str(SELF)}
        values.update(env)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(**values),
        )

    def decision(self, command, **env):
        result = self.run_hook(command, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None, ""
        output = json.loads(result.stdout)["hookSpecificOutput"]
        return output["permissionDecision"], output["permissionDecisionReason"]

    def assert_allowed(self, command, **env):
        decision, reason = self.decision(command, **env)
        self.assertIsNone(decision, f"素通しのはずが {decision}: {command}\n{reason}")

    def assert_denied(self, command, **env):
        decision, reason = self.decision(command, **env)
        self.assertEqual(decision, "deny", f"止めるはずが {decision}: {command}")
        return reason

    def assert_asked(self, command, **env):
        decision, reason = self.decision(command, **env)
        self.assertEqual(decision, "ask", f"聞くはずが {decision}: {command}")
        return reason

    # --- 踏んだもの ---------------------------------------------------------

    def test_踏んだコマンドそのものを止める(self):
        self.assert_denied(INCIDENT)

    def test_別セッションの_MCP_サーバへ数字で送っても止める(self):
        reason = self.assert_denied("kill -INT 5682")
        self.assertIn("別の Claude Code セッション", reason)
        self.assertIn("5666", reason)

    def test_シグナル_0_でも別セッションなら止める(self):
        self.assertIn("別の Claude Code セッション", self.assert_denied("kill -0 5682"))

    # --- 自分が立てたものは通す --------------------------------------------

    def test_自分が背面で立てたプロセスは通す(self):
        self.assert_allowed("kill -INT 13728")

    def test_自分が立てたプロセスの子も通す(self):
        self.assert_allowed("kill -TERM 24585")

    def test_シグナルの指定の書き方によらず通す(self):
        self.assert_allowed("kill 13728")
        self.assert_allowed("kill -s TERM 13728")
        self.assert_allowed("kill -9 -- 13728")
        self.assert_allowed("/bin/kill -0 13728 2>/dev/null")

    def test_存在しない_PID_は通す(self):
        self.assert_allowed("kill 99999")

    def test_一覧は通す(self):
        self.assert_allowed("kill -l")

    # --- 自分のものでも止める・聞くもの ------------------------------------

    def test_自分の_MCP_サーバは聞く(self):
        self.assertIn("直接持つ子", self.assert_asked("kill 88920"))

    def test_exec_で置き換えた自分の背面プロセスは_MCP_サーバと区別できず聞く(self):
        # 実測: `exec sleep 900` を背面実行すると sleep が claude の直接の子になる
        self.assert_asked("kill 13925")

    def test_自分の_claude_とシェルは止める(self):
        self.assertIn("このセッション自身", self.assert_denied("kill 88906"))
        self.assertIn("このセッション自身", self.assert_denied("kill 63174"))

    # --- 持ち主が分からないもの --------------------------------------------

    def test_どのセッションにも属さない孤児は聞く(self):
        self.assert_asked("kill 30652")

    def test_フックがセッションの下に居なければ聞く(self):
        self.assert_asked("kill 13728", SIGNAL_GUARD_SELF="30652")

    def test_複数の送り先はいちばん強い判定になる(self):
        self.assertIn("別の Claude Code セッション", self.assert_denied("kill 13728 5682"))
        self.assert_asked("kill 13728 30652")

    # --- 送り先を静的に読めない形 ------------------------------------------

    def test_名前で送る道具は止める(self):
        self.assert_denied("pkill -f 'mokume watch'")
        self.assert_denied("killall mokume")

    def test_xargs_で送るのは止める(self):
        self.assert_denied("pgrep -f 'mokume watch' | xargs kill -INT")

    def test_数字でない送り先は止める(self):
        self.assert_denied("kill $P")
        self.assert_denied("kill -INT $(cat pidfile)")
        self.assert_denied("kill %1")

    def test_プロセスグループへ送るのは止める(self):
        self.assert_denied("kill -9 -123")
        self.assert_denied("kill 0")

    def test_前置きや連結の後ろの_kill_も拾う(self):
        for command in ("sudo kill 5682", "FOO=1 kill 5682", "echo x && kill 5682", "sleep 1\nkill 5682", "( kill 5682 )"):
            self.assertIn("別の Claude Code セッション", self.assert_denied(command), command)

    # --- kill ではないもの -------------------------------------------------

    def test_引用の中の語は拾わない(self):
        self.assert_allowed('git commit -m "fix: kill 5682 の後に孤児が残る"')
        self.assert_allowed("grep -n 'pkill' README.md")

    def test_heredoc_の本文は拾わない(self):
        self.assert_allowed("cat > note.md <<'EOF'\nkill 5682\npkill mokume\nEOF\necho done")

    def test_kill_を含む別の語は拾わない(self):
        self.assert_allowed("echo skill && ./killer.sh")

    def test_無効化スイッチで黙る(self):
        self.assert_allowed("kill 5682", CLAUDE_SIGNAL_GUARD="0")


if __name__ == "__main__":
    unittest.main()
