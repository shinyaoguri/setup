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


class DeclaredChecksActuallyRunTest(unittest.TestCase):
    """設定ファイルを置いただけで流していない検査を作らない。

    .ansible-lint は profile: production を宣言していたのに流す口がどこにも無く、
    実際に流すと 8 件で落ちた。宣言と実際の検査が食い違うと、守れているつもりの
    ものが守れていない (issue #179)。
    """

    def test_ansible_lint_config_has_a_runner(self):
        self.assertTrue((REPO / ".ansible-lint").exists(), ".ansible-lint が無い")
        ran = any("ansible-lint" in p.read_text() for p in workflow_files())
        self.assertTrue(ran, ".ansible-lint を流す job が CI に無い")

    def test_shellcheck_has_a_runner(self):
        """bash スクリプトは手元で流すしかなく、指摘が溜まっても気付けなかった。"""
        bodies = "\n".join(p.read_text() for p in workflow_files())
        self.assertIn("shellcheck", bodies, "shellcheck を流す job が CI に無い")

    def test_shellcheck_targets_are_discovered(self):
        """対象を一覧で並べない。足した人が忘れた瞬間に検査から外れる。"""
        bodies = "\n".join(p.read_text() for p in workflow_files())
        if "shellcheck" not in bodies:
            self.skipTest("shellcheck の job が無い")
        self.assertIn("find claude bin", bodies, "対象を find で拾っていない")

    def test_shellcheck_version_is_pinned(self):
        """手元と CI で版が違うと「手元で通るのに CI で落ちる」が起きる。

        実際にこの job を足したとき、手元 (0.11.0) では -S style でも 0 件なのに
        CI (apt の 0.9.0) だけ SC2015 で落ちた。
        """
        bodies = "\n".join(p.read_text() for p in workflow_files())
        if "shellcheck" not in bodies:
            self.skipTest("shellcheck の job が無い")
        self.assertNotIn(
            "apt-get install -y shellcheck", bodies,
            "apt の版は Ubuntu のリリースに紐づくので手元と食い違う",
        )
        self.assertRegex(
            bodies, r"SHELLCHECK_VERSION:\s*v\d+\.\d+\.\d+",
            "shellcheck の版が固定されていない",
        )

    def test_the_linter_gets_the_collections_it_needs(self):
        """osx_defaults / homebrew_cask は community.general にある。

        入れないと syntax-check[unknown-module] で落ち、本当の指摘が埋もれる。
        """
        bodies = "\n".join(p.read_text() for p in workflow_files())
        if "ansible-lint" not in bodies:
            self.skipTest("ansible-lint の job が無い")
        self.assertIn("community.general", bodies)


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
