#!/usr/bin/env python3
"""`.github/scripts/claude-changelog-diff.py` のテスト。

このスクリプトは週次の検知 (起票するかどうか) と、見直しスキルが読む材料の両方を
決める。間違え方が 2 つあって、どちらも黙って起きる:

  拾いすぎ   差が在るたびに起票する。確認が反射になり、誰も読まなくなる
  拾い漏れ   版の比較や境界を誤り、見直していない版を「差なし」と言う

なので、境界 (見直し済みの版そのものは含めない)・数値としての版の比較
(2.1.99 < 2.1.100)・語の境界 (`Edit` が `Edited` に当たらない)・起票条件の 4 通り・
節の優先度と切り詰めを、作った changelog で固定する。

    python3 claude/tests/changelog_diff_test.py
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / ".github" / "scripts" / "claude-changelog-diff.py"

spec = importlib.util.spec_from_file_location("claude_changelog_diff", SCRIPT)
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)

CHANGELOG = """# Changelog

## 2.1.100

- Added `updatedInput` support to PreToolUse hooks
- Added a brand new `fooBar` setting nobody wrote down
- Edited wording in the onboarding flow

## 2.1.99

- Fixed Bash tool output truncation
- Removed the legacy `--foo` flag

## 2.1.98

- Fixed a crash in the PreToolUse hook runner

## 2.1.9

