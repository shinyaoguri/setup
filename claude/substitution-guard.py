#!/usr/bin/env python3
"""Claude Code の PreToolUse フック: 文字列のつもりの箇所がコマンド置換として実行されるのを止める。

止めるのはコマンド置換のうち**バッククォート**だけで、`$( )` はどの文脈でも止めない (下の実測)。

塞ぐ実害は setup#309。mokume の Issue を作業していたセッションが、Issue の本文を書き換える
ために次の形のコマンドを打った。

    gh issue view 2007 --json body --jq .body > $S/body.md; python3 - <<EOF
    ...
    new=\"\"\"## 完了条件
    ... `make test` と `make test-release` の `swift test` は … \"\"\"
    EOF

区切りの EOF を引用符で囲んでいないので、シェルは本文を展開する。Markdown のつもりで書いた
インラインコードがコマンド置換になり、`make test` (13 分) → `make test-release` が書かれた順に
実行された。120 秒で終わらずバックグラウンドへ回ったのでセッションからは見えず、GPU を塞いだ
まま 2026-10-04 13:58 (JST) に手元機がカーネルパニックで再起動した (mokume-metal/mokume#2052)。
起動元を割り出すのに約 1 時間かかっている。

**同じ根は広い。** 手元の全セッションの記録 (Bash の tool_use 5,522 件) を走査すると、引用符なしの
heredoc の本文にバッククォートが 117 件・`$(` が 12 件、二重引用符の引数 (`git commit -m "…"`・
`gh … --body "…"`) の中にバッククォートが 107 件あった。二重引用符の側では、コミットメッセージや
コメントの中身が黙って実行結果に置き換わりうる。

上の件数は字面での粗い数え方で、逃がした `\\`` や引用した heredoc の本文も含む。このガードの
字句解析で同じ時点の記録 (サブエージェントを含む Bash 44,149 件) を数え直すと、実際に置換になる
形は 17 件だった。

  - 事故 7 件は**すべてバッククォート**だった。heredoc の本文が 3 件 (上の実害を含む)、二重引用符の
    中が 4 件 (`python3 -c "…"` の中の DocC の ``…`` や `grep "…"` のパターンが黙って崩れていた)
  - heredoc の本文の `$(` 10 件は**すべて意図した用途**だった (`$(git rev-parse --short HEAD)`・
    `$(cat $S/top.txt)` のように、本文へ値を差し込むために書いたもの。バッククォートの側は `\\``
    で逃がしてあったものまである)

だから `$(` は止めない。Markdown が `$(` を書くことはまず無く、事故の形はバッククォートに集まる。
止めれば意図した 10 件を毎回書き直させる代償だけが残る。

**見るのは「シェルが置換として解釈するか」であって、字面ではない。** だから正規表現 1 本では
なく、引用・逃がし・heredoc を追う小さな字句解析で読む。判定:

  バッククォート
    引用符の外                                   → deny (置換のつもりなら `$(…)` に書く)
    二重引用符の中・`${…}` の中                   → deny
    引用符なしの heredoc (`<<EOF`・`<<-EOF`) の本文 → deny
    単一引用符・`$'…'` の中                       → 素通し
    区切りを引用した heredoc (`<<'EOF'`・`<<"EOF"`・`<<\\EOF`・`<<E"OF"`) の本文
                                                 → 素通し
    バックスラッシュで逃がしたもの (`\\``)        → 素通し (単一引用符の中の `\\` は逃がしでない)
    コメント (語の先頭の `#` から行末)            → 素通し
  `$(`
    引用符の外・二重引用符の中                    → 素通し (`GH_TOKEN="$(cmd)"` は正当な用途)。
                                                    中身はシェルのコードとして同じ規則で読み直す
    引用符なしの heredoc の本文                   → 素通し (実測で事故 0 件・意図した用途 10 件)
  here-string (`<<<`)                            → heredoc ではない。続く語を普通の語として読む
  `$VAR`・`${VAR}` だけの heredoc                 → 素通し

`$( )` の中は入れ子のシェルとして読み直すので、`git commit -m "$(cat <<'EOF' … EOF\\n)"` の形
(本文に Markdown のバッククォートを含む) は素通しになり、区切りの引用を外すと deny になる。

**`$( )` を止めないのは、意図して書く形だからである。** 実害はどれも「文字列のつもりの場所」に
書いたバッククォートで起きている。引用符の外のバッククォートは置換の意図で書かれたものもあるが、`$(…)` へ
書き換えれば同じ意味で通るので、止めても打ち直しは 1 語で済む。

deny なのは、正しい形が機械的に決まってその場で打ち直せるから (人を呼ぶ必要がない)。差し戻しの
文面は文脈ごとに打ち直せる形を出す: heredoc なら区切りを `<<'EOF'` に、二重引用符なら単一引用符か
ファイル経由 (Write ツール → `--body-file` / `-F`) に、変数を差し込みたいなら本文の外で渡す。

**解析できないコマンド (引用・置換が閉じない) には何も言わない** (fail-open)。シェル自身も構文誤りで
実行しないので、置換が走ることはない。

**塞いでいない穴:** `case … in a) …` を `$( )` の中に書くと、パターンの `)` で置換が閉じたと読む。
`eval`・`bash -c '…'`・`sh -c` に単一引用符で渡した文字列は、内側でもう一度シェルが読むが、
ここでは単一引用符の中として素通しにする。スクリプトファイルの中身は見ない。

環境変数:
  CLAUDE_SUBSTITUTION_GUARD=0  無効化する

契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
呼び出し口は settings.json の hooks.PreToolUse、テストは
claude/tests/substitution_guard_test.py (python3 で直接実行)。
"""

