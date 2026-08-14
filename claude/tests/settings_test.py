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


def mutation_tokens(cmd):
    """状態変更語との照合に使う語を返す。

    空白だけで割ると、ハイフンで繋がった名前 (エイリアスに多い `gone-clean`
    `stale-clean` など) の中に隠れた語を取り逃がす。ハイフンで割った断片も
    足して照合する。`cherry-pick` のように語そのものがハイフンを含むものは
    分割前の形を集合に残すので、これまでどおり拾える。
    """
    tokens = set(cmd.split())
    return tokens | {part for token in tokens for part in token.split("-") if part}


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
            hits = MUTATING_WORDS.intersection(mutation_tokens(cmd))
            with self.subTest(rule=rule):
                self.assertEqual(
                    hits, set(),
                    f"状態を変えうる語 {sorted(hits)} を含む allow は置かない "
                    f"(確認プロンプトが消えるのは読み取り専用のコマンドだけ)",
                )

    def test_allow_rules_unique(self):
        self.assertEqual(len(self.allow), len(set(self.allow)), "allow が重複している")


class AdditionalDirectoriesTestCase(unittest.TestCase):
    """`permissions.additionalDirectories` のテスト。

    ここに書いたディレクトリは全マシンで確認なしに読める範囲になる (編集の可否は
    permission mode に従うので、default モードなら書き込みの確認は残る)。緩めるのは
    「毎回同じ承認を押している場所」だけに留めたいので、範囲の広すぎとマシン依存の
    書き方を CI で落とす。
    """

    @classmethod
    def setUpClass(cls):
        data = json.loads(SETTINGS.read_text())
        cls.dirs = data.get("permissions", {}).get("additionalDirectories", [])

    def test_paths_are_home_relative(self):
        """`~/` 始まりで書く (ユーザー名を埋め込むとマシンを移ったとき効かなくなる)。

        `~` が展開されることは実測済み: 範囲外の空ディレクトリから `claude -p` を
        走らせると、`~/Repos` を渡したときだけ範囲外のファイルを読めた。
        """
        for path in self.dirs:
            with self.subTest(path=path):
                self.assertTrue(
                    path.startswith("~/"),
                    "additionalDirectories は ~/ 始まりで書く "
                    "(絶対パスはユーザー名がマシンに依存する)",
                )

    def test_paths_are_not_too_broad(self):
        """ホーム直下や / をまるごと開けない。

        個別のディレクトリを足すのは範囲の追加だが、ホームそのものを足すのは
        「全部入り」への切り替えで意味が違う。
        """
        for path in self.dirs:
            with self.subTest(path=path):
                self.assertNotIn(
                    path.rstrip("/"), {"~", "/", ""},
                    "ホーム全体・ルート全体を開ける指定は置かない",
                )

    def test_paths_unique(self):
        self.assertEqual(
            len(self.dirs), len(set(self.dirs)),
            "additionalDirectories が重複している",
        )


class AutoModeTestCase(unittest.TestCase):
    """auto mode の分類器設定 (autoMode) の検証。

    分類器は allow / soft_deny / hard_deny / environment を配列として読むが、
    `"$defaults"` を含まない配列を書くと**その節の組み込みルールを丸ごと捨てる**
    (force push・curl | bash・本番デプロイ・データ持ち出し・auto-mode bypass)。
    緩めた自覚が残らないまま守りが消えるので、書式の側で落とす。

    この設定はプロジェクトの .claude/settings.json からもプラグインからも供給
    できない (リポジトリが自分でルールを緩められないための公式仕様) ため、
    このファイルが唯一の置き場になる。
    """

    SECTIONS = ("allow", "soft_deny", "hard_deny", "environment")

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(SETTINGS.read_text())
        cls.auto = cls.data.get("autoMode", {})

    def test_arrays_keep_builtin_defaults(self):
        for name in self.SECTIONS:
            value = self.auto.get(name)
            if value is None:
                continue
            with self.subTest(section=name):
                self.assertIsInstance(value, list, f"autoMode.{name} は配列で書く")
                self.assertIn(
                    "$defaults", value,
                    f'autoMode.{name} に "$defaults" が無い '
                    f"(この節の組み込みルールを丸ごと捨てている)",
                )

    def test_default_mode_is_auto(self):
        """承認プロンプトを分類器へ寄せる前提そのもの。

        auto でなければ autoMode の記述は効かず、上のテストも意味を失う。
        """
        self.assertEqual(
            self.data.get("permissions", {}).get("defaultMode"), "auto",
            "permissions.defaultMode を auto にする (ユーザー設定でのみ有効)",
        )

    def test_ask_rules_exist(self):
        """取り返しのつかない操作で必ず人間に返る境界を残す。

        ask ルールは分類器より前に評価され、auto モードでも確実にプロンプトになる。
        空にすると、削除まで分類器の判断だけに委ねることになる。
        """
        ask = self.data.get("permissions", {}).get("ask", [])
        self.assertTrue(ask, "permissions.ask に不可逆操作のチェックポイントを残す")
        for rule in ask:
            with self.subTest(rule=rule):
                self.assertRegex(rule, RULE_RE, "ask は Bash(...) 形式で書く")


if __name__ == "__main__":
    unittest.main(verbosity=2)
