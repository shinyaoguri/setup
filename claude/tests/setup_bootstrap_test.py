#!/usr/bin/env python3
"""setup.zsh / sillicon_mac_setup.zsh のブートストラップのテスト。

このリポジトリの目的は「1 行で新しいマシンを構築する」ことなので、その 1 行が
途中で黙って終わる形を回帰させない (issue #166)。実機のセットアップは流せないため、
検証は 3 点に絞る:

  - 取得に失敗したとき、空や部分的なスクリプトを実行せずに止まること (偽 curl で実測)
  - 入口 (setup.zsh) が後段の終了コードをそのまま返し、README が案内するフラグを
    後段へ渡すこと (後段を偽物に差し替えて実測。issue #194 / #195)
  - Homebrew を入れた直後に、実行中シェルへ PATH を通していること

    python3 claude/tests/setup_bootstrap_test.py
"""

import os
import shutil
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


class EntryPointTest(unittest.TestCase):
    """入口は後段を呼ぶだけの薄い層。終了コードと引数をそのまま通すこと。

    後段 (sillicon_mac_setup.zsh) のオプション解析は bootstrap_selection_test.py が
    見ているが、**入口がそこまで引数を届けているか**は誰も流していなかった。README が
    案内する `zsh setup.zsh --with-optional` は入口の getopts で弾かれ、cloud モードは
    zsh の読み取り専用変数 `status` への代入で、成功しても exit 1 になっていた。
    """

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.args_file = self.root / "STAGE2_ARGS"
        self.download_record = self.root / "DOWNLOADED_TO"

    def stage2_body(self, exit_code):
        """偽の後段。渡された引数を 1 行 1 個で書き出し、指定のコードで終わる。"""
        return f'for a in "$@"; do echo "$a"; done > {self.args_file}; exit {exit_code}'

    def fake_curl_serving(self, body):
        """-o の指す先へ body を書いて成功する偽 curl (cloud モードの取得を再現)。

        書いた先のパスも控える。macOS の mktemp は TMPDIR でなく
        _CS_DARWIN_USER_TEMP_DIR を見るので、置き場を隔離して後始末を確かめることが
        できない — 入口が実際に使ったパスを偽 curl から教えてもらう。
        """
        script = self.bin / "curl"
        script.write_text(
            "#!/bin/sh\n"
            'out=""\n'
            'prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "-o" ]; then out=$a; fi\n'
            "  prev=$a\n"
            "done\n"
            f'printf %s {body!r} > "$out"\n'
            f'printf %s "$out" > {self.download_record}\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def run_entry(self, script, *args):
        env = clean_env()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        return subprocess.run(
            ["zsh", str(script), *args],
            capture_output=True, text=True, cwd=self.root, env=env, timeout=60,
        )

    def forwarded_args(self):
        return self.args_file.read_text().split()

    # --- cloud モード -------------------------------------------------------

    def test_cloud_mode_returns_zero_when_stage2_succeeds(self):
        """後段が成功したら 0 で終わる。

        `status=$?` と書いていたため、zsh の読み取り専用変数への代入で落ちて
        **成功しても exit 1** になっていた (issue #195)。
        """
        self.fake_curl_serving(self.stage2_body(0))
        result = self.run_entry(SETUP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("read-only variable", result.stderr)

    def test_cloud_mode_propagates_stage2_exit_code(self):
        """後段の失敗は、その終了コードのまま返す (握り潰しも、別の値への化けもしない)。"""
        self.fake_curl_serving(self.stage2_body(7))
        result = self.run_entry(SETUP)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    def test_cloud_mode_removes_downloaded_script(self):
        """取得したスクリプトは、後段が成功しても失敗しても残さない。"""
        for code in (0, 7):
            with self.subTest(stage2_exit=code):
                self.fake_curl_serving(self.stage2_body(code))
                self.run_entry(SETUP)
                downloaded = Path(self.download_record.read_text())
                self.addCleanup(downloaded.unlink, missing_ok=True)
                self.assertFalse(downloaded.exists(), f"一時ファイルが残った: {downloaded}")

    def test_cloud_mode_forwards_optional_flags(self):
        """README が案内する形。入口の getopts が `--` を弾いていた (issue #194)。"""
        for flag in ("--with-optional", "--no-optional"):
            with self.subTest(flag=flag):
                self.fake_curl_serving(self.stage2_body(0))
                result = self.run_entry(SETUP, flag)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.forwarded_args(), [flag])

    # --- local モード -------------------------------------------------------

    def local_checkout(self, exit_code=0):
        """入口だけを本物にした偽の checkout。後段は `${0:A:h}` の隣から引かれる。"""
        checkout = self.root / "checkout"
        checkout.mkdir()
        shutil.copy(SETUP, checkout / "setup.zsh")
        (checkout / STAGE2.name).write_text(self.stage2_body(exit_code) + "\n")
        return checkout / "setup.zsh"

    def test_local_mode_forwards_optional_flags(self):
        """-l と並べて渡しても届く。順序はどちらでもよい。"""
        entry = self.local_checkout()
        for args in (["-l", "--no-optional"], ["--with-optional", "-l"]):
            with self.subTest(args=args):
                result = self.run_entry(entry, *args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                flag = next(a for a in args if a != "-l")
                self.assertEqual(sorted(self.forwarded_args()), sorted(["-l", flag]))

    def test_unknown_option_is_rejected_before_running_anything(self):
        """知らないオプションは後段を呼ばずに止める (黙って無視して全部入れない)。"""
        entry = self.local_checkout()
        result = self.run_entry(entry, "-l", "--with-everything")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.args_file.exists(), "後段が実行された")


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
