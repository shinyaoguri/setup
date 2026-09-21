#!/usr/bin/env python3
"""zshrc のテスト。

~/.zshrc はこのリポジトリへの symlink なので、**インストーラが ~/.zshrc へ追記した行は
そのままリポジトリの変更になる**。そのマシンにしか無い道具の設定が紛れ込むと、他の
マシンではシェルを開くたびにエラーが出る (issue #198 — Unity CLI の
`. "$HOME/.unity/env"` が存在チェック無しで入っていた)。

マシン固有の設定の置き場は ~/.zshrc.local。ここでは 2 点を固定する:

  - 何も入っていないマシンで読んでも、エラーを出さないこと (実際に source して見る)
  - ~/.zshrc.local が在れば読むこと

    python3 claude/tests/zshrc_test.py
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent
ZSHRC = REPO / "zshrc"


class FreshMachineTest(unittest.TestCase):
    """道具が何も入っていない HOME で zshrc を読む。"""

    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.home = Path(self.workdir.name)
        # oh-my-zsh だけは tasks/zshrc.yml が必ず入れるので、在る前提でよい (中身は空で足りる)
        omz = self.home / ".oh-my-zsh"
        omz.mkdir()
        (omz / "oh-my-zsh.sh").write_text("")

    def source_zshrc(self):
        # PATH を絞るのは、手元に入っている fnm / direnv を「入っていない」状態にするため
        env = clean_env(HOME=str(self.home), PATH="/usr/bin:/bin", ZDOTDIR=None)
        return subprocess.run(
            ["zsh", "-f", "-c", f'source "{ZSHRC}"'],
            env=env, capture_output=True, text=True, cwd=self.home, timeout=30,
        )

    def test_sourcing_on_a_fresh_machine_is_silent(self):
        result = self.source_zshrc()
        self.assertEqual(result.stderr, "", "何も入っていないマシンで zshrc がエラーを出す")
        self.assertEqual(result.returncode, 0)

    def test_machine_local_file_is_sourced_when_present(self):
        marker = self.home / "LOCAL_WAS_SOURCED"
        (self.home / ".zshrc.local").write_text(f'touch "{marker}"\n')
        result = self.source_zshrc()
        self.assertEqual(result.stderr, "")
        self.assertTrue(marker.exists(), "~/.zshrc.local が読まれていない")


class NoInstallerResidueTest(unittest.TestCase):
    """インストーラの追記がリポジトリに残っていないこと。

    上の実行テストは「存在チェック付きで追記された行」を通してしまう。エラーは出ないが、
    そのマシンにしか無い道具の設定が全マシンへ配られることに変わりはない。
    """

    def test_no_installer_marker_blocks(self):
        markers = re.findall(r"^# *(?:>>>|<<<).*$", ZSHRC.read_text(), re.M)
        self.assertEqual(
            markers, [],
            "インストーラが追記したブロックが zshrc に残っている (~/.zshrc.local へ移す)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
