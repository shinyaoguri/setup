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
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ZSHENV = REPO / "zshenv"
ZPROFILE = REPO / "zprofile"
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
        ["zsh", "-f", "-c", f'source "{ZSHENV}"; printf "%s\\n%s\\n%s\\n" "$SSH_AUTH_SOCK" "$GYAZO_TOKEN_REF" "$PATH"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    sock, ref, path = result.stdout.split("\n")[:3]
    return {"SSH_AUTH_SOCK": sock, "GYAZO_TOKEN_REF": ref, "PATH": path}


def read_var(name):
    """zshenv を source して 1 つの変数を取り出す (環境に既にある値は落としてから見る)。"""
    env = {k: v for k, v in os.environ.items()}
    env.pop(name, None)
    return subprocess.run(
        ["zsh", "-f", "-c", f'source "{ZSHENV}"; printf "%s" "${name}"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


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

    def test_exports_the_gyazo_token_command_for_mokume(self):
        # mokume の口はコマンドの形しか受け取らない (スキルは eval・example-shots は bash -c)。
        # **無いと、失効したトークンと同じ 401 になる** — 空の access_token にも Gyazo は
        # You are not authorized. を返すので、未設定が「トークンが死んだ」に見える (#159)
        self.assertTrue(read_var("MOKUME_GYAZO_TOKEN_CMD"))

    def test_the_gyazo_token_command_reads_the_reference_variable(self):
        # 参照の literal を 2 つに増やさない。直書きすると、参照を変えたときに片方だけ古くなる
        reference = read_var("GYAZO_TOKEN_REF")
        command = read_var("MOKUME_GYAZO_TOKEN_CMD")
        self.assertIn("$GYAZO_TOKEN_REF", command, "参照を直書きしている")
        argument = subprocess.run(
            ["bash", "-c", 'eval "set -- $CMD"; printf "%s" "$2"'],
            env={"PATH": "/usr/bin:/bin", "CMD": command, "GYAZO_TOKEN_REF": reference},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(reference, argument)

    def test_puts_the_repository_bin_on_path(self):
        # secret-read は非対話シェルからも引ける必要がある
        env = source_zshenv()
        self.assertIn(str(REPO / "bin"), env["PATH"].split(":"))

    def test_puts_homebrew_on_path(self):
        """op も非対話シェルから引ける必要がある (issue #170)。

        secret-read の refresh_if_stale は `command -v op` で抜けるので、op が
        引けないと**自動ローテートが一度も走らない**。zshrc は対話シェルしか読まれず、
        hook / cron / launchd から走る zsh には届かない。
        """
        env = source_zshenv(PATH="/usr/bin:/bin")
        path = env["PATH"].split(":")
        self.assertIn("/opt/homebrew/bin", path)
        self.assertIn("/opt/homebrew/sbin", path)


class CommandWithoutPathTest(unittest.TestCase):
    """変数で渡すコマンドは PATH に頼らない (#154)。

    Claude デスクトップアプリは起動時の PATH を持ち続け、Bash ツールのシェルスナップショットが
    zshenv の後でそれを書き戻す。PATH だけが上書きされて変数は残るので、「変数はあるのに
    コマンドが無い」になり、mokume のエージェントは 1Password の承認待ちへ落ちていた。
    """

    def assert_resolves_without_setup_bin_on_path(self, name):
        cmd = read_var(name)
        # 使う側 (mokume の gh-app-token.sh・gyazo-evidence スキル) は bash で eval する。
        # PATH は setup を知らない形にする
        resolved = subprocess.run(
            ["bash", "-c", 'eval "set -- $CMD"; command -v "$1"'],
            env={"PATH": "/usr/bin:/bin", "CMD": cmd},
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, resolved.returncode, f"解決できない: {cmd}")
        self.assertEqual(str(REPO / "bin" / "secret-read"), resolved.stdout.strip())

    def test_app_private_key_command_resolves_without_setup_bin_on_path(self):
        self.assert_resolves_without_setup_bin_on_path("MOKUME_APP_PRIVATE_KEY_CMD")

    def test_gyazo_token_command_resolves_without_setup_bin_on_path(self):
        self.assert_resolves_without_setup_bin_on_path("MOKUME_GYAZO_TOKEN_CMD")


class LoginShellPathOrderTest(unittest.TestCase):
    """ログインシェルでも Homebrew がシステムのパスより前に来る (issue #197)。

    macOS の /etc/zprofile は path_helper を呼び、zshenv が組んだ PATH を「システムの
    パスを先頭、残りを後ろ」に並べ替える。Homebrew の PATH を zshenv へ移した (#170) のは
    非対話シェルへ届かせるための正しい修正だが、ログインシェル (Terminal.app が開く形) では
    /opt/homebrew/bin が /usr/bin の後ろへ回り、`git` が Apple のものを指していた。
    人が打つ端末と無人セッションで、同じ名前が別の実行ファイルになる。

    配備と同じ形 (~/.zshenv と ~/.zprofile が setup への symlink) を ZDOTDIR の下に作り、
    本物の /etc/zprofile を通したうえで順序を見る。
    """

    def login_shell_path(self):
        with tempfile.TemporaryDirectory() as zdotdir:
            (Path(zdotdir) / ".zshenv").symlink_to(ZSHENV)
            if ZPROFILE.exists():
                (Path(zdotdir) / ".zprofile").symlink_to(ZPROFILE)
            env = {k: v for k, v in os.environ.items()}
            env["ZDOTDIR"] = zdotdir
            # 呼び出し元の PATH に Homebrew が居ると、並べ替えの前後が紛れる
            env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            result = subprocess.run(
                ["zsh", "-l", "-c", "print -l $path"],
                env=env, capture_output=True, text=True, check=True,
            )
        return result.stdout.split("\n")

    def test_path_helper_is_in_play(self):
        """対照。path_helper が無い環境では、下のテストは何も確かめていない。"""
        if not Path("/usr/libexec/path_helper").exists():
            self.skipTest("path_helper が無い環境")
        self.assertIn("path_helper", Path("/etc/zprofile").read_text())

    def test_homebrew_comes_before_system_paths_in_a_login_shell(self):
        path = self.login_shell_path()
        self.assertIn("/opt/homebrew/bin", path)
        self.assertLess(
            path.index("/opt/homebrew/bin"), path.index("/usr/bin"),
            "ログインシェルで /opt/homebrew/bin が /usr/bin の後ろにある "
            "(brew で入れた git などが使われない)",
        )
        self.assertLess(path.index("/opt/homebrew/sbin"), path.index("/usr/sbin"))

    def test_homebrew_is_listed_once(self):
        """先頭へ入れ直しても重複させない (typeset -U が効いていること)。"""
        path = self.login_shell_path()
        self.assertEqual(path.count("/opt/homebrew/bin"), 1)


class AllowlistConsistencyTest(unittest.TestCase):
    """zshenv が渡す参照は、どれも secret-cache-allowlist に載っている (issue #215)。

    zshenv の参照は無人セッション (hook・scheduled task) のために置いてある。許可リストに
    無い参照は secret-read がキャッシュせず、素の `op read` へ落ちる — つまり 1Password の
    承認を待って止まる。このリポジトリが繰り返し踏んできた症状で、原因が「2 か所の
    不一致」だとは気付きにくい。

    mokume の鍵の参照は日付入りのファイル名で、鍵を差し替えるたびに zshenv と許可リストの
    両方を直す必要がある。片方だけ直した変更を CI で落とす。
    """

    ALLOWLIST = REPO / "secret-cache-allowlist"

    def allowed(self):
        lines = (line.strip() for line in self.ALLOWLIST.read_text().splitlines())
        return {line for line in lines if line and not line.startswith("#")}

    def exported_references(self):
        """zshenv を実際に source し、export された変数の値から op:// の参照を拾う。

        ファイルを正規表現で読まないのは、`$GYAZO_TOKEN_REF` のように**別の変数を経由して
        渡している参照**を展開後の形で見るためと、コメントの中の例を拾わないため。
        """
        output = subprocess.run(
            ["zsh", "-f", "-c", f'source "{ZSHENV}"; export -p'],
            env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, check=True,
        ).stdout
        # 参照は空白を含む (op://Automation/Gyazo API/credential)。引用符か行末まで
        return set(re.findall(r"op://[^\"'$\\\n]+", output))

    def test_references_are_found(self):
        """対照。1 つも拾えていなければ、下は何も確かめていない。"""
        references = self.exported_references()
        self.assertIn("op://Automation/Gyazo API/credential", references)
        self.assertGreaterEqual(len(references), 2, references)

    def test_every_exported_reference_is_allowlisted(self):
        missing = self.exported_references() - self.allowed()
        self.assertEqual(
            missing, set(),
            "zshenv が渡している参照が secret-cache-allowlist に無い "
            "(キャッシュされず、無人セッションが 1Password の承認待ちで止まる)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
