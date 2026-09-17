#!/usr/bin/env python3
"""tasks/*.yml のうち、判定を間違えると黙って何もしなくなるものを固定する。

ansible のタスクは「実行されなかった」と「実行して変更が無かった」が結果として
同じ緑に見える。when の条件が永久に偽になっていても気付けないので、条件の根拠に
している外部コマンドの挙動ごと検証する (issue #172)。

    python3 claude/tests/ansible_tasks_test.py
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent.parent / "tasks"

ABSENT_PKG = "com.example.definitely.not.installed"


class RosettaDetectionTest(unittest.TestCase):
    """Rosetta の存在チェック。誤ると新しいマシンに Rosetta が入らない。"""

    def setUp(self):
        raw = (TASKS / "rosetta.yml").read_text()
        # コメントは「なぜその形にしたか」を書くのに悪い形を引用するので、検査から外す
        self.body = "\n".join(
            line for line in raw.split("\n") if not line.lstrip().startswith("#")
        )

    def test_uses_a_form_that_reports_absence(self):
        """`pkgutil --pkgs <id>` はスペース区切りだと引数を無視して必ず rc=0 を返す。

        この形で書くと when が永久に偽になり、インストールが一度も走らないまま
        タスクは緑で通る。
        """
        self.assertNotRegex(
            self.body, r"pkgutil\s+--pkgs\s+[^=\s]",
            "pkgutil --pkgs <id> はスペース区切りだと常に rc=0 (存在確認にならない)",
        )
        self.assertRegex(self.body, r"pkgutil\s+--pkg-info\s+com\.apple\.pkg\.")

    def test_install_is_guarded_by_the_check(self):
        """チェック結果を実際に使っていること (register だけして見ない形を防ぐ)。"""
        self.assertIn("rosetta_check.rc != 0", self.body)

    @unittest.skipUnless(shutil.which("pkgutil"), "pkgutil が無い環境")
    def test_pkgutil_forms_behave_as_assumed(self):
        """前提にしている pkgutil の挙動そのもの。OS 側が変われば赤くなる。"""
        spaced = subprocess.run(
            ["pkgutil", "--pkgs", ABSENT_PKG], capture_output=True, text=True
        )
        self.assertEqual(
            spaced.returncode, 0,
            "--pkgs <id> が存在確認として働くようになった (タスクを見直せる)",
        )

        info = subprocess.run(
            ["pkgutil", "--pkg-info", ABSENT_PKG], capture_output=True, text=True
        )
        self.assertNotEqual(
            info.returncode, 0, "--pkg-info が不在を rc で伝えなくなった"
        )


class TaskTagNamingTest(unittest.TestCase):
    """CLAUDE.md の「tag 名はファイル名と同じ」を守る。

    ずれていると `--tags <ファイル名>` が 0 タスクで黙って成功する。
    """

    def test_every_task_file_is_imported_with_a_matching_tag(self):
        playbook = (TASKS.parent / "playbook_sillicon_mac.yml").read_text()
        imports = re.findall(
            r"tags:\s*(\S+)\s*\n\s*ansible\.builtin\.import_tasks:\s*tasks/(\S+)\.yml",
            playbook,
        )
        self.assertTrue(imports, "playbook から import を読み取れなかった")
        mismatched = [(tag, name) for tag, name in imports if tag != name]
        self.assertEqual(
            mismatched, [],
            "tag 名とファイル名がずれている (--tags <ファイル名> が 0 タスクで成功する)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
