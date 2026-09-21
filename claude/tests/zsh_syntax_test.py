#!/usr/bin/env python3
"""zsh スクリプトの構文検査。

CI の shellcheck は zsh に対応していないので、setup.zsh・sillicon_mac_setup.zsh・
zshenv・zshrc・zprofile には構文検査すら当たっていなかった (issue #217)。構文エラーは
実機で流すまで分からず、とくに **zshenv の構文エラーはすべての zsh の起動に効く**
(Claude の Bash ツールのシェルを含む)。

workflow に step を足すのではなくテストにしてあるのは、必須チェック (claude-scripts) の
中で流れ、手元の `python3 -m unittest discover` でも同じものが流れるようにするため。

`zsh -n` が拾うのは構文エラーだけ。読み取り専用変数への代入 (#195) や getopts の
取りこぼし (#194) のような実行時の誤りは拾えないので、そちらは実際に流すテストが見る
(setup_bootstrap_test.py / bootstrap_selection_test.py)。

    python3 claude/tests/zsh_syntax_test.py
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent

# 拡張子を持たない rc ファイル。~/.<名前> として symlink される (tasks/zshrc.yml)
RC_FILES = ("zshenv", "zprofile", "zshrc")


def zsh_files():
    """リポジトリ直下の zsh スクリプト。*.zsh と rc ファイルと、zsh の shebang を持つもの。"""
    found = set(REPO.glob("*.zsh"))
    found |= {REPO / name for name in RC_FILES if (REPO / name).exists()}
    for path in REPO.iterdir():
        if path.is_file() and not path.is_symlink():
            try:
                first = path.open(errors="replace").readline()
            except OSError:
                continue
            if first.startswith("#!") and "zsh" in first:
                found.add(path)
    return sorted(found)


def syntax_check(path):
    return subprocess.run(
        ["zsh", "-f", "-n", str(path)],
        capture_output=True, text=True, env=clean_env(), timeout=30,
    )


class ZshSyntaxTest(unittest.TestCase):
    def test_targets_are_found(self):
        """対象が 0 本で素通りしない。名指しで在るはずのものも確かめる。"""
        names = {p.name for p in zsh_files()}
        for expected in ("setup.zsh", "sillicon_mac_setup.zsh", *RC_FILES):
            self.assertIn(expected, names)

    def test_every_zsh_file_parses(self):
        for path in zsh_files():
            with self.subTest(path.name):
                result = syntax_check(path)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_check_actually_detects_a_syntax_error(self):
        """対照。`zsh -n` が壊れたスクリプトを落とせていなければ、上は何も確かめていない。"""
        with tempfile.NamedTemporaryFile("w", suffix=".zsh") as broken:
            broken.write('if [[ -n "$x" ]]; then\n  echo unterminated\n')
            broken.flush()
            self.assertNotEqual(syntax_check(Path(broken.name)).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
