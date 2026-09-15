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

# setup#153: 孤児の出所を見るための印。scratchpad は 1 セッション 1 つなので session_id は
# 持ち主を一意に決めるが、作業ディレクトリは同じ所を複数のセッションが開きうる
SESSION = "25ffe969-aaa7-4e44-ae8b-7d73ff17aebf"
OTHER_SESSION = "ed5539c6-47dd-4545-85c0-5131afae864b"
OWN_CWD = "/Users/u/Repos/proj/.claude/worktrees/issue-1"
OTHER_CWD = "/Users/u/Repos/proj/.claude/worktrees/issue-2"
SCRATCH = "/private/tmp/claude-501/-Users-u-Repos-proj"
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
    # setup#153 の実測を写した孤児 — スケッチは起動したシェルが終わると ppid=1 へ移る
    (31001, 1, "sketch", f"mokume-cli run {SCRATCH}/{SESSION}/scratchpad/sketch.swift"),
    (31002, 1, "sketch", f"mokume-cli run {SCRATCH}/{OTHER_SESSION}/scratchpad/sketch.swift"),
    (31003, 1, "mokume-cli", "mokume-cli watch"),  # 出所は cwd にしか無い (下の CWD)
    (31004, 1, "mokume-cli", "mokume-cli watch"),
    # 名前がこのセッションの worktree で始まるだけの**別の** worktree (issue-1 と issue-1-other)
    (31005, 1, "mokume-cli", "mokume-cli watch"),
    (31006, 1, "sketch", f"mokume-cli run {OWN_CWD}-other/.build/debug/sketch"),
]

# プロセスの cwd (fixture の 6 列目)。書いておくとフックは lsof を呼ばない
CWD = {
    88906: OWN_CWD,  # このセッションの claude
    5666: OTHER_CWD,  # 別セッションの claude — 別の worktree を開いている
    30652: "/tmp/fresh",
    31003: OWN_CWD,
    31004: OTHER_CWD,
    31005: OWN_CWD + "-other",
}

# PreToolUse の payload が渡してくる印
MINE = {"session_id": SESSION, "cwd": OWN_CWD}
# 別セッション (5666) と同じ worktree を開いている場合
SHARED = {"session_id": SESSION, "cwd": OTHER_CWD}

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
            cwd = CWD.get(pid, "")
            cls.fixture.write(
                f"{pid}\t{ppid}\tMon Sep 14 17:12:54 2026\t{comm}\t{args}\t{cwd}\n"
            )
        cls.fixture.close()

    @classmethod
    def tearDownClass(cls):
        Path(cls.fixture.name).unlink()

    def run_hook(self, command, payload=None, **env):
        values = {"SIGNAL_GUARD_PS": self.fixture.name, "SIGNAL_GUARD_SELF": str(SELF)}
        values.update(env)
        body = {"tool_name": "Bash", "tool_input": {"command": command}}
        body.update(payload or {})
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(**values),
        )

    def decision(self, command, payload=None, **env):
        result = self.run_hook(command, payload, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None, ""
        output = json.loads(result.stdout)["hookSpecificOutput"]
        return output["permissionDecision"], output["permissionDecisionReason"]

    def assert_allowed(self, command, payload=None, **env):
        decision, reason = self.decision(command, payload, **env)
        self.assertIsNone(decision, f"素通しのはずが {decision}: {command}\n{reason}")

    def assert_denied(self, command, payload=None, **env):
        decision, reason = self.decision(command, payload, **env)
        self.assertEqual(decision, "deny", f"止めるはずが {decision}: {command}")
        return reason

    def assert_asked(self, command, payload=None, **env):
        decision, reason = self.decision(command, payload, **env)
        self.assertEqual(decision, "ask", f"聞くはずが {decision}: {command}")
        return reason

    def assert_decided_allow(self, command, payload=None, **env):
        """素通し (判定を permissions へ戻す) ではなく、フックが allow を名乗ること。"""
        decision, reason = self.decision(command, payload, **env)
        self.assertEqual(decision, "allow", f"確認なしで通すはずが {decision}: {command}\n{reason}")
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

    # --- 孤児の出所で持ち主を見直す (setup#153) ----------------------------

    def test_自分の_scratchpad_から起きた孤児は確認なしで通す(self):
        reason = self.assert_decided_allow("kill 31001", MINE)
        self.assertIn(SESSION, reason)

    def test_別のセッションの_scratchpad_から起きた孤児は聞く(self):
        self.assert_asked("kill 31002", MINE)

    def test_自分の作業ディレクトリから起きた孤児は確認なしで通す(self):
        # 出所が cwd にしか無い形 (`mokume-cli watch` を引数なしで立てたもの)
        self.assertIn(OWN_CWD, self.assert_decided_allow("kill 31003", MINE))

    def test_同じ作業ディレクトリを開いたセッションが他に居れば聞く(self):
        reason = self.assert_asked("kill 31004", SHARED)
        self.assertIn("他にも居る", reason)
        self.assertIn("5666", reason)

    def test_出所がどちらでもない孤児は印を渡しても聞く(self):
        self.assert_asked("kill 30652", MINE)

    def test_名前が前方一致するだけの別の_worktree_は自分のものにしない(self):
        # issue-1 と issue-1-other。前方一致で見ると隣の worktree を自分のものと誤判定する
        self.assert_asked("kill 31005", MINE)  # cwd に出る形
        self.assert_asked("kill 31006", MINE)  # コマンド行に出る形

    def test_出所の印を渡しても別セッションのものは止める(self):
        self.assertIn("別の Claude Code セッション", self.assert_denied("kill 5682", MINE))

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
