#!/usr/bin/env python3
"""claude/settings.json のテスト。

settings.json は全マシンのグローバル設定の正本で、JSON が壊れると設定が丸ごと
無視される (repo-standards の env-doctor が検知する事故そのもの)。ここでは
パースの成立に加えて、`permissions.allow` が**読み取り専用の範囲を出ていない**
ことを検証する。

allow は確認プロンプトを消すための宣言なので、うっかり書き込み系を載せると
「聞かれずに実行される」側へ倒れる。文書ルールで抑えるのでなく、変更を伴う
コマンドが混ざった時点で CI が落ちるようにしておく。

    python3 claude/tests/settings_test.py
"""

import json
import re
import unittest
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "settings.json"

# 状態を変える (取り消しに手間がかかる・外部へ influence が及ぶ) サブコマンドや
# フラグの語。allow に載せてよいのは、これらを含まない読み取り専用の呼び出しだけ。
# 新しい allow を足すときに引っかかったら、それは足す前に考え直す合図
MUTATING_WORDS = {
    # git: 履歴・作業ツリー・リモートを変える
    "commit", "push", "merge", "rebase", "reset", "revert", "restore",
    "checkout", "switch", "branch", "tag", "clean", "stash", "cherry-pick",
    "apply", "mv", "gc", "prune", "remote", "submodule", "worktree",
    # gh: GitHub 側を変える / 任意のメソッドを投げられる
    "api", "create", "edit", "delete", "close", "reopen", "comment",
    "upload", "sync", "clone", "fork", "ready", "review", "run", "rerun",
    # 一般
    "rm", "mkdir", "cp", "install", "publish", "deploy", "curl", "ssh",
}

# 上の語を含んでいても読み取りに閉じている例外。語単位の判定では拾えないので
# ルール全体で照合する (git worktree list は一覧表示、gh pr diff は差分の表示)
READ_ONLY_EXCEPTIONS = {
    "git worktree list",
    "gh pr diff",
}

RULE_RE = re.compile(r"^Bash\((?P<cmd>.+?)(?P<glob>:\*|\s\*)?\)$")


class SettingsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SETTINGS.read_text()
        cls.data = json.loads(cls.raw)
        cls.allow = cls.data.get("permissions", {}).get("allow", [])

    def test_is_valid_json(self):
        # setUpClass で落ちれば十分だが、失敗理由を「JSON が壊れている」と
        # 名指しできるようにテストとしても残す
        self.assertIsInstance(json.loads(self.raw), dict)

    def test_allow_rules_are_bash_rules(self):
        for rule in self.allow:
            with self.subTest(rule=rule):
                self.assertRegex(rule, RULE_RE, "allow は Bash(...) 形式で書く")

    def test_allow_rules_are_not_blanket(self):
        """コマンド名だけの包括ルール (Bash(git:*) など) を禁じる。

        サブコマンドを問わず許すと、読み取りのつもりが push や delete まで
        通ってしまう。許すのは必ずサブコマンドまで固定した形にする。
        """
        for rule in self.allow:
            with self.subTest(rule=rule):
                m = RULE_RE.match(rule)
                self.assertIsNotNone(m, f"解析できないルール: {rule}")
                cmd = m.group("cmd").strip()
                self.assertNotEqual(cmd, "*", "全コマンドを許すルールは置かない")
                if cmd in {"git", "gh", "npm", "docker", "kubectl"}:
                    self.fail(f"サブコマンドを固定していない包括ルール: {rule}")

    def test_allow_rules_are_read_only(self):
        for rule in self.allow:
            m = RULE_RE.match(rule)
            cmd = m.group("cmd").strip() if m else rule
            if cmd in READ_ONLY_EXCEPTIONS:
                continue
            hits = MUTATING_WORDS.intersection(cmd.split())
            with self.subTest(rule=rule):
                self.assertEqual(
                    hits, set(),
                    f"状態を変えうる語 {sorted(hits)} を含む allow は置かない "
                    f"(確認プロンプトが消えるのは読み取り専用のコマンドだけ)",
                )

    def test_allow_rules_unique(self):
        self.assertEqual(len(self.allow), len(set(self.allow)), "allow が重複している")


if __name__ == "__main__":
    unittest.main(verbosity=2)
