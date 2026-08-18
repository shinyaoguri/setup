#!/usr/bin/env python3
"""zshenv のテスト。

zshenv は「人が打つとき以外にも要るもの」の置き場。zshrc は非対話シェル
(Claude Code の hook・scheduled task・cron) では読まれないので、そこへ置いたものは
無人セッションでだけ黙って消える。GYAZO_TOKEN_REF で一度踏み (#90)、SSH_AUTH_SOCK で
同じ轍を踏んだ (#91) ため、両方ここで固定する。

肝は SSH_AUTH_SOCK の分岐で、次の 2 つを対にして見る:
  - ローカルのシェルでは Secretive (Secure Enclave) の agent を指すこと
  - SSH 越しに入っているときは触らないこと (forwarding された agent を壊さない)

    python3 claude/tests/zshenv_test.py
"""

import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ZSHENV = REPO / "zshenv"
SECRETIVE_SOCK = "com.maxgoedjen.Secretive.SecretAgent/Data/socket.ssh"


def source_zshenv(**overrides):
    """zshenv を非対話 zsh で source し、見たい変数を取り出す。

    値に None を渡した変数は環境から落とす (未設定の再現)。
    """
    env = {k: v for k, v in os.environ.items()}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        ["zsh", "-c", f'source "{ZSHENV}"; printf "%s\\n%s\\n%s\\n" "$SSH_AUTH_SOCK" "$GYAZO_TOKEN_REF" "$PATH"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    sock, ref, path = result.stdout.split("\n")[:3]
    return {"SSH_AUTH_SOCK": sock, "GYAZO_TOKEN_REF": ref, "PATH": path}


class SshAuthSockTest(unittest.TestCase):
    """SSH_AUTH_SOCK の分岐。ここが崩れると無人セッションの署名が落ちる"""

    def test_points_at_secretive_on_a_local_shell(self):
        # 非対話シェルでも読まれること自体が要件 (zshrc に置くと読まれない)
        env = source_zshenv(SSH_CONNECTION=None, SSH_AUTH_SOCK=None)
        self.assertIn(SECRETIVE_SOCK, env["SSH_AUTH_SOCK"])

    def test_overrides_a_stale_value_on_a_local_shell(self):
        # 1Password 時代の値を引きずったセッションでも上書きされること
        env = source_zshenv(SSH_CONNECTION=None, SSH_AUTH_SOCK="/tmp/stale-1password.sock")
        self.assertIn(SECRETIVE_SOCK, env["SSH_AUTH_SOCK"])

    def test_leaves_a_forwarded_agent_alone_over_ssh(self):
        # SSH 越しは forwarding された agent を指している。上書きすると相手の鍵が使えなくなる
        env = source_zshenv(
            SSH_CONNECTION="10.0.0.1 54321 10.0.0.2 22",
            SSH_AUTH_SOCK="/tmp/forwarded-agent.sock",
        )
        self.assertEqual("/tmp/forwarded-agent.sock", env["SSH_AUTH_SOCK"])


class NonInteractiveEssentialsTest(unittest.TestCase):
    """zshrc ではなくここに置くべきものが揃っているか"""

    def test_exports_the_gyazo_token_reference(self):
        env = source_zshenv(GYAZO_TOKEN_REF=None)
        self.assertTrue(env["GYAZO_TOKEN_REF"].startswith("op://"))

    def test_puts_the_repository_bin_on_path(self):
        # secret-read は非対話シェルからも引ける必要がある
        env = source_zshenv()
        self.assertIn(str(REPO / "bin"), env["PATH"].split(":"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
