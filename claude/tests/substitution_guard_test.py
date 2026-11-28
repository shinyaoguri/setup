#!/usr/bin/env python3
"""claude/substitution-guard.py のテスト。

フックの契約 (stdin の JSON → deny の JSON、あるいは無出力) をサブプロセス経由で
検証する。判定材料はコマンド文字列だけなので、外のコマンドは一切実行しない。

    python3 claude/tests/substitution_guard_test.py
"""

import json
import subprocess
import unittest
from pathlib import Path

from hookenv import clean_env

SCRIPT = Path(__file__).resolve().parent.parent / "substitution-guard.py"

# setup#309 で実際に打たれた形 (Issue 番号とパスを伏せ、本文を短くしただけ)
INCIDENT = (
    "S=x; gh issue view 1 > $S/b.md; python3 - <<EOF\n"
    'p="$S/b.md"\n'
    'new="""## 完了条件\n'
    "… `make test` と `make test-release` の `swift test` は …\n"
    '"""\n'
    "EOF"
)

# deny になるもの: (説明, コマンド, 差し戻しの文面に含まれるべき語)
DENIED = [
    ("実害の実物の形", INCIDENT, "<<'EOF'"),
    ("二重引用符のコミットメッセージ", 'git commit -m "fix: `foo` を直す"', "単一引用符"),
    ("二重引用符の gh の本文", 'gh pr comment 1 --body "`make test` を流した"', "--body-file"),
    ("heredoc の本文の $( の中のバッククォート", "cat <<EOF\n$(echo `x`)\nEOF", "<<'EOF'"),
    ("<<- の本文 (タブ付きの区切り)", "cat <<-EOF\n\t`x`\n\tEOF", "<<'EOF'"),
    ("<<- の後に空白", "cat <<- EOF\n`x`\nEOF", "<<'EOF'"),
    ("区切りの名前を文面に写す", "cat <<BODY\n`x`\nBODY", "<<'BODY'"),
    ("引用符の外の素のバッククォート", "echo `date`", "$(…)"),
    (
        "1 行に 2 つの heredoc (2 つ目だけ引用なし)",
        "cat <<'A' - <<B\n`a`\nA\n`b`\nB",
        "<<'B'",
    ),
    ("heredoc の本文の引用符は字", "cat <<EOF\n'`x`'\nEOF", "<<'EOF'"),
    ("heredoc の本文の逃がしの逃がし", "cat <<EOF\n\\\\`x`\nEOF", "<<'EOF'"),
    ("区切りに届かない本文も読む", "cat <<EOF\n`x`", "<<'EOF'"),
    ("$( ) の中の heredoc を引用し忘れた", 'git commit -m "$(cat <<EOF\nfix: `foo`\nEOF\n)"', "<<'EOF'"),
    ("$( ) の中の二重引用符", 'X="$(echo "`y`")"', "単一引用符"),
    ("${…} の既定値の中", 'echo "${X:-`date`}"', "単一引用符"),
    ("単一引用符の中の \\ は逃がしにならない", "echo 'a\\' `date`", "$(…)"),
    ("語の途中の # はコメントでない", "echo a#`date`", "$(…)"),
    ("改行の後の行も見る", "echo ok\ngit commit -m \"`x`\"", "単一引用符"),
    ("here-string の二重引用符の中", 'cat <<< "`x`"', "単一引用符"),
    ("<( ) の中", "diff <(echo `x`) b", "$(…)"),
]

