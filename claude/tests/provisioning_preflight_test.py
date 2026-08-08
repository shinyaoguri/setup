#!/usr/bin/env python3
"""claude/provisioning-preflight.sh のテスト。

実物の ansible を回すと遅く、結果がマシンの状態に依存してしまうので、PATH の先頭に
偽の `ansible-playbook` を置いて出力を固定する。検証したいのは「予告をどう読んで
どう判断するか」であって ansible の挙動ではない。

    python3 claude/tests/provisioning_preflight_test.py
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "provisioning-preflight.sh"

RECAP_NO_CHANGE = "localhost : ok=7 changed=0 unreachable=0 failed=0"
RECAP_WITH_CHANGE = "localhost : ok=16 changed=1 unreachable=0 failed=0"
DIFF_BODY = """TASK [Set Git global email] ****
--- before
+++ after
@@ -1 +1 @@
-36407060+shinyaoguri@users.noreply.github.com
+ogrsny@gmail.com

changed: [localhost]
"""


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def fake_ansible(self, body, exit_code=0):
        """偽 ansible-playbook を PATH に置く。--check のときだけ body を出す。"""
        script = self.bin / "ansible-playbook"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'if ! printf "%s" "$*" | grep -q -- "--check"; then\n'
            '  echo "本番実行された (テストの想定外)" >&2; exit 9\n'
            "fi\n"
            f"cat <<'EOF'\n{body}\nEOF\n"
            f"exit {exit_code}\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def slow_ansible(self, seconds):
        script = self.bin / "ansible-playbook"
        script.write_text(f"#!/usr/bin/env bash\nsleep {seconds}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def run_hook(self, command):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["PROVISIONING_PREFLIGHT_LIMIT"] = "2"   # 時間切れの検証を待たずに済ませる
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, cwd=self.root, env=env, timeout=60,
        )

    def assert_allowed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "素通しのはずが出力があった")

    def assert_decision(self, result, expected):
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], expected)
        return output["permissionDecisionReason"]


class ForecastTest(HookTestCase):
    """dry-run の結果で判断する。変わるものが無ければ人間を呼ばない。"""

    def test_no_change_passes_silently(self):
        """冪等な流し直しは止めない。"""
        self.fake_ansible(RECAP_NO_CHANGE)
        self.assert_allowed(self.run_hook("ansible-playbook playbook.yml --tags git"))

    def test_change_is_reported_with_diff(self):
        """変わるものがあるときだけ、中身を添えて判断を返す。"""
        self.fake_ansible(DIFF_BODY + RECAP_WITH_CHANGE)
        reason = self.assert_decision(
            self.run_hook("ansible-playbook playbook.yml --tags git"), "ask"
        )
        self.assertIn("1 件が変更される", reason)
        self.assertIn("noreply", reason)   # 実際に消える値が見えること
        self.assertIn("Set Git global email", reason)

    def test_dry_run_failure_asks(self):
        """予告できなかったものは通さない。"""
        self.fake_ansible("ERROR! playbook not found", exit_code=1)
        reason = self.assert_decision(self.run_hook("ansible-playbook nope.yml"), "ask")
        self.assertIn("失敗", reason)

    def test_unreadable_recap_asks(self):
        """PLAY RECAP が無ければ changed の数が分からない。"""
        self.fake_ansible("何かの出力だが RECAP が無い")
        reason = self.assert_decision(self.run_hook("ansible-playbook playbook.yml"), "ask")
        self.assertIn("読み取れなかった", reason)

    def test_timeout_asks(self):
        """時間切れも「分からない」なので通さない。"""
        self.slow_ansible(30)
        reason = self.assert_decision(self.run_hook("ansible-playbook playbook.yml"), "ask")
        self.assertIn("秒で終わらなかった", reason)


class CommandShapeTest(HookTestCase):
    """予告を組み立てられる形かどうかを先に見る。"""

    def test_cd_prefix_is_supported(self):
        """`cd <dir> && ansible-playbook …` は頻出。cd 先で予告する。"""
        self.fake_ansible(RECAP_NO_CHANGE)
        target = self.root / "setup"
        target.mkdir()
        self.assert_allowed(
            self.run_hook(f"cd {target} && ansible-playbook playbook.yml --tags git")
        )

    def test_compound_command_asks(self):
        """他のコマンドと繋がっていると、そのままでは予告できない。"""
        self.fake_ansible(RECAP_NO_CHANGE)
        reason = self.assert_decision(
            self.run_hook("ansible-playbook playbook.yml | tee log.txt"), "ask"
        )
        self.assertIn("繋がっている", reason)

    def test_explicit_check_passes(self):
        """dry-run そのものは状態を変えないので素通し (二重に走らせない)。"""
        self.assert_allowed(self.run_hook("ansible-playbook playbook.yml --check --diff"))

    def test_unrelated_commands_pass(self):
        for command in (
            "ls -la",
            "git status --short",
            "brew install jq",
            "echo ansible-playbook",   # 語として現れるだけの実行しない文字列
        ):
            with self.subTest(command):
                self.assert_allowed(self.run_hook(command))


if __name__ == "__main__":
    unittest.main(verbosity=2)
