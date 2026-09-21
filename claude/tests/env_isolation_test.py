#!/usr/bin/env python3
"""テストが呼び出し元のセッションの環境変数を子プロセスへ漏らしていないかを見るメタテスト。

背景と落とし方は claude/tests/hookenv.py の冒頭。ここが見るのは「その方針が守られて
いるか」だけで、見るのは 3 つ:

  A. 変数の集め方が壊れていないか (実在するスイッチを拾えているか)
  B. スクリプトを起動する subprocess.run が、環境を明示して渡しているか
  C. その環境が呼び出し元の環境の写しでないか (os.environ を組み直していないか)
  D. zsh を起動するテストが、呼び出し元の rc ファイル (~/.zshenv) を読ませていないか

B と C は両方が要る。issue #138 のテストは `env()` を直せば C を満たすが、`env` を
渡さない `subprocess.run` が 5 件残っていて、そちらは親の環境をそのまま継承していた
(`subprocess.run` は `env` 省略時に親の環境を渡す)。

    python3 claude/tests/env_isolation_test.py
"""

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import (
    SYSTEM_VARIABLES,
    clean_env,
    raw_variables_read_by,
    script_files,
    switch_variables,
)

TESTS = Path(__file__).resolve().parent

# 走査が壊れて「何も見ていない緑」になるのを防ぐ番人。実在するスイッチを名指しで置く
CANARIES = frozenset(
    {
        "CLAUDE_PLAN_RECORD",
        "CLAUDE_GH_COMMENT_GUARD",
        "CLAUDE_WAIT_DEADLINE_GUARD",
        "TERM_GUARD_RULES_DIR",
        "SECRET_CACHE_ALLOWLIST",
        # numeric_env 経由 (eval) で読まれるもの。走査がこの綴りを拾えていることの番人
        "SECRET_CACHE_TTL",
        "SECRET_CACHE_RETRY",
    }
)

# 素の環境を組み直していないかを見る綴り
BARE_ENVIRON = ("dict(os.environ)", "**os.environ", "os.environ.items()")


def test_modules():
    return sorted(TESTS.glob("*_test.py"))


def script_launches(module):
    """その test が repo のスクリプトを起動する subprocess.run の一覧 (ast ノード)。"""
    source = module.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if "SCRIPT" in segment:
            yield node


def zsh_launches(module):
    """その test が zsh を起動する subprocess.run と、それを囲む関数の本文。"""
    source = module.read_text()
    tree = ast.parse(source)
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.get_source_segment(source, function) or ""
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and node.args
                and isinstance(node.args[0], ast.List)
                and node.args[0].elts
                and isinstance(node.args[0].elts[0], ast.Constant)
                and node.args[0].elts[0].value == "zsh"
            ):
                continue
            argv = [e.value for e in node.args[0].elts if isinstance(e, ast.Constant)]
            yield node, argv, body


