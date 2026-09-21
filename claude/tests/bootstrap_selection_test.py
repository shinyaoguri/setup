#!/usr/bin/env python3
"""すぐに要らないものを選ばせる仕組みの不変条件 (issue #191)。

対話そのものは自動で検証しにくいので、**壊れると困るところ**に絞って見る:

  - 一覧の抽出が新しいキー (required / optional) から正しく読めること
  - **非対話で止まらないこと** — CI や無人実行で fzf を出すと、待ち続けて終わらない
  - required に置いたものが optional へ落ちていないこと (落ちると playbook が壊れる)
  - **選んでいないものが入らないこと** — ここだけは本物の fzf を pty 上で動かして見る
    (issue #267)

    python3 claude/tests/bootstrap_selection_test.py
"""

import fcntl
import os
import pty
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "sillicon_mac_setup.zsh"
PACKAGES = REPO / "vars" / "packages.yml"

def script_function(name):
    """本体から関数の定義を切り出す (`name() {` から、行頭の `}` まで)。

    定義行の後ろにコメント (`f() {  # $1=...`) が付く関数もあるので、改行までは読み飛ばす。
    """
    match = re.search(rf"^{name}\(\) \{{[^\n]*\n.*?^\}}\n", SCRIPT.read_text(), re.M | re.S)
    if match is None:
        raise AssertionError(f"{SCRIPT.name} に {name}() が見つからない")
    return match.group(0)


def yaml_list(key):
    """**本体の yaml_list をそのまま流す。** 以前はここに awk の写しを持っていた。写しは
    本体だけを直したときに「テストは緑なのに本番は読めない」をそのまま起こす (#218)。
    """
    r = subprocess.run(
        ["zsh", "-f", "-c", script_function("yaml_list") + '\nyaml_list "$1"', "zsh", key],
        capture_output=True, text=True, check=True,
        env=clean_env(PACKAGES_YAML=str(PACKAGES)),
    )
    return [l for l in r.stdout.split("\n") if l]


class ListExtractionTest(unittest.TestCase):
    """スクリプトが読む形で一覧が取れること。"""

    def test_required_and_optional_are_both_present(self):
        for key in (
            "homebrew_packages_required",
            "homebrew_packages_optional",
            "homebrew_cask_packages_required",
            "homebrew_cask_packages_optional",
            "font_casks",
        ):
            with self.subTest(key):
                self.assertTrue(yaml_list(key), f"{key} が読めない")

    def test_old_flat_keys_are_gone(self):
        """旧キーが残っていると、どちらを読んでいるか分からなくなる。"""
        body = PACKAGES.read_text()
        for old in ("\nhomebrew_packages:", "\nhomebrew_cask_packages:", "\nappstore_apps:"):
            with self.subTest(old.strip()):
                self.assertNotIn(old, body, f"旧キー {old.strip()} が残っている")

    def test_infrastructure_stays_required(self):
        """playbook 自身か鍵まわりの前提は optional へ落とさない。

        落ちると、選ばなかったマシンで Step 6 が実行ファイル不在で失敗する。
        """
        formulae = yaml_list("homebrew_packages_required")
        casks = yaml_list("homebrew_cask_packages_required")
        for name, where in (
            ("git", formulae), ("mas", formulae), ("fzf", formulae), ("gum", formulae),
            ("claude-code", casks), ("secretive", casks),
            ("1password", casks), ("1password-cli", casks),
        ):
            with self.subTest(name):
                self.assertIn(name, where, f"{name} が required から外れている")

    def test_heavy_apps_are_optional(self):
        """実測で重いものが必須側に戻っていないこと (Office 2.7GB / ScanSnap 1.1GB)。"""
        optional = yaml_list("homebrew_cask_packages_optional")
        for name in ("microsoft-office", "fujitsu-scansnap-home"):
            with self.subTest(name):
                self.assertIn(name, optional)

    def test_appstore_has_no_required_side(self):
        """App Store に必須は無い (RunCat の seed はカードを書くだけで失敗しない)。"""
        body = PACKAGES.read_text()
        self.assertNotIn("appstore_apps_required:", body)
        self.assertIn("appstore_apps_optional:", body)