import json
import os
import sys

# 単語の先頭とみなす直前の字 (`#` がコメントになる位置)
WORD_BREAK = set(" \t\n;&|()<>")
HEREDOC_WORD_END = set(" \t\n;&|()<>")


class Unparsable(Exception):
    pass


class Finding:
    """置換として解釈されるバッククォート。`where` は bare / dquote / heredoc。"""

    def __init__(self, where, line, snippet, delimiter=None):
        self.where = where
        self.line = line
        self.snippet = snippet
        self.delimiter = delimiter


class Scanner:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.findings = []
        # (区切り, 引用されているか, `<<-` か)
        self.pending = []

    # --- 道具 -----------------------------------------------------------------

    def peek(self, offset=0):
        j = self.i + offset
        return self.s[j] if 0 <= j < len(self.s) else ""

    def line_of(self, index):
        return self.s.count("\n", 0, index) + 1

    def snippet_at(self, start, end):
        text = self.s[start:end].replace("\n", "⏎")
        return text if len(text) <= 60 else text[:57] + "…"

    def add(self, where, start, end, delimiter=None):
        self.findings.append(
            Finding(where, self.line_of(start), self.snippet_at(start, end), delimiter)
        )

    # --- 文脈ごとの読み -----------------------------------------------------------

    def code(self, stop=None):
        """シェルのコードとして読む。stop が ")" なら対応する `)` で戻る (`$(`・`<(` の中)。"""
        depth = 0
        while True:
            c = self.peek()
            if c == "":
                if stop:
                    raise Unparsable("置換が閉じていない")
                self.read_heredocs()
                return
            if c == "\\":
                self.i += 2
                continue
            if c == "\n":
                self.i += 1
                self.read_heredocs()
                continue
            if c == "#" and (self.i == 0 or self.s[self.i - 1] in WORD_BREAK):
                while self.peek() not in ("", "\n"):
                    self.i += 1
                continue
            if c == "'":
                self.single_quote()
                continue
            if c == '"':
                self.i += 1
                self.double_quote()
                continue
            if c == "`":
                self.backtick("bare")
                continue
            if c == "$":
                self.dollar("bare")
                continue
            if c == "<" and self.s.startswith("<<<", self.i):
                # here-string。続く語は普通の語として読む
                self.i += 3
                continue
            if c == "<" and self.peek(1) == "<":
                self.heredoc_operator()
                continue
            if c in "<>" and self.peek(1) == "(":
                self.i += 2
                self.code(stop=")")
                continue
            if c == "(":
                depth += 1
                self.i += 1
                continue
            if c == ")":
                self.i += 1
                if depth > 0:
                    depth -= 1
                elif stop == ")":
                    return
                continue
            self.i += 1

    def single_quote(self):
        end = self.s.find("'", self.i + 1)
        if end < 0:
            raise Unparsable("単一引用符が閉じていない")
        self.i = end + 1

    def ansi_c_quote(self):
        """`$'…'`。中の `\\` は次の 1 字を逃がす (`\\'` で閉じない)。"""
        self.i += 2
        while True:
            c = self.peek()
            if c == "":
                raise Unparsable("$'…' が閉じていない")
            if c == "\\":
                self.i += 2
                continue
            self.i += 1
            if c == "'":
                return

    def double_quote(self, stop='"'):
        """二重引用符の中 (開きの `"` は読んである)。stop が "}" なら `${…}` の中。"""
        brace = 0
        while True:
            c = self.peek()
            if c == "":
                raise Unparsable(f"{stop} が閉じていない")
            if c == "\\":
                self.i += 2
                continue
            if stop == "}" and c == "{":
                brace += 1
            if c == stop:
                self.i += 1
                if stop == "}" and brace > 0:
                    brace -= 1
                    continue
                return
            if stop == "}" and c == '"':
                self.i += 1
                self.double_quote()
                continue
            if c == "`":
                self.backtick("dquote")
                continue
            if c == "$":
                self.dollar("dquote")
                continue
            self.i += 1

    def backtick(self, where):
        """`` ` `` から対になる `` ` `` までを 1 つの置換として記録する。"""
        start = self.i
        end = self.i + 1
        while end < len(self.s) and self.s[end] != "`":
            end += 2 if self.s[end] == "\\" else 1
        if end >= len(self.s):
            raise Unparsable("バッククォートが閉じていない")
        self.add(where, start, end + 1)
        self.i = end + 1

    def dollar(self, where):
        if self.s.startswith("$((", self.i):
            self.arithmetic()
        elif self.s.startswith("$(", self.i):
            # 引用符の外・二重引用符の中の $( ) は意図して書く形。中は入れ子のシェルとして読む
            self.i += 2
            self.code(stop=")")
        elif self.s.startswith("${", self.i):
            self.i += 2
            self.double_quote(stop="}")
        elif self.s.startswith("$'", self.i) and where == "bare":
            self.ansi_c_quote()
        else:
            self.i += 1

    def arithmetic(self):
        depth, j = 0, self.i + 1
        while j < len(self.s):
            if self.s[j] == "(":
                depth += 1
            elif self.s[j] == ")":
                depth -= 1
                if depth == 0:
                    self.i = j + 1
                    return
            j += 1
        raise Unparsable("$(( が閉じていない")

    # --- heredoc -------------------------------------------------------------------

    def heredoc_operator(self):
        self.i += 2
        strip_tabs = self.peek() == "-"
        if strip_tabs:
            self.i += 1
        while self.peek() in (" ", "\t"):
            self.i += 1
        delimiter, quoted = [], False
        while True:
            c = self.peek()
            if c == "" or c in HEREDOC_WORD_END:
                break
            if c == "\\":
                quoted = True
                delimiter.append(self.peek(1))
                self.i += 2
            elif c in "'\"":
                end = self.s.find(c, self.i + 1)
                if end < 0:
                    raise Unparsable("heredoc の区切りの引用が閉じていない")
                quoted = True
                delimiter.append(self.s[self.i + 1 : end])
                self.i = end + 1
            else:
                delimiter.append(c)
                self.i += 1
        if not delimiter:
            raise Unparsable("heredoc の区切りが無い")
        self.pending.append(("".join(delimiter), quoted, strip_tabs))

    def read_heredocs(self):
        """改行の直後で、溜まっている heredoc の本文を順に読む。"""
        while self.pending:
            delimiter, quoted, strip_tabs = self.pending.pop(0)
            body_start = self.i
            body_end = len(self.s)
            while self.i < len(self.s):
                end = self.s.find("\n", self.i)
                line = self.s[self.i :] if end < 0 else self.s[self.i : end]
                candidate = line.lstrip("\t") if strip_tabs else line
                if candidate == delimiter:
                    body_end = self.i
                    self.i = len(self.s) if end < 0 else end + 1
                    break
                self.i = len(self.s) if end < 0 else end + 1
            if not quoted:
                self.heredoc_body(body_start, body_end, delimiter)

    def heredoc_body(self, start, end, delimiter):
        """引用符なしの heredoc の本文。`\\` は次の 1 字を逃がし、引用符は字として残る。

        `$( )` は止めない (意図して書く形)。中のバッククォートは置換なので、そのまま拾う。
        """
        j = start
        while j < end:
            c = self.s[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                close = j + 1
                while close < end and self.s[close] != "`":
                    close += 2 if self.s[close] == "\\" else 1
                stop = min(close + 1, end)
                self.add("heredoc", j, stop, delimiter)
                j = stop
                continue
            j += 1


def findings_of(command):
    """置換として解釈される箇所の一覧。解析できなければ Unparsable。"""
    scanner = Scanner(command)
    scanner.code()
    return scanner.findings


# --- 差し戻しの文面 -------------------------------------------------------------------

INCIDENT = (
    "setup#309 では、Issue 本文を書き換える `python3 - <<EOF` の本文にあった Markdown の "
    "`make test` がそのまま実行され、GPU を塞いだまま手元機がカーネルパニックで再起動した "
    "(mokume-metal/mokume#2052)。"
)

ADVICE = {
    "heredoc": (
        "引用符なしの heredoc の本文はシェルが展開する。区切りを引用して `<<'{delimiter}'` に"
        "すれば本文は一字も展開されない。変数を差し込みたいなら本文の外で渡す "
        "(例: `S=\"$S\" python3 - <<'{delimiter}'` として本文では os.environ[\"S\"] を読む・"
        "`python3 - \"$S\" <<'{delimiter}'` として sys.argv[1] を読む)。"
        "本文が長い・Markdown なら Write ツールでファイルに書き、`--body-file <ファイル>` / "
        "`-F <ファイル>` で渡す。"
    ),
    "dquote": (
        "二重引用符の中のバッククォートはコマンド置換になる。文字列のつもりなら単一引用符で囲む "
        "(`git commit -m 'fix: `foo` を直す'`)。本文が長い・Markdown・単一引用符を含むなら "
        "Write ツールでファイルに書き、`git commit -F <ファイル>` / `gh … --body-file <ファイル>` "
        "で渡す。字として 1 つだけ要るなら `\\`` と逃がす。置換のつもりなら `$(…)` に書く。"
    ),
    "bare": (
        "引用符の外のバッククォートはコマンド置換になる。置換のつもりなら `$(…)` に書き換える "
        "(意味は同じで、このガードを通る)。字のつもりなら単一引用符で囲むか `\\`` と逃がす。"
    ),
}

WHERE_LABEL = {
    "heredoc": "引用符なしの heredoc の本文",
    "dquote": "二重引用符の中",
    "bare": "引用符の外",
}


def reason_for(findings):
    lines = ["文字列のつもりかもしれない箇所を、シェルがコマンド置換として実行する形になっている。"]
    seen = []
    for finding in findings:
        if len(seen) >= 5:
            break
        label = WHERE_LABEL[finding.where]
        lines.append(f"  - {finding.line} 行目・{label}: {finding.snippet}")
        seen.append(finding)
    if len(findings) > len(seen):
        lines.append(f"  - ほか {len(findings) - len(seen)} 箇所")
    lines.append("")
    advised = set()
    for finding in findings:
        if finding.where in advised:
            continue
        advised.add(finding.where)
        delimiter = finding.delimiter or "EOF"
        lines.append(ADVICE[finding.where].format(delimiter=delimiter))
    lines.append("")
    lines.append(INCIDENT)
    return "\n".join(lines)


def main():
    if os.environ.get("CLAUDE_SUBSTITUTION_GUARD", "1") == "0":
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "`" not in command:
        return
    try:
        findings = findings_of(command)
    except (Unparsable, RecursionError):
        # シェル自身も構文誤りで実行しない形。置換は走らないので何も言わない
        return
    if not findings:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason_for(findings),
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
