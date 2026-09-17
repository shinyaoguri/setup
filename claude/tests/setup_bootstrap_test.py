#!/usr/bin/env python3
"""setup.zsh / sillicon_mac_setup.zsh のブートストラップのテスト。

このリポジトリの目的は「1 行で新しいマシンを構築する」ことなので、その 1 行が
途中で黙って終わる形を回帰させない (issue #166)。実機のセットアップは流せないため、
検証は 2 点に絞る:

  - 取得に失敗したとき、空や部分的なスクリプトを実行せずに止まること (偽 curl で実測)
  - Homebrew を入れた直後に、実行中シェルへ PATH を通していること

    python3 claude/tests/setup_bootstrap_test.py
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

ROOT = Path(__file__).resolve().parent.parent.parent
SETUP = ROOT / "setup.zsh"
STAGE2 = ROOT / "sillicon_mac_setup.zsh"


class DownloadFailureTest(unittest.TestCase):
    """コマンド置換は終了ステータスを捨てるので、取得失敗が「成功」に見えていた。"""

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def fake_curl(self, exit_code, body=None):
        """偽 curl。body を渡すと -o の指す先へ書く (部分取得の再現にも使う)。"""
        script = self.bin / "curl"
        write = ""
        if body is not None:
            write = (
                'out=""\n'
                'prev=""\n'
                'for a in "$@"; do\n'
                '  if [ "$prev" = "-o" ]; then out=$a; fi\n'
                '  prev=$a\n'
                "done\n"
                f'[ -n "$out" ] && printf %s {body!r} > "$out"\n'
            )
        script.write_text(f"#!/bin/sh\n{write}exit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def run_setup(self):
        env = clean_env()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        return subprocess.run(
            ["zsh", str(SETUP)],
            capture_output=True, text=True, cwd=self.root, env=env, timeout=60,
        )

    def test_download_failure_stops_with_nonzero_exit(self):
        """取得に失敗したら止まる。空のスクリプトを実行して黙って終わらない。"""
        self.fake_curl(exit_code=22)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0, "取得失敗が成功として素通りした")
        self.assertIn("取得できませんでした", result.stdout + result.stderr)

    def test_download_failure_does_not_run_partial_script(self):
        """途中で切断されたときも、落ちてきた断片を実行しない。

        後段は sudo セッションを握って cask / mas / ansible を回すので、
        部分実行には実害がある。
        """
        marker = self.root / "PARTIAL_RAN"
        self.fake_curl(exit_code=18, body=f"touch {marker}\n")   # 18 = 転送が途中で終わった
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists(), "部分的に取得したスクリプトを実行した")


class HomebrewPathTest(unittest.TestCase):
    """インストーラは実行中シェルの PATH を変えない。入れた直後に通す必要がある。"""

    def test_stage2_exports_homebrew_path_after_install(self):
        """`brew shellenv` を eval していないと、直後の Step 3 が command not found で死ぬ。

        Homebrew は入った後に落ちるので原因が分かりにくい形になる。実機の
        インストールは流せないため、入れた直後に PATH を通す一行が在ることを見る。
        """
        body = STAGE2.read_text()
        install = body.index("install.sh")
        step3 = body.index("Step 3: Ansible")
        between = body[install:step3]
        self.assertIn(
            'eval "$(/opt/homebrew/bin/brew shellenv)"', between,
            "Homebrew を入れた後、Step 3 までの間に PATH を通していない",
        )

    def test_brew_shellenv_actually_puts_brew_on_path(self):
        """その一行が実際に効くこと (手元に Homebrew がある場合のみ)。"""
        brew = Path("/opt/homebrew/bin/brew")
        if not brew.exists():
            self.skipTest("Homebrew が無い環境")
        env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            ["zsh", "-c", 'eval "$(/opt/homebrew/bin/brew shellenv)"; command -v brew'],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith("/brew"), result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
