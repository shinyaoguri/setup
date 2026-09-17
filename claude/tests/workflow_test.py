#!/usr/bin/env python3
"""CI の workflow そのものの守り。

CI は「文書ルールではなく決定論的に拾う」ための仕組みなので、その CI 自身が
緩んでいると気付けない。とくに次の 2 つは黙って効かなくなる種類 (issue #179):

  - permissions の宣言が無いと、リポジトリの既定 (write) がそのまま job に付く。
    テストを流すだけの job が書き込みトークンを持つ
  - action のバージョンは自分では古びない。dependabot が居ないと、v4 のまま
    v7 まで離れても誰も気付けない

    python3 claude/tests/workflow_test.py
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
DEPENDABOT = REPO / ".github" / "dependabot.yml"


def workflow_files():
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


class LeastPrivilegeTest(unittest.TestCase):
    """既定のトークンは読み取りだけ。書き込みは要る job にだけ宣言する。"""

    def test_every_workflow_declares_permissions(self):
        missing = [
            p.name for p in workflow_files()
            if not re.search(r"^permissions:", p.read_text(), re.M)
        ]
        self.assertEqual(
            missing, [],
            "permissions の宣言が無い (リポジトリの既定 write がそのまま付く)",
        )

    def test_default_permission_is_read_only(self):
        for path in workflow_files():
            body = path.read_text()
            block = re.search(r"^permissions:\n((?:  .*\n)+)", body, re.M)
            with self.subTest(path.name):
                self.assertIsNotNone(block, "permissions の中身が読めない")
                self.assertNotIn(
                    "write", block.group(1),
                    "既定の permissions に write がある (要る job にだけ宣言する)",
                )


class ActionFreshnessTest(unittest.TestCase):
    """action のバージョンは自分では古びない。拾う仕組みが要る。"""

    def test_dependabot_watches_github_actions(self):
        self.assertTrue(DEPENDABOT.exists(), ".github/dependabot.yml が無い")
        body = DEPENDABOT.read_text()
        self.assertIn("package-ecosystem: github-actions", body)

    def test_actions_are_pinned_to_a_major_version(self):
        """裸の @main / @master を使わない (いつ変わるか分からない)。"""
        loose = []
        for path in workflow_files():
            for ref in re.findall(r"uses:\s*(\S+)", path.read_text()):
                if "@" not in ref:
                    loose.append(f"{path.name}: {ref} (バージョン指定が無い)")
                elif ref.split("@")[1] in ("main", "master", "latest"):
                    loose.append(f"{path.name}: {ref}")
        self.assertEqual(loose, [], "追従先が動くバージョン指定になっている")


if __name__ == "__main__":
    unittest.main(verbosity=2)
