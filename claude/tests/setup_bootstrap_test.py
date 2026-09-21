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
        # 呼ばれた痕跡を残す。偽物より前に本物の curl が引かれると、テストは本物の
        # bootstrap を落として実行する (issue #238) — 「偽物が呼ばれた」を必ず確かめる
        called = f'touch "{self.root / "FAKE_CURL_CALLED"}"\n'
        script.write_text(f"#!/bin/sh\n{called}{write}exit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def assert_fake_curl_was_used(self):
        self.assertTrue(
            (self.root / "FAKE_CURL_CALLED").exists(),
            "偽 curl が呼ばれていない — 本物の curl が先に引かれている可能性がある",
        )

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
        self.assert_fake_curl_was_used()
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
        self.assert_fake_curl_was_used()
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
            # -f: ~/.zshenv が先に /opt/homebrew/bin を通すと、eval が無くても緑になる
            ["zsh", "-f", "-c", 'eval "$(/opt/homebrew/bin/brew shellenv)"; command -v brew'],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith("/brew"), result.stdout)


class XcodeLicenseTest(unittest.TestCase):
    """`xcode-select -p` が通っても git が動くとは限らない (issue #266)。

    新品の Mac は Xcode を一度も開いていないので、Xcode.app が選ばれていると
    ライセンス未同意で git / clang が終了コード 69 で落ちる。Step 1 がそれを
    素通しすると、直後の Step 1.5 で git が失敗し、理由を捨てた結果
    「別のリポジトリです」という無関係な診断で終わる。

    Step 1 の断片だけを切り出して流す (#218 — テスト側にロジックの写しを持たない)。
    """

    LICENSE_ERROR = (
        "You have not agreed to the Xcode license agreements. "
        "You must agree to both license agreements below in order to use Xcode."
    )

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.git_called = self.root / "FAKE_GIT_CALLED"
        # CLT は入っている前提にする (Step 1 の前半は issue #166 が見ている)
        self.fake_command("xcode-select", "echo /Library/Developer/CommandLineTools\n")

    def fake_command(self, name, body):
        script = self.bin / name
        script.write_text(f"#!/bin/sh\n{body}")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def fake_git(self, body):
        """偽 git。呼ばれた痕跡を残す — 本物が先に引かれていたら気付く (issue #238)。"""
        self.fake_command("git", f'touch "{self.git_called}"\n{body}')

    def assert_fake_git_was_used(self):
        self.assertTrue(
            self.git_called.exists(),
            "偽 git が呼ばれていない — 本物の git が先に引かれている可能性がある",
        )

    def run_step1(self):
        body = STAGE2.read_text()
        step1 = body[body.index('echo "📦 Step 1: '):body.index("# Step 1.5:")]
        return subprocess.run(
            # -t 0 が偽になる (端末が無い) ので、同意画面は出さず案内して止まる側を通る
            ["zsh", "-f", "-c", "set -e\n" + step1],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
            env=clean_env(HOME=str(self.root), PATH=f"{self.bin}:/usr/bin:/bin"),
        )

    def test_license_not_accepted_stops_with_the_command_to_run(self):
        """ライセンス未同意は、同意の一手を出して止まる (今回の回帰)。"""
        self.fake_git(f'echo "{self.LICENSE_ERROR}" >&2\nexit 69\n')
        result = self.run_step1()
        self.assert_fake_git_was_used()
        self.assertNotEqual(result.returncode, 0, "git が動かないまま先へ進んだ")
        self.assertIn("xcodebuild -license accept", result.stdout + result.stderr)

    def test_other_git_failure_shows_the_reason(self):
        """ライセンス以外の失敗も、git のメッセージをそのまま見せて止まる。"""
        self.fake_git('echo "xcode-select: error: invalid active developer path" >&2\nexit 1\n')
        result = self.run_step1()
        self.assert_fake_git_was_used()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid active developer path", result.stdout + result.stderr)

    def test_working_git_passes_through(self):
        """git が動くなら何も言わずに次のステップへ進む。"""
        self.fake_git('echo "git version 2.39.5"\n')
        result = self.run_step1()
        self.assert_fake_git_was_used()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SetupRepoStateTest(unittest.TestCase):
    """Step 1.5 は $HOME/.setup の状態を見分ける (issue #266)。

    `git remote get-url origin 2>/dev/null || echo ""` が失敗理由を捨てていたため、
    git が動かない・origin が無いといった別の事情まで「別のリポジトリです: (空)」に
    化けていた。**空を「別のリポジトリ」と呼ばない**ことを見る。
    """

    OTHER_URL = "https://github.com/someone/other.git"

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.setup_dir = self.home / ".setup"
        self.git_log = self.root / "GIT_LOG"

    def fake_git(self, cases=""):
        """偽 git。引数は毎回ログへ残す (本物が引かれていないかも、ここで分かる)。

        clone だけは本物と同じく「最後の引数の置き場を作る」ところまで真似る。
        そこを真似ないと、どこへクローンしているのかを見分けられない。
        """
        script = self.bin / "git"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.git_log}"\n'
            'for a in "$@"; do last=$a; done\n'
            'case "$*" in\n'
            f"{cases}\n"
            '  *clone*) mkdir -p "$last/.git" ;;\n'
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def leftovers(self):
        """$HOME に残った作りかけ (.setup.partial.<pid> など)。"""
        return sorted(p.name for p in self.home.iterdir() if p.name.startswith(".setup."))

    def git_calls(self):
        self.assertTrue(
            self.git_log.exists(),
            "偽 git が呼ばれていない — 本物の git が先に引かれている可能性がある",
        )
        return self.git_log.read_text().splitlines()

    def existing_checkout(self):
        (self.setup_dir / ".git").mkdir(parents=True)

    def run_step15(self):
        body = STAGE2.read_text()
        # SETUP_DIR / GITHUB_REPO_URL / localmode は変数初期化の塊が持っている
        variables = body[body.index("localmode=false"):body.index("usage() {")]
        step15 = body[body.index("# Step 1.5:"):body.index("# Step 2: Homebrew")]
        code = "set -e\n" + variables + step15 + '\nprintf "%s" "$PLAYBOOK"\n'
        return subprocess.run(
            ["zsh", "-f", "-c", code],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
            env=clean_env(HOME=str(self.home), PATH=f"{self.bin}:/usr/bin:/bin"),
        )

    def test_unreadable_repository_is_not_called_another_repository(self):
        """リポジトリとして読めないときは、その理由を見せる (今回の回帰)。"""
        self.existing_checkout()
        self.fake_git('  *rev-parse*) echo "fatal: not a git repository" >&2; exit 128 ;;')
        result = self.run_step15()
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("別のリポジトリ", out, "git の失敗を「別のリポジトリ」と誤診した")
        self.assertIn("not a git repository", out, "git のメッセージを握り潰した")
        self.assertFalse(
            [c for c in self.git_calls() if "clone" in c or "pull" in c],
            "読めない状態のまま clone / pull を走らせた",
        )

    def test_missing_origin_is_reported_as_missing_origin(self):
        """origin が無いのは「別のリポジトリ」ではない (クローンの残骸など)。"""
        self.existing_checkout()
        self.fake_git('  *"remote get-url"*) exit 2 ;;')
        result = self.run_step15()
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("別のリポジトリ", out)
        self.assertIn("origin", out)

    def test_different_remote_is_reported_with_its_url(self):
        """本当に別のリポジトリなら、そう言う — URL 付きで。"""
        self.existing_checkout()
        self.fake_git(f'  *"remote get-url"*) echo "{self.OTHER_URL}" ;;')
        result = self.run_step15()
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("別のリポジトリ", out)
        self.assertIn(self.OTHER_URL, out)

    def test_matching_remote_pulls(self):
        """正しい checkout なら pull して先へ進む。"""
        self.existing_checkout()
        self.fake_git('  *"remote get-url"*) echo "https://github.com/shinyaoguri/setup.git" ;;')
        result = self.run_step15()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue([c for c in self.git_calls() if "pull" in c], "pull していない")
        self.assertEqual(result.stdout.splitlines()[-1], str(self.setup_dir / "playbook_sillicon_mac.yml"))

    def test_missing_checkout_clones(self):
        """何も無ければクローンする。作りかけは残さない。"""
        self.fake_git()
        result = self.run_step15()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue([c for c in self.git_calls() if "clone" in c], "clone していない")
        self.assertTrue((self.setup_dir / ".git").is_dir(), "クローンしたものが配備先に無い")
        self.assertEqual(self.leftovers(), [], "作りかけが残った")

    def test_interrupted_clone_leaves_nothing_behind(self):
        """中断したクローンは配備先を作らない (issue #269)。

        配備先へ直接クローンしていると半端な `.git` が残り、次の実行は
        `-d "$SETUP_DIR/.git"` が真になって**クローンのやり直しへ戻れない**。
        1 行で新しいマシンを構築することが目的なので、最初の一歩の失敗が
        手作業を要求する状態になってはいけない。
        """
        self.fake_git('  *clone*) mkdir -p "$last/.git"; exit 130 ;;')   # 130 = Ctrl-C
        result = self.run_step15()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue([c for c in self.git_calls() if "clone" in c], "clone していない")
        self.assertFalse(self.setup_dir.exists(), "中断したクローンの残骸が配備先に残った")
        self.assertEqual(self.leftovers(), [], "作りかけが残った")


if __name__ == "__main__":
    unittest.main(verbosity=2)
