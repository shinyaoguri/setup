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

    def test_actions_are_pinned_to_a_commit(self):
        """action は commit の SHA で固定し、版はコメントで添える (issue #219)。

        `@v7` は可動タグで、上流が付け替えれば同じ指定のまま別のコードが走る。job の
        トークンと checkout した中身に触れるコードなので、走るものを commit で確定させる。
        dependabot は SHA 固定でもコメントの版を読んで追従する。
        """
        loose = []
        for path in workflow_files():
            for line in path.read_text().splitlines():
                match = re.match(r"\s*-?\s*uses:\s*(\S+)(.*)", line)
                if not match:
                    continue
                ref, rest = match.groups()
                if ref.startswith("./"):
                    continue   # リポジトリ内の action は同じ commit で走る
                if not re.search(r"@[0-9a-f]{40}$", ref):
                    loose.append(f"{path.name}: {ref} (commit の SHA で固定されていない)")
                elif not re.search(r"#\s*v\d+\.\d+\.\d+", rest):
                    loose.append(f"{path.name}: {ref} (版のコメントが無い — 何の版か読めない)")
        self.assertEqual(loose, [], "追従先が動くバージョン指定になっている")

    def test_uses_lines_are_found(self):
        """対照。uses を 1 つも読めていなければ、上は何も確かめていない。"""
        count = sum(
            len(re.findall(r"^\s*-?\s*uses:", path.read_text(), re.M)) for path in workflow_files()
        )
        self.assertGreaterEqual(count, 4)


class DownloadedToolsAreVerifiedTest(unittest.TestCase):
    """CI が外から取ってくる道具は、版と中身を固定する (issue #219)。"""

    def setUp(self):
        self.body = code_lines(WORKFLOWS / "test.yml")

    def test_shellcheck_tarball_is_checked_against_a_known_hash(self):
        """取得した tarball を、展開する前に既知の sha256 と突き合わせる。

        curl の出力をそのまま tar へ流す形だと、配布物が差し替わっても気付けない。
        """
        self.assertRegex(self.body, r"SHELLCHECK_SHA256:\s*[0-9a-f]{64}")
        self.assertRegex(self.body, r"sha256sum\s+(-c|--check)")
        self.assertNotRegex(
            self.body, r"curl[^\n]*\\?\n?[^\n]*\|\s*tar",
            "検証の前に展開している (curl の出力を tar へ直接流している)",
        )

    def test_ansible_lint_version_is_pinned(self):
        """shellcheck と同じ理由 — 版が上がった日に、無関係な PR が赤くなる。"""
        self.assertRegex(self.body, r"pip install ['\"]?ansible-lint==\d+\.\d+")

def code_lines(path):
    """コメント行を落とした本文。コメントは機構の名前を引用するので、素で探すと常に当たる。"""
    return "\n".join(
        line for line in path.read_text().split("\n")
        if not line.lstrip().startswith("#")
    )


class UpstreamDetectionTest(unittest.TestCase):
    """Claude 本体の更新の検知が、置いただけで動いていない状態にならないこと (#232)。

    差分スクリプトと台帳が在っても、定期的に流す口が無ければ見直しは「思い出したとき」に
    戻る。逆に口だけ在ってスクリプトを呼んでいなければ、緑のまま何も検知しない。
    """

    WORKFLOW = WORKFLOWS / "claude-upstream.yml"
    SCRIPT = REPO / ".github" / "scripts" / "claude-changelog-diff.py"

    def setUp(self):
        self.assertTrue(self.WORKFLOW.exists(), "claude-upstream.yml が無い")
        self.code = code_lines(self.WORKFLOW)

    def test_runs_on_a_schedule_and_by_hand(self):
        self.assertRegex(self.code, r"(?m)^  schedule:\n\s+- cron:", "定期実行の口が無い")
        self.assertRegex(self.code, r"(?m)^  workflow_dispatch:", "手で流して確かめる口が無い")

    def test_calls_the_script_with_the_real_ledger(self):
        self.assertTrue(self.SCRIPT.exists())
        self.assertIn(".github/scripts/claude-changelog-diff.py", self.code)
        self.assertIn("--intents claude/intents.json", self.code)

    def test_issue_is_gated_on_the_script_verdict(self):
        """起票の要否を決めるのはスクリプト。workflow 側で条件を足したり外したりしない。"""
        self.assertIn("steps.diff.outputs.issue_needed == 'true'", self.code)

    def test_one_issue_is_kept_by_exact_title(self):
        """版ごとに起票したりコメントで追記したりすると、累積の差分が重複して積もる。"""
        self.assertIn("select(.title == env.ISSUE_TITLE)", self.code)
        self.assertIn("gh issue edit", self.code)
        self.assertNotIn("gh issue comment", self.code)

    def test_write_permission_is_on_the_job_only(self):
        self.assertRegex(self.code, r"(?m)^    permissions:\n(?:      .*\n)*      issues: write")

    def test_fetch_failure_is_not_silenced(self):
        """curl が落ちたら step ごと赤くなるのではなく、読めない changelog としてスクリプトへ渡す。"""
        self.assertRegex(self.code, r'curl -fsSL[^\n]*\\\n\s+\|\| : > "\$RUNNER_TEMP/CHANGELOG\.md"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