# 素通しになるもの: (説明, コマンド)
ALLOWED = [
    ("実害の形を <<'EOF' にしたもの", INCIDENT.replace("<<EOF", "<<'EOF'")),
    ('<<"EOF"', INCIDENT.replace("<<EOF", '<<"EOF"')),
    ("<<\\EOF", INCIDENT.replace("<<EOF", "<<\\EOF")),
    ('部分的な引用 <<E"OF"', INCIDENT.replace("<<EOF", '<<E"OF"')),
    ("単一引用符のコミットメッセージ", "git commit -m 'fix: `foo` を直す'"),
    ("二重引用符の中で逃がしたもの", 'echo "\\`x\\`"'),
    ("引用符の外で逃がしたもの", "echo \\`x\\`"),
    ("heredoc の本文で逃がしたもの", "cat <<EOF\n\\`x\\` \\$(y)\nEOF"),
    ("GH_TOKEN の代入", 'GH_TOKEN="$(bash x.sh)" && gh pr create'),
    ("引用符の外の $( )", "gh pr view $(git branch --show-current)"),
    # setup#309 の実測で、引用符なしの heredoc の $( は事故 0 件・意図した用途 10 件だった
    ("引用符なしの heredoc の $(", "cat <<EOF\n$(date)\nEOF"),
    ("引用符なしの heredoc の $( (実測の形)", "cat <<EOF\nmokume @ $(git rev-parse --short HEAD)\nEOF"),
    ("<<- の本文の $(", "cat <<-EOF\n\t$(cat $S/top.txt)\n\tEOF"),
    # 実測の形: バッククォートは逃がし、$( ) で値を差し込む。バッククォートを含むので字句解析まで届く
    (
        "逃がしたバッククォートと heredoc の $(",
        "cat <<EOF\n\\`cmp\\` で main (@ $(git rev-parse --short origin/main)) と比べた\nEOF",
    ),
    ("$VAR だけの heredoc", "cat <<EOF\n$HOME ${USER}\nEOF"),
    ("引用した heredoc の本文", "cat <<'EOF'\n`x` $(y)\nEOF"),
    ("引用した <<- の本文", "cat <<-'EOF'\n\t`x`\n\tEOF"),
    ("単一引用符の中", "grep -n '`' file"),
    ("$'…' の中", "echo $'a`b'"),
    ("$'…' の中の逃がした引用符", "echo $'a\\'`b'"),
    ("コメント", "x=1 # `comment`"),
    ("行頭のコメント", "# `x` を確かめる\necho ok"),
    ("here-string の単一引用符", "cat <<< '`x` $(y)'"),
    ("算術展開の <<", "echo $((1<<2))"),
    ("Claude Code の既定のコミットの形", "git commit -m \"$(cat <<'EOF'\nfix: `foo`\nEOF\n)\""),
    ("$( ) の中の単一引用符", "X=\"$(gh api x --jq '.body | \"`\"')\""),
    ("引用した heredoc の後の行の $( ) は heredoc の外", "cat <<'EOF'\n`x`\nEOF\necho \"$(date)\""),
    ("バッククォートも $( も無い", "ls -la"),
]

# 解析できない (シェル自身も実行しない) もの: 何も出さない
UNPARSABLE = [
    ("二重引用符が閉じない", 'echo "`x`'),
    ("単一引用符が閉じない", "echo '`x`"),
    ("バッククォートが閉じない", "echo `x"),
    ("$( が閉じない", 'echo "$(cat <<EOF\n`x`\nEOF\n'),
]


class HookTestCase(unittest.TestCase):
    def run_hook(self, command, env=None):
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(**(env or {})),
        )

    def assert_silent(self, command, env=None):
        result = self.run_hook(command, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", f"素通しのはずが止めた: {command!r}")

    def assert_denied(self, command):
        result = self.run_hook(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "", f"止めるはずが素通しした: {command!r}")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny", result.stdout)
        return payload["hookSpecificOutput"]["permissionDecisionReason"]


class DenyTest(HookTestCase):
    def test_置換として解釈される箇所を止める(self):
        for name, command, hint in DENIED:
            with self.subTest(name):
                reason = self.assert_denied(command)
                self.assertIn(hint, reason)

    def test_文面は実害に触れる(self):
        reason = self.assert_denied(INCIDENT)
        self.assertIn("setup#309", reason)
        self.assertIn("mokume-metal/mokume#2052", reason)

    def test_文面は止めた箇所と行を示す(self):
        reason = self.assert_denied(INCIDENT)
        self.assertIn("`make test`", reason)
        self.assertIn("4 行目", reason)
        self.assertIn("heredoc", reason)

    def test_文面は変数を本文の外で渡す形を示す(self):
        reason = self.assert_denied(INCIDENT)
        self.assertIn("os.environ", reason)

    def test_文面は_heredoc_の_置換を挙げない(self):
        # 止めるのはバッククォートだけ。同じ本文の $( ) は意図した差し込みなので箇所に数えない
        reason = self.assert_denied("cat <<EOF\n@ $(git rev-parse --short HEAD)\n`x`\nEOF")
        self.assertIn("3 行目", reason)
        self.assertNotIn("git rev-parse", reason)

    def test_文脈が複数なら打ち直し方を両方示す(self):
        reason = self.assert_denied('git commit -m "`a`" && cat <<EOF\n`b`\nEOF')
        self.assertIn("<<'EOF'", reason)
        self.assertIn("単一引用符で囲む", reason)

    def test_箇所が多ければ残りを数で示す(self):
        reason = self.assert_denied("echo " + " ".join(f'"`x{n}`"' for n in range(8)))
        self.assertIn("ほか 3 箇所", reason)


class AllowTest(HookTestCase):
    def test_置換にならない箇所は素通し(self):
        for name, command in ALLOWED:
            with self.subTest(name):
                self.assert_silent(command)

    def test_解析できなければ何も言わない(self):
        for name, command in UNPARSABLE:
            with self.subTest(name):
                self.assert_silent(command)

    def test_無効化スイッチ(self):
        self.assert_silent(INCIDENT, env={"CLAUDE_SUBSTITUTION_GUARD": "0"})

    def test_壊れた入力では何もしない(self):
        result = subprocess.run(
            [str(SCRIPT)], input="not json", capture_output=True, text=True, timeout=30, env=clean_env()
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_command_が無い入力では何もしない(self):
        result = subprocess.run(
            [str(SCRIPT)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
            capture_output=True,
            text=True,
            timeout=30,
            env=clean_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
