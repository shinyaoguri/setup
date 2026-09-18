#!/usr/bin/env python3
"""すぐに要らないものを選ばせる仕組みの不変条件 (issue #191)。

対話そのものは自動で検証しにくいので、**壊れると困るところ**に絞って見る:

  - 一覧の抽出が新しいキー (required / optional) から正しく読めること
  - **非対話で止まらないこと** — CI や無人実行で fzf を出すと、待ち続けて終わらない
  - required に置いたものが optional へ落ちていないこと (落ちると playbook が壊れる)

    python3 claude/tests/bootstrap_selection_test.py
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "sillicon_mac_setup.zsh"
PACKAGES = REPO / "vars" / "packages.yml"

# スクリプト本体と同じ awk を使う。ここで別の実装を書くと、片方だけ直したときに
# 「テストは緑なのに本番は読めない」が起きる
YAML_LIST = r"""
awk -v key="$1" '
    $0 ~ "^" key ":" { flag=1; next }
    /^[^ #]/         { flag=0 }
    flag && /^[[:space:]]*-[[:space:]]/ {
        sub(/^[[:space:]]*-[[:space:]]*/,"")
        print
    }
' "$2"
"""


def yaml_list(key):
    r = subprocess.run(
        ["zsh", "-c", YAML_LIST, "zsh", key, str(PACKAGES)],
        capture_output=True, text=True, check=True,
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
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(
            ["zsh", "-c", body[start:end] + '\nprintf "%s" "$OPTIONAL_MODE"', "zsh", *args],
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