- Ancient PreToolUse entry that must never be reported
"""


def ledger(version="2.1.98", date="2026-09-01"):
    return {
        "version": 1,
        "reviewed_against": {"claude_code": version, "date": date},
        "intents": [
            {
                "id": "guard",
                "depends_on": [
                    {"surface": "hook-event", "name": "PreToolUse", "stability": "documented"},
                    {"surface": "tool-name", "name": "Bash", "stability": "documented"},
                    {"surface": "tool-name", "name": "Edit", "stability": "documented"},
                    {"surface": "harness-behavior", "name": "日本語で書いた振る舞いの説明", "stability": "undocumented"},
                ],
            },
            {
                "id": "signature",
                "depends_on": [
                    {"surface": "hook-output", "name": "updatedInput", "stability": "documented"},
                    {"surface": "hook-event", "name": "PreToolUse", "stability": "documented"},
                ],
            },
        ],
    }


class VersionBoundaryTest(unittest.TestCase):
    def test_reviewed_version_itself_is_excluded(self):
        summary, newer, _ = diff.summarize(CHANGELOG, ledger("2.1.98"), "2026-09-02")
        self.assertEqual([label for _v, label, _l in newer], ["2.1.100", "2.1.99"])
        self.assertEqual(summary["versions"], 2)

    def test_versions_compare_as_numbers(self):
        """文字列で比べると 2.1.99 > 2.1.100、2.1.9 > 2.1.98 になって拾い漏れる。"""
        _, newer, _ = diff.summarize(CHANGELOG, ledger("2.1.99"), "2026-09-02")
        self.assertEqual([label for _v, label, _l in newer], ["2.1.100"])
        self.assertEqual(diff.summarize(CHANGELOG, ledger("2.1.98"), "2026-09-02")[0]["latest"], "2.1.100")

    def test_up_to_date_is_silent(self):
        summary, newer, hits = diff.summarize(CHANGELOG, ledger("2.1.100"), "2026-09-02")
        self.assertEqual((newer, hits), ([], []))
        self.assertFalse(summary["issue_needed"])

    def test_reviewed_version_missing_from_changelog(self):
        """見直し済みの版が changelog に載っていなくても、数値の大小だけで切れる。"""
        _, newer, _ = diff.summarize(CHANGELOG, ledger("2.1.50"), "2026-09-02")
        self.assertEqual([label for _v, label, _l in newer], ["2.1.100", "2.1.99", "2.1.98"])


class HitTest(unittest.TestCase):
    def setUp(self):
        _, _, self.hits = diff.summarize(CHANGELOG, ledger("2.1.98"), "2026-09-02")

    def hit(self, fragment):
        return next(h for h in self.hits if fragment in h[1])

    def test_strong_hit_names_every_intent(self):
        label, _line, words, intent_ids, strong = self.hit("updatedInput")
        self.assertEqual(label, "2.1.100")
        self.assertEqual(sorted(words), ["PreToolUse", "updatedInput"])
        self.assertEqual(intent_ids, ["guard", "signature"])
        self.assertTrue(strong)

    def test_tool_name_is_a_weak_hit(self):
        _label, _line, words, _ids, strong = self.hit("Bash tool")
        self.assertEqual(words, ["Bash"])
        self.assertFalse(strong)

    def test_word_boundary(self):
        """`Edit` は `Edited` に当たらない。"""
        self.assertFalse([h for h in self.hits if "Edited wording" in h[1]])

    def test_old_sections_are_never_reported(self):
        self.assertFalse([h for h in self.hits if "Ancient" in h[1] or "crash" in h[1]])

    def test_non_ascii_names_are_not_searched(self):
        self.assertNotIn("日本語で書いた振る舞いの説明", diff.vocabulary(ledger()))

    def test_word_is_strong_if_any_surface_is_strong(self):
        mixed = ledger()
        mixed["intents"][0]["depends_on"].append({"surface": "cli", "name": "Bash", "stability": "documented"})
        self.assertTrue(diff.vocabulary(mixed)["Bash"][0])


class IssueNeededTest(unittest.TestCase):
    """起票条件: 新しい版が在り、かつ (強い当たりが在る または 見直しが古い)。"""

    WEAK_ONLY = "## 2.1.99\n\n- Fixed Bash tool output truncation\n\n## 2.1.98\n\n- old\n"

    def test_strong_hit(self):
        self.assertTrue(diff.summarize(CHANGELOG, ledger("2.1.98"), "2026-09-02")[0]["issue_needed"])

    def test_weak_hits_alone_stay_silent(self):
        summary, _, hits = diff.summarize(self.WEAK_ONLY, ledger("2.1.98"), "2026-09-02")
        self.assertEqual((summary["strong_hits"], summary["weak_hits"], len(hits)), (0, 1, 1))
        self.assertFalse(summary["issue_needed"])

    def test_stale_review_without_strong_hits(self):
        summary, _, _ = diff.summarize(self.WEAK_ONLY, ledger("2.1.98", "2026-08-01"), "2026-09-02")
        self.assertEqual(summary["age_days"], 32)
        self.assertTrue(summary["issue_needed"])

    def test_age_boundary_is_exclusive(self):
        self.assertFalse(diff.summarize(self.WEAK_ONLY, ledger("2.1.98", "2026-08-03"), "2026-09-02")[0]["issue_needed"])

    def test_stale_but_nothing_new_stays_silent(self):
        self.assertFalse(diff.summarize(CHANGELOG, ledger("2.1.100", "2026-01-01"), "2026-09-02")[0]["issue_needed"])

    def test_unparseable_changelog_is_reported_not_silenced(self):
        """取得の失敗や見出し形式の変更を「差なし」と読むと、検知が黙って死ぬ。"""
        summary, newer, hits = diff.summarize("<html>rate limited</html>", ledger(), "2026-09-02")
        self.assertTrue(summary["parse_failed"])
        self.assertTrue(summary["issue_needed"])
        self.assertIn("読めなかった", diff.render(summary, newer, hits))


class RenderTest(unittest.TestCase):
    HEADINGS = ("## 本体の仕様語", "## 追加・変更・削除", "## ツール名・一般語", "## 全抜粋")

    def setUp(self):
        self.args = diff.summarize(CHANGELOG, ledger("2.1.98"), "2026-09-02")

    def section(self, report, index):
        start = report.index(self.HEADINGS[index])
        rest = self.HEADINGS[index + 1:]
        return report[start:report.index(rest[0])] if rest else report[start:]

    def test_sections_in_order_of_reading_value(self):
        report = diff.render(*self.args)
        self.assertIn("# Claude Code 2.1.98 → 2.1.100 (2 版)", report)
        positions = [report.index(h) for h in self.HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("意図: `guard`, `signature`", report)

    def test_new_feature_outside_the_vocabulary_is_surfaced(self):
        """自作を置き換えられる新機能は、台帳の語彙に無い言葉で書かれる。当たりの節だけでは拾えない。"""
        report = diff.render(*self.args)
        self.assertNotIn("fooBar", self.section(report, 0))
        self.assertIn("fooBar", self.section(report, 1))
        self.assertIn("Removed the legacy", self.section(report, 1))

    def test_strong_hits_are_not_repeated_as_changes(self):
        self.assertNotIn("updatedInput", self.section(diff.render(*self.args), 1))

    def test_fixes_are_not_listed_as_changes(self):
        self.assertNotIn("truncation", self.section(diff.render(*self.args), 1))

    def long_args(self):
        long_log = (
            "## 2.1.99\n\n- Added PreToolUse thing\n"
            + "".join(f"- Added setting number {n}\n" for n in range(40))
            + "".join(f"- Fixed Bash filler line {n}\n" for n in range(5000))
        )
        return diff.summarize(long_log, ledger("2.1.98"), "2026-09-02")

    def test_never_exceeds_the_limit(self):
        """GitHub の Issue 本文には上限が在る。節が増えても、どれかが長くても超えない。"""
        for limit in (60000, 20000, 5000, 2500):
            with self.subTest(limit):
                self.assertLessEqual(len(diff.render(*self.long_args(), max_chars=limit)), limit)

    def test_lower_sections_are_cut_first(self):
        report = diff.render(*self.long_args(), max_chars=5000)
        self.assertIn("Added PreToolUse thing", self.section(report, 0))
        self.assertNotIn("入らなかった", self.section(report, 0))
        self.assertIn("Added setting number 39", self.section(report, 1))
        self.assertIn("入らなかった", self.section(report, 3))
        self.assertIn(diff.CHANGELOG_URL, self.section(report, 3))

    def test_empty_sections_say_so(self):
        quiet = "## 2.1.99\n\n- Fixed something unrelated\n\n## 2.1.98\n\n- old\n"
        report = diff.render(*diff.summarize(quiet, ledger("2.1.98", "2026-01-01"), "2026-09-02"))
        self.assertIn("(なし)", self.section(report, 0))
        self.assertIn("Fixed something unrelated", self.section(report, 3))


class CommandLineTest(unittest.TestCase):
    def test_prints_summary_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "CHANGELOG.md").write_text(CHANGELOG)
            (tmp / "intents.json").write_text(json.dumps(ledger("2.1.98")))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--changelog", str(tmp / "CHANGELOG.md"),
                 "--intents", str(tmp / "intents.json"), "--today", "2026-09-02",
                 "--report", str(tmp / "report.md")],
                capture_output=True, text=True, check=True, env=clean_env(),
            )
            summary = json.loads(result.stdout)
            self.assertEqual((summary["latest"], summary["versions"], summary["issue_needed"]), ("2.1.100", 2, True))
            self.assertIn("## 全抜粋", (tmp / "report.md").read_text())

    def test_reads_the_real_ledger(self):
        """台帳の形が変わってスクリプトが読めなくなったら、ここで落ちる。"""
        real = json.loads((REPO / "claude" / "intents.json").read_text())
        words = diff.vocabulary(real)
        self.assertTrue(words["PreToolUse"][0])
        self.assertFalse(words["Bash"][0])


if __name__ == "__main__":
    unittest.main()