class NonInteractiveTest(unittest.TestCase):
    """端末が無いときに選択を出さないこと。

    CI や無人実行で fzf を出すと入力が来ず、セッションが終わるまで待ち続ける。
    """

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "fzf-was-called"
        # 呼ばれたら痕跡を残す偽 fzf。止まらないよう即座に終わる
        fzf = self.bin / "fzf"
        fzf.write_text(f'#!/bin/sh\ntouch "{self.trace}"\nexit 130\n')
        fzf.chmod(fzf.stat().st_mode | stat.S_IEXEC)

    def run_option_logic(self, *args):
        """スクリプト冒頭と同じ判定を、同じ書き方で走らせる。"""
        body = SCRIPT.read_text()
        start = body.index("OPTIONAL_MODE=ask")
        end = body.index('echo "============================================================"')
        env = clean_env(PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(
            ["zsh", "-f", "-c", body[start:end] + '\nprintf "%s" "$OPTIONAL_MODE"', "zsh", *args],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=30,
        )

    def test_falls_back_to_none_without_a_terminal(self):
        result = self.run_option_logic()
        self.assertEqual(result.stdout, "none", result.stderr)
        self.assertFalse(self.trace.exists(), "非対話なのに fzf を呼んだ")

    def test_with_optional_installs_everything(self):
        self.assertEqual(self.run_option_logic("--with-optional").stdout, "all")

    def test_no_optional_skips(self):
        self.assertEqual(self.run_option_logic("--no-optional").stdout, "none")

    def test_flags_are_documented_in_usage(self):
        body = SCRIPT.read_text()
        self.assertIn("--with-optional", body)
        self.assertIn("--no-optional", body)


class PreviewQuotingTest(unittest.TestCase):
    """fzf は {N} を**シェル引用して**渡す (issue #191)。

    文字列の中へ埋めると `App Store ID: '524141863'` のように引用符ごと表示される。
    preview は「何なのか分からないものを確かめる」ための窓なので、そこに実装の都合が
    漏れると読む側の負担になる。printf の引数として渡せば引用は剥がれる。
    """

    def preview_command(self, marker):
        """スクリプトから preview コマンド (fzf_select の第 2 引数) を取り出す。"""
        for line in SCRIPT.read_text().split("\n"):
            if marker in line and not line.lstrip().startswith("#"):
                m = re.search(r"'([^']*" + re.escape(marker) + r"[^']*)'", line)
                self.assertIsNotNone(m, f"preview コマンドを読み取れない: {line}")
                return m.group(1)
        self.fail(f"{marker} を含む preview コマンドが無い")

    def test_appstore_preview_shows_no_quotes(self):
        cmd = self.preview_command("App Store ID")
        # fzf がするのと同じ引用をして実行する
        cmd = cmd.replace("{2..}", "'RunCatNeo'").replace("{1}", "'6757801838'")
        out = subprocess.run(
            ["zsh", "-f", "-c", cmd], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("RunCatNeo", out)
        self.assertIn("6757801838", out)
        self.assertNotIn(
            "'", out, "fzf の引用符が preview に漏れている (printf の引数へ渡す)"
        )


class FnmGuardTest(unittest.TestCase):
    """fnm は optional になったので、無いマシンがある (issue #191)。"""

    def test_fnm_task_is_guarded(self):
        body = (REPO / "tasks" / "fnm.yml").read_text()
        self.assertIn("command -v fnm", body, "fnm の存在を見ていない")
        self.assertIn(
            "check_mode: false", body,
            "--check で判定タスクが skip され、下の when が rc を引けなくなる",
        )
        self.assertIn("fnm_present.rc | default(1) == 0", body)

    @unittest.skipUnless(sys.platform == "darwin", "macOS の同梱物を見る検査")
    def test_the_probe_works_without_a_shell(self):
        """`ansible.builtin.command` はシェルを介さず argv を exec する。

        `command` は本来シェル組み込みで、これが動くのは **macOS が
        /usr/bin/command を実行ファイルとして同梱している**から。無くなれば
        存在確認が rc != 0 を返し続け、「fnm が入っているのに永久に skip」へ倒れる
        — しかも `failed_when: false` なのでタスクは緑のままになる。
        """
        present = subprocess.run(
            ["command", "-v", "zsh"], capture_output=True, text=True
        )
        self.assertEqual(
            present.returncode, 0,
            "シェル無しで `command -v` が使えない (tasks/fnm.yml の判定が常に偽になる)",
        )
        self.assertTrue(present.stdout.strip(), "パスを返していない")

        absent = subprocess.run(
            ["command", "-v", "definitely-not-an-installed-program"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(
            absent.returncode, 0, "不在を rc で伝えていない (存在確認にならない)"
        )


class CaskInstallTest(unittest.TestCase):
    """cask を 1 本入れる関数の振る舞い (issue #199)。

    手で入れたアプリがあるマシンでは、`brew list --cask` が偽を返し (brew の管理下に
    無い)、素の `brew install --cask` は "It seems there is already an App at …" で
    失敗する。スクリプトは set -e なので、その 1 本で setup 全体が終わっていた。
    required の 4 本 (1Password・Secretive・Claude Code) はどれも手で入れがちなもので、
    新品でないマシンへ setup を流すと踏む。
    """

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "brew.log"
        # 偽 brew。引数を控え、名前が broken-* の cask だけ失敗する
        brew = self.bin / "brew"
        brew.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.log}"\n'
            'for a in "$@"; do case "$a" in broken-*) exit 1 ;; esac; done\n'
            "exit 0\n"
        )
        brew.chmod(brew.stat().st_mode | stat.S_IEXEC)

    def run_installs(self, *pairs):
        """本体から install_cask を切り出し、本体と同じ set -e の下で順に呼ぶ。"""
        body = SCRIPT.read_text()
        start = body.index("CASK_FAILED_REQUIRED=()")
        end = body.index("# --- cask の導入ここまで")
        calls = "\n".join(f"install_cask {name} {kind}" for name, kind in pairs)
        report = (
            'printf "required=%s\\n" "${CASK_FAILED_REQUIRED[*]}"\n'
            'printf "optional=%s\\n" "${CASK_FAILED_OPTIONAL[*]}"\n'
        )
        env = clean_env(PATH=f"{self.bin}:/usr/bin:/bin")
        return subprocess.run(
            # -f: ~/.zshenv を読ませない。読むと /opt/homebrew/bin が偽 brew の前へ入り、
            # **本物の brew を呼んでしまう** (このテストを書いているときに実際に踏んだ)
            ["zsh", "-f", "-c", "set -e\n" + body[start:end] + "\n" + calls + "\n" + report],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=30,
        )

    def test_existing_apps_are_adopted(self):
        """既に在るアプリは brew の管理下へ取り込む (--adopt)。"""
        self.run_installs(("secretive", "required"))
        self.assertIn("install --cask --adopt secretive", self.log.read_text())

    def test_one_failure_does_not_stop_the_rest(self):
        """1 本の失敗で setup 全体を止めない。後ろの cask も入れ、失敗は控える。"""
        result = self.run_installs(
            ("broken-required", "required"),
            ("broken-optional", "optional"),
            ("after-the-failures", "optional"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("after-the-failures", self.log.read_text(), "失敗の後ろが実行されていない")
        self.assertIn("required=broken-required\n", result.stdout)
        self.assertIn("optional=broken-optional\n", result.stdout)

    def test_success_is_not_recorded_as_failure(self):
        result = self.run_installs(("fine", "required"))
        self.assertIn("required=\n", result.stdout)

    def test_every_cask_install_goes_through_the_function(self):
        """素の `brew install --cask` を残さない (そこだけ set -e で止まる形に戻る)。"""
        body = SCRIPT.read_text()
        start = body.index("CASK_FAILED_REQUIRED=()")
        end = body.index("# --- cask の導入ここまで")
        outside = body[:start] + body[end:]
        bare = [
            line.strip() for line in outside.splitlines()
            if "brew install --cask" in line and not line.lstrip().startswith(("#", "echo"))
        ]
        self.assertEqual(bare, [])

    def test_required_failure_makes_the_script_exit_nonzero_at_the_end(self):
        """必須が入らなかったら、最後まで進めたうえで失敗として終わる。

        途中で止めないことと、成功として終わらせないことは別。必須が欠けたまま
        緑で終わると、鍵まわりや playbook の前提が無いことに気付けない。
        """
        body = SCRIPT.read_text()
        tail = body[body.index("セットアップが完了しました"):]
        self.assertRegex(tail, r"CASK_FAILED_REQUIRED\[@\]\} > 0[^\n]*\n(?:.*\n)*?\s*exit 1")


class OptionalFormulaTest(unittest.TestCase):
    """optional の formula も、選べて入る (issue #240)。

    #191 で「optional は選ばせる」形にしたとき、formula の選択が抜けた。playbook が
    入れるのは required だけ、fzf が出すのは cask と App Store だけで、
    `homebrew_packages_optional` (vim / neovim / fnm / direnv) は**どこからも読まれて
    いなかった**。tasks/fnm.yml は「fnm が無ければ skip」なので、新しいマシンでは
    Node.js が黙って入らない。
    """

    def setUp(self):
        self.body = SCRIPT.read_text()

    def test_every_optional_key_is_read_by_the_script(self):
        """宣言だけで読み手の居ないキーを作らない。"""
        packages = (REPO / "vars" / "packages.yml").read_text()
        keys = re.findall(r"^([a-z_]+_optional):", packages, re.M)
        self.assertGreaterEqual(len(keys), 3, f"optional のキーを読み取れていない: {keys}")
        unread = [
            key for key in keys
            if not re.search(rf"yaml_(?:list|appstore)\s+{key}\b", self.body)
        ]
        self.assertEqual(unread, [], "宣言されているのに、選択肢へ出す読み手が居ない")

    def test_formulae_are_offered_before_the_playbook_runs(self):
        """fnm は tasks/fnm.yml の前に入っていないと、その回の Node.js が skip される。"""
        offered = self.body.index("yaml_list homebrew_packages_optional")
        playbook = self.body.index('ansible-playbook -i "localhost,"')
        self.assertLess(offered, playbook)

    def run_installs(self, *names):
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        bin_dir = Path(workdir.name) / "bin"
        bin_dir.mkdir()
        self.log = Path(workdir.name) / "brew.log"
        brew = bin_dir / "brew"
        brew.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.log}"\n'
            'for a in "$@"; do case "$a" in broken-*) exit 1 ;; esac; done\n'
            "exit 0\n"
        )
        brew.chmod(brew.stat().st_mode | stat.S_IEXEC)
        start = self.body.index("FORMULA_FAILED=()")
        end = self.body.index("# --- formula の導入ここまで")
        calls = "\n".join(f"install_formula {name}" for name in names)
        return subprocess.run(
            ["zsh", "-f", "-c", "set -e\n" + self.body[start:end] + "\n" + calls
             + '\nprintf "failed=%s\\n" "${FORMULA_FAILED[*]}"\n'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
            env=clean_env(PATH=f"{bin_dir}:/usr/bin:/bin"),
        )

    def test_one_failure_does_not_stop_the_rest(self):
        """cask と同じ扱い。1 本の失敗で setup 全体を止めず、控えて続ける (#199)。"""
        result = self.run_installs("broken-formula", "fnm")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("install fnm", self.log.read_text(), "失敗の後ろが実行されていない")
        self.assertIn("failed=broken-formula\n", result.stdout)

    def test_failures_are_reported_at_the_end(self):
        tail = self.body[self.body.index("セットアップが完了しました"):]
        self.assertIn("FORMULA_FAILED", tail)



FZF = shutil.which("fzf")

# pty へ送るキー。矢印のエスケープ列は端末の設定に左右されるので control キーで送る
ENTER = b"\r"
TAB = b"\t"       # fzf では toggle+down (マークして次へ)
DOWN = b"\x0e"    # Ctrl-N


class SelectionDecisionTest(unittest.TestCase):
    """**選んでいないものを入れないこと** (issue #267)。

    fzf は `--multi` でも、マークが 0 件のまま Enter を押すと**カーソル位置の 1 件**を
    返す。header は `Tab で選択` と書いているので、チェックボックスのつもりで Enter を
    押した人に意図しないアプリが入っていた。重いものを選択式にした #191 の趣旨を
    直接壊すので、ここは文字列の検査ではなく**本物の fzf を pty 上で動かして**見る。
    """

    def setUp(self):
        if FZF is None:
            # CI では .github/workflows/test.yml が fzf を入れる。入れ忘れたまま
            # skip され続けると、この検査は在るだけで何も見ていないことになる
            if os.environ.get("CI"):
                self.fail("fzf が無い (.github/workflows/test.yml で入れているか確かめる)")
            self.skipTest("fzf が無いので実挙動は見られない (brew install fzf)")
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)

    def select(self, keys, items=("alpha", "bravo", "charlie")):
        """本体の fzf_select() を、本物の fzf ごと pty 上で動かして選ばれたものを返す。"""
        chosen = Path(self.workdir.name) / "chosen"
        script = (
            script_function("fzf_select")
            + f'\nprintf "%s\\n" "$@" | fzf_select "任意のアプリ" "echo {{1}}" right:30%:wrap'
            + f' > {chosen}\n'
        )
        env = clean_env(TERM="xterm-256color")
        pid, fd = pty.fork()
        if pid == 0:  # 子: pty を端末として fzf を出す
            try:
                os.execve("/bin/zsh", ["zsh", "-f", "-c", script, "zsh", *items], env)
            finally:
                os._exit(127)
        # 端末の大きさを教える (0x0 のままだと --height の計算が成り立たない)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        try:
            self.wait_until_drawn(fd)
            for key in keys:
                os.write(fd, key)
                time.sleep(0.3)
            self.drain(fd, pid)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        return [line for line in chosen.read_text().split("\n") if line]

    def wait_until_drawn(self, fd, timeout=20):
        """header が描かれるまで待つ。描き終わる前にキーを送ると取りこぼす。"""
        needle = "Tab で選択".encode()
        seen = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            seen += self.read_available(fd)
            if needle in seen:
                return
            time.sleep(0.1)
        self.fail(f"fzf が描画しなかった: {seen[-500:]!r}")

    def drain(self, fd, pid, timeout=20):
        """終了まで読み続ける (読まないと pty が詰まって子が止まる)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                return
            self.read_available(fd)
            time.sleep(0.1)
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        self.fail("fzf が終わらなかった")

    @staticmethod
    def read_available(fd):
        try:
            return os.read(fd, 65536)
        except OSError:  # 子が終わると EIO
            return b""

    def test_enter_without_marks_takes_nothing(self):
        """何もマークせず Enter — ここが報告された不具合そのもの。"""
        self.assertEqual(self.select([ENTER]), [])

    def test_moving_the_cursor_is_not_a_selection(self):
        """カーソルを動かしただけでは選んだことにならない。"""
        self.assertEqual(self.select([DOWN, ENTER]), [])

    def test_marked_item_comes_back(self):
        """Tab でマークしたものは、これまでどおり返る。"""
        self.assertEqual(self.select([DOWN, TAB, ENTER]), ["bravo"])

    def test_several_marks_come_back_in_order(self):
        self.assertEqual(self.select([TAB, TAB, ENTER]), ["alpha", "bravo"])


class SelectionGuardTest(unittest.TestCase):
    """ガードの掛け方 (fzf が無い環境でも見られるところ)。"""

    def fzf_select_body(self):
        return script_function("fzf_select")

    def test_only_fzf_select_runs_fzf(self):
        """生の fzf 呼び出しは 1 か所だけ。

        2 か所へ写すと、片方だけにガードが付いた状態が作れてしまう
        (App Store の選択が実際にその形で写しになっていた)。
        """
        body = self.fzf_select_body()
        callers = [
            line for line in SCRIPT.read_text().split("\n")
            if re.search(r"(?:^|\||\s)fzf\s+-", line) and not line.lstrip().startswith("#")
        ]
        self.assertTrue(callers, "fzf の呼び出しが見つからない")
        for line in callers:
            with self.subTest(line.strip()):
                self.assertIn(line, body, "fzf_select() の外から fzf を呼んでいる")

    def transform_decision(self, select_count):
        """ガードの式だけを取り出し、fzf がするのと同じ sh で評価する。"""
        match = re.search(r"--bind 'enter:transform:(.*)'", self.fzf_select_body())
        self.assertIsNotNone(match, "enter のガードが無い")
        env = clean_env(FZF_SELECT_COUNT=select_count)
        return subprocess.run(
            ["/bin/sh", "-c", match.group(1)], capture_output=True, text=True,
            check=True, timeout=10, env=env,
        ).stdout.strip()

    def test_no_selection_aborts(self):
        self.assertEqual(self.transform_decision("0"), "abort")

    def test_selection_accepts(self):
        self.assertEqual(self.transform_decision("2"), "accept")

    def test_old_fzf_without_the_variable_keeps_working(self):
        """FZF_SELECT_COUNT は fzf 0.52 以降。持たない版では Enter を殺さない。"""
        self.assertEqual(self.transform_decision(None), "accept")


if __name__ == "__main__":
    unittest.main(verbosity=2)
