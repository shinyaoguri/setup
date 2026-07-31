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
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "素通しのはずが出力があった")

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
            "git branch -d merged/x",  # 小文字 -d はマージ済みしか消せない
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
