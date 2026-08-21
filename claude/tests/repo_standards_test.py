#!/usr/bin/env python3
"""claude/repo-standards.json のテスト。

repo-standards.json はリポジトリ標準チェックリストの正本で、消費者は
claude-plugins の repo-standards プラグイン (同梱スクリプトが jq でパースする)。
ここではスキーマの整合 (enum・必須フィールド・参照解決) を検証し、
消費側が黙って項目を読み飛ばす事故を防ぐ。

    python3 claude/tests/repo_standards_test.py
"""

import json
import unittest
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "repo-standards.json"

LAYERS = {"repo", "github", "claude"}
LEVELS = {"required", "recommended", "rejected"}
CHECK_TYPES = {"file_exists", "file_absent", "glob_exists", "gh_api", "builtin", "llm"}
# 修正の性質。repo-audit-fix が承認の粒度を決める材料で、値はプラグイン側と共有する契約
FIX_KINDS = {"deterministic", "generative", "destructive"}
# 実行すると取り返しがつかない fix はこの語のどれかを必ず含む。分類の取りこぼしを機械で捕まえる
DESTRUCTIVE_MARKERS = ("削除", "git rm ", "履歴の書き換え")
# check type ごとの必須フィールド。消費側スクリプトとの契約
CHECK_REQUIRED_FIELDS = {
    "file_exists": {"path"},
    "file_absent": {"path"},
    "glob_exists": {"path"},
    "gh_api": {"endpoint", "jq", "expect"},
    "builtin": {"name"},
    "llm": {"prompt"},
}


class RepoStandardsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(MANIFEST.read_text())
        cls.items = cls.data["items"]
        cls.kinds = cls.data["kinds"]

    def test_version(self):
        self.assertEqual(self.data["version"], 1)

    def test_kind_ids_unique_and_fallback_exists(self):
        ids = [k["id"] for k in self.kinds]
        self.assertEqual(len(ids), len(set(ids)), "kind id が重複している")
        # marker: null の kind が無いと、どの marker にも合致しないリポの kind が決まらない
        self.assertTrue(
            any(k["marker"] is None for k in self.kinds),
            "フォールバック用の marker: null な kind が無い",
        )

    def test_item_ids_unique(self):
        ids = [i["id"] for i in self.items]
        self.assertEqual(len(ids), len(set(ids)), "item id が重複している")

    def test_enums(self):
        for item in self.items:
            with self.subTest(id=item["id"]):
                self.assertIn(item["layer"], LAYERS)
                self.assertIn(item["level"], LEVELS)
                self.assertIn(item["check"]["type"], CHECK_TYPES)

    def test_check_required_fields(self):
        for item in self.items:
            check = item["check"]
            missing = CHECK_REQUIRED_FIELDS[check["type"]] - set(check)
            with self.subTest(id=item["id"]):
                self.assertFalse(
                    missing, f"check.type={check['type']} に必須の {missing} が無い"
                )

    def test_check_fields_not_blank(self):
        # 必須フィールドが空文字でも存在チェックだけは通ってしまう。空の prompt は
        # LLM 判定が何も判定できないまま ok に見え、空の path は全ファイルに一致する
        for item in self.items:
            check = item["check"]
            for field in CHECK_REQUIRED_FIELDS[check["type"]]:
                with self.subTest(id=item["id"], field=field):
                    self.assertTrue(
                        str(check[field]).strip(), f"check.{field} が空"
                    )

    def test_applies_to_resolves(self):
        kind_ids = {k["id"] for k in self.kinds} | {"all"}
        for item in self.items:
            with self.subTest(id=item["id"]):
                self.assertTrue(item["applies_to"], "applies_to が空")
                for target in item["applies_to"]:
                    self.assertIn(target, kind_ids, f"未定義の kind: {target}")

    def test_when_visibility(self):
        for item in self.items:
            when = item.get("when")
            if when is None:
                continue
            with self.subTest(id=item["id"]):
                self.assertEqual(set(when), {"visibility"}, "when は visibility のみ対応")
                self.assertIn(when["visibility"], {"public", "private"})

    def test_required_items_have_fix(self):
        # required 違反は必ず修正提案とセットで報告する。fix 無しでは監査が行き止まりになる
        for item in self.items:
            if item["level"] != "required":
                continue
            with self.subTest(id=item["id"]):
                self.assertTrue(item.get("fix", "").strip(), "required 項目に fix が無い")

    def test_fix_kind_values(self):
        for item in self.items:
            if "fix_kind" not in item:
                continue
            with self.subTest(id=item["id"]):
                self.assertIn(item["fix_kind"], FIX_KINDS)

    def test_fix_kind_accompanies_fix(self):
        # fix_kind は fix の性質を宣言するフィールド。fix が無い項目に付けると意味が濁り、
        # fix があるのに付いていないと修正側が LLM 推定へ落ちて承認の粒度がブレる
        for item in self.items:
            with self.subTest(id=item["id"]):
                if item.get("fix", "").strip():
                    self.assertIn("fix_kind", item, "fix があるのに fix_kind が無い")
                else:
                    self.assertNotIn("fix_kind", item, "fix が無いのに fix_kind がある")

    def test_destructive_fixes_are_marked(self):
        # 削除・追跡外し・履歴の書き換えを促す fix が deterministic に紛れると、
        # まとめて 1 承認の群に入って自動適用されてしまう
        for item in self.items:
            fix = item.get("fix", "")
            if not any(marker in fix for marker in DESTRUCTIVE_MARKERS):
                continue
            with self.subTest(id=item["id"]):
                self.assertEqual(item.get("fix_kind"), "destructive")

    def test_every_item_has_why(self):
        # why はレポートにそのまま出す根拠。無いと「なぜ直すのか」が説明できない
        for item in self.items:
            with self.subTest(id=item["id"]):
                self.assertTrue(item.get("why", "").strip(), "why が無い")


if __name__ == "__main__":
    unittest.main()