class EnvIsolationTestCase(unittest.TestCase):
    # --- A: 集め方が効いているか --------------------------------------------

    def test_スクリプトから変数を拾えている(self):
        missing = CANARIES - switch_variables()
        self.assertFalse(
            missing,
            f"実在するスイッチを拾えていない — 走査が壊れている: {sorted(missing)}",
        )

    def test_システム変数を落とす対象にしていない(self):
        # スクリプトは USER のようなシステム変数も既定値つきで読む。走査はそれも拾うので、
        # 除外リストが効いていないと clean_env がそれを落とし、子プロセスが動かなくなる
        leaked = {v for v in switch_variables() if v in SYSTEM_VARIABLES}
        self.assertFalse(
            leaked,
            f"システム変数を落とす対象にしている: {sorted(leaked)}",
        )

    def test_走査がシステム変数も拾っている(self):
        # 上の検査が空振りしないことの確認。生の走査にシステム変数が 1 つも現れないなら、
        # 除外リストは何も守っておらず、上は永久に緑になる
        raw = set()
        for script in script_files():
            raw |= raw_variables_read_by(script)
        self.assertTrue(
            raw & SYSTEM_VARIABLES,
            "生の走査にシステム変数が無い — 除外リストの検査が空振りしている",
        )

    def test_clean_env_が子プロセスに要る変数を残す(self):
        env = clean_env()
        for name in ("PATH", "HOME"):
            if name in os.environ:
                self.assertIn(name, env, f"{name} を落としている")

    # --- B: 起動時に環境を明示しているか ------------------------------------

    def test_スクリプトの起動が環境を明示している(self):
        problems = [
            f"{module.name}:{node.lineno}"
            for module in test_modules()
            for node in script_launches(module)
            if not any(keyword.arg == "env" for keyword in node.keywords)
        ]
        self.assertFalse(
            problems,
            "env を渡さない subprocess.run がある (親の環境をそのまま継承する):\n  "
            + "\n  ".join(problems)
            + "\n\nenv=clean_env(...) を渡してください。",
        )

    # --- C: 呼び出し元の環境の写しでないか ----------------------------------
    #
    # 漏れる経路は「os.environ を丸ごと写す」ことだけなので、そこだけを見る。
    # ゼロから組んだ辞書 (runcat_metrics_test) は写していないので素通しでよい

    def test_素の_os_environ_を組み直していない(self):
        problems = []
        for module in test_modules():
            if not any(script_launches(module)):
                continue
            source = module.read_text()
            for spelling in BARE_ENVIRON:
                if spelling in source:
                    problems.append(f"{module.name} に {spelling} がある")
        self.assertFalse(
            problems,
            "呼び出し元の環境をそのまま組み直している (スイッチが漏れる):\n  "
            + "\n  ".join(problems)
            + "\n\nhookenv.clean_env を使ってください。",
        )

    # --- D: rc ファイル経由で戻ってこないか ----------------------------------
    #
    # 環境変数を落としても、**子の zsh が ~/.zshenv を読めば戻ってくる**。このリポジトリの
    # zshenv は /opt/homebrew/bin と setup の bin/ を PATH の先頭へ足すので、偽コマンドを
    # PATH の先頭へ置いたつもりのテストで、Homebrew に同名があれば本物が先に引かれる。
    # issue #199 のテストを書いているとき、偽 brew のつもりで本物の
    # `brew install --cask --adopt` が走った (issue #238)。
    #
    # もう 1 つの害は「検証対象のすり替わり」。~/.zshenv は本体 checkout の zshenv への
    # symlink なので、worktree の zshenv を壊しても、先に読まれた本体側が値を入れて緑になる。

    def test_clean_env_は呼び出し元の_zshenv_を読ませない(self):
        with tempfile.TemporaryDirectory() as home:
            marker = Path(home) / "ZSHENV_WAS_READ"
            (Path(home) / ".zshenv").write_text(f'touch "{marker}"\n')

            subprocess.run(["zsh", "-c", "true"], env=clean_env(HOME=home), check=True)
            self.assertFalse(marker.exists(), "clean_env の下で ~/.zshenv が読まれた")

            # 対照。ZDOTDIR を外せば読まれる (上が空振りしていないこと)
            subprocess.run(["zsh", "-c", "true"], env=clean_env(HOME=home, ZDOTDIR=None), check=True)
            self.assertTrue(marker.exists(), "対照が成立していない — zsh が ~/.zshenv を読まない環境")

    def test_clean_env_の_ZDOTDIR_は空のディレクトリ(self):
        zdotdir = Path(clean_env()["ZDOTDIR"])
        self.assertTrue(zdotdir.is_dir())
        self.assertEqual(list(zdotdir.iterdir()), [])

    def test_zsh_の起動が_rc_ファイルを読ませていない(self):
        """zsh を起動するなら -f を付けるか、clean_env (ZDOTDIR を空へ向ける) を通す。

        スクリプトをファイルとして流す形 (`zsh setup.zsh`) は -f では足りない —
        そのスクリプトが起動する孫の zsh が ~/.zshenv を読む。ZDOTDIR は環境変数なので
        孫まで届く。
        """
        found = 0
        problems = []
        for module in test_modules():
            for node, argv, body in zsh_launches(module):
                found += 1
                if "-f" in argv or "clean_env(" in body or "ZDOTDIR" in body:
                    continue
                problems.append(f"{module.name}:{node.lineno}")
        self.assertGreater(found, 0, "zsh の起動を 1 つも見つけられない — 走査が壊れている")
        self.assertFalse(
            problems,
            "呼び出し元の ~/.zshenv を読む zsh の起動がある:\n  "
            + "\n  ".join(problems)
            + "\n\n-f を付けるか、env=clean_env(...) を渡してください。",
        )


if __name__ == "__main__":
    unittest.main()
