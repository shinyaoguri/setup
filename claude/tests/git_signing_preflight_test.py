#!/usr/bin/env python3
"""claude/git-signing-preflight.sh のテスト。

フックは stdin の JSON を読んで走り切る作りなので、実際の契約
(stdin の JSON → stdout の deny JSON / 無出力) をサブプロセス経由で検証する。
op-ssh-sign は本物を呼ぶと 1Password の承認プロンプトが出てしまうため、
CLAUDE_SIGNING_PREFLIGHT_SIGNER で偽物に差し替える。

    python3 claude/tests/git_signing_preflight_test.py
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "git-signing-preflight.sh"

# 偽の op-ssh-sign。git は `-Y sign -n git -f <鍵> <署名対象>` の順で呼ぶので、
# 署名対象は常に最後の引数になる。
SIGNER_OK = """#!/bin/sh
eval payload=\\${$#}
printf -- '-----BEGIN SSH SIGNATURE-----\\n' > "$payload.sig"
"""

SIGNER_FAILS = """#!/bin/sh
echo "1Password: No such file or directory (os error 2)" >&2
exit 1
"""

# 1Password アプリが終了しているとこうなる (ソケットのファイルだけは残る)
SIGNER_NOT_RUNNING = """#!/bin/sh
echo "1Password: Could not connect to socket. Is the agent running?" >&2
exit 1
"""

# 承認プロンプトが背面で待ち続けている状態 (1Password 側は 60 秒で失効させる) の再現
SIGNER_HANGS = """#!/bin/sh
sleep 60
"""

# 署名は 0 で返るのに署名ファイルが無い、という壊れ方も落としたい
SIGNER_SILENTLY_EMPTY = """#!/bin/sh
exit 0
"""

# 本物の op-ssh-sign は署名先の名前を渡した名前から組み立てる (p.txt なら p.sig)。
# こちらで名前を決め打ちすると取りこぼすので、名前を変えてくる署名でも通ることを守る。
SIGNER_RENAMES_SIGNATURE = """#!/bin/sh
eval payload=\\${$#}
printf -- '-----BEGIN SSH SIGNATURE-----\\n' > "$(dirname "$payload")/renamed.sig"
"""


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.root = Path(self.workdir.name)
        self.addCleanup(self.workdir.cleanup)

        # user.signingkey はリポジトリ設定から読むので、テスト用のリポジトリを用意する
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.key = self.root / "signing_key.pub"
        self.key.write_text("ssh-ed25519 AAAA... test\n")
        self.set_signing_key(str(self.key))

    def set_signing_key(self, value):
        subprocess.run(
            ["git", "config", "--local", "user.signingkey", value],
            cwd=self.repo,
            check=True,
        )

    def make_signer(self, body, name="op-ssh-sign"):
        """偽の op-ssh-sign を置いてそのパスを返す。

        名前が op-ssh-sign で終わるかどうかでフックの対象判定が変わるので、
        名前も差し替えられるようにしておく。
        """
        path = self.root / name
        path.write_text(body)
        path.chmod(0o755)
        return str(path)

    def run_hook(self, command, signer=None, timeout=None):
        env = dict(os.environ)
        env["CLAUDE_SIGNING_PREFLIGHT_SIGNER"] = (
            signer if signer is not None else self.make_signer(SIGNER_OK)
        )
        env["CLAUDE_SIGNING_PREFLIGHT_TIMEOUT"] = str(timeout if timeout else 5)
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=env,
            timeout=90,
        )

    def assert_allowed(self, result):
        """素通し (無出力 + 終了コード 0) を確かめる。"""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "素通しのはずが出力があった")

    def assert_denied(self, result):
        """deny の JSON を返していることを確かめ、理由の文字列を返す。"""
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        return output["permissionDecisionReason"]


class BypassTest(HookTestCase):
    """署名なしコミットへの逃げ道は塞ぐ。"""

    def test_no_gpg_sign_is_denied(self):
        reason = self.assert_denied(self.run_hook('git commit --no-gpg-sign -m "x"'))
        self.assertIn("署名なしコミットは禁止", reason)

    def test_gpgsign_false_is_denied(self):
        reason = self.assert_denied(
            self.run_hook('git -c commit.gpgsign=false commit -m "x"')
        )
        self.assertIn("署名なしコミットは禁止", reason)

    def test_bypass_is_denied_even_without_signing_setup(self):
        """署名の設定を見に行く前に落とす (設定不備を口実に素通ししない)。"""
        self.set_signing_key(str(self.root / "missing.pub"))
        self.assert_denied(
            self.run_hook('git commit --no-gpg-sign -m "x"', signer="/nonexistent")
        )


class ScopeTest(HookTestCase):
    """署名が走らないものにまで事前確認を持ち込まない。"""

    def test_unrelated_command_passes(self):
        self.assert_allowed(self.run_hook("ls -la"))

    def test_read_only_git_command_passes(self):
        self.assert_allowed(self.run_hook("git status --short"))

    def test_signer_other_than_1password_passes(self):
        """1Password 以外で署名している環境ではこのフックの出番はない。"""
        signer = self.make_signer(SIGNER_FAILS, name="gpg")
        self.assert_allowed(self.run_hook('git commit -m "x"', signer=signer))

    def test_commit_after_another_command_is_checked(self):
        """&& でつないだ先の commit も拾う。"""
        signer = self.make_signer(SIGNER_FAILS)
        self.assert_denied(
            self.run_hook('git add -A && git commit -m "x"', signer=signer)
        )

    def test_git_with_global_option_is_checked(self):
        signer = self.make_signer(SIGNER_FAILS)
        self.assert_denied(
            self.run_hook('git -C /tmp/repo commit -m "x"', signer=signer)
        )

    def test_signed_tag_is_checked(self):
        signer = self.make_signer(SIGNER_FAILS)
        self.assert_denied(self.run_hook('git tag -s v1.0 -m "x"', signer=signer))

    def test_env_prefixed_commit_is_checked(self):
        signer = self.make_signer(SIGNER_FAILS)
        self.assert_denied(
            self.run_hook('TMPDIR=/private/tmp git commit -m "x"', signer=signer)
        )

    def test_commit_inside_a_string_is_not_checked(self):
        """コマンドとして走らない commit を拾って読み取り操作まで止めない。"""
        signer = self.make_signer(SIGNER_FAILS)
        self.assert_allowed(self.run_hook('echo "git commit -m x"', signer=signer))


class PreflightTest(HookTestCase):
    """署名できるかを実際に試してから通す。"""

    def test_commit_passes_when_signing_works(self):
        self.assert_allowed(self.run_hook('git commit -m "x"'))

    def test_commit_is_denied_when_signing_fails(self):
        signer = self.make_signer(SIGNER_FAILS)
        reason = self.assert_denied(self.run_hook('git commit -m "x"', signer=signer))
        self.assertIn("事前確認に失敗", reason)
        # 何が起きたのかを判断できるよう、署名プログラムの出力をそのまま渡す
        self.assertIn("os error 2", reason)

    def test_signature_is_found_even_if_the_signer_names_it(self):
        """署名ファイルの名前を決め打ちにしない (op-ssh-sign は名前を組み立て直す)。"""
        signer = self.make_signer(SIGNER_RENAMES_SIGNATURE)
        self.assert_allowed(self.run_hook('git commit -m "x"', signer=signer))

    def test_commit_is_denied_when_signature_is_missing(self):
        signer = self.make_signer(SIGNER_SILENTLY_EMPTY)
        reason = self.assert_denied(self.run_hook('git commit -m "x"', signer=signer))
        self.assertIn("事前確認に失敗", reason)

    def test_commit_is_denied_without_waiting_for_the_prompt_to_expire(self):
        """承認プロンプトが背面で待っている場合、60 秒の失効を待たずに打ち切る。"""
        signer = self.make_signer(SIGNER_HANGS)
        result = self.run_hook('git commit -m "x"', signer=signer, timeout=1)
        reason = self.assert_denied(result)
        self.assertIn("応答しなかった", reason)
        self.assertIn("1Password", reason)

    def test_missing_signing_key_is_denied(self):
        self.set_signing_key(str(self.root / "missing.pub"))
        reason = self.assert_denied(self.run_hook('git commit -m "x"'))
        self.assertIn("公開鍵が読めない", reason)

    def test_missing_signer_binary_is_denied(self):
        reason = self.assert_denied(
            self.run_hook(
                'git commit -m "x"', signer=str(self.root / "nowhere/op-ssh-sign")
            )
        )
        self.assertIn("署名プログラムが見つからない", reason)


class RemedyTest(HookTestCase):
    """止めるだけでなく、症状に合った直し方を伝える。"""

    def test_agent_not_running_is_told_apart(self):
        """1Password が終了しているときは、そう言い当てて常駐の設定まで案内する。"""
        signer = self.make_signer(SIGNER_NOT_RUNNING)
        reason = self.assert_denied(self.run_hook('git commit -m "x"', signer=signer))
        self.assertIn("1Password アプリが起動していない", reason)
        self.assertIn("メニューバー", reason)
        # ロックや承認の話に迷い込ませない
        self.assertNotIn("ロックを解除", reason)

    def test_no_response_points_at_the_prompt_and_the_lock(self):
        signer = self.make_signer(SIGNER_HANGS)
        reason = self.assert_denied(
            self.run_hook('git commit -m "x"', signer=signer, timeout=1)
        )
        self.assertIn("承認プロンプト", reason)
        self.assertIn("ロックを解除", reason)

    def test_every_denial_forbids_retrying(self):
        for name, body in (
            ("失敗", SIGNER_FAILS),
            ("未起動", SIGNER_NOT_RUNNING),
        ):
            with self.subTest(name):
                signer = self.make_signer(body)
                reason = self.assert_denied(
                    self.run_hook('git commit -m "x"', signer=signer)
                )
                self.assertIn("繰り返し試さない", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
