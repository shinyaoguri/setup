#!/usr/bin/env python3
"""Claude Code の PreToolUse フック: 秘密の値が Bash ツールの出力に出るコマンドを止める。

塞ぐ実害は setup#291。Gyazo へのアップロードの失敗を切り分けるつもりで

    T=$(secret-read "$REF" 2>&1 >/dev/null | head -2); echo "stderr: $T"

と打ち、**トークンがツールの出力 (= セッションのトランスクリプト) に出た**。secret-read は
契約どおり stdout にしか出していない。原因は **zsh の MULTIOS** で、Bash ツールのシェルは zsh
なので `cmd >/dev/null | head` は stdout を /dev/null とパイプの**両方**へ複製する
(`echo hi >/dev/null | wc -c` が 3 になる)。bash の感覚で「捨てた」stdout が素通りしていた。

書き方の注意を文書へ足しても、同じ種類の事故 (パイプ・`echo "$T"`・`curl -v`・`set -x`) は
防げない。**値が出力に届くかは、コマンド文字列から機械的に判定できる**のでここで止める。

判定の対象は「値を出す呼び出し」:

  - `secret-read <参照>` / `secret-read --refresh <参照>` (`--check` などの値を出さない形は対象外)
  - `op read …` / `op item get … --reveal`
  - `security find-generic-password` / `find-internet-password` の `-w` / `-g`

**通すのは、それが単独でコマンド置換 `$(…)` になっていて、置換が値を表示しないところに
使われているときだけ:**

  - 値を受け取る道具 (`CONSUMERS`: curl・gh など) の引数
      例: curl -F "access_token=$(secret-read "$REF")" https://…
  - 環境変数の前置 (値を表示するコマンド `ECHOERS` の前は除く)
      例: GH_TOKEN=$(secret-read "$REF") gh api …

引数を任意のコマンドに許さないのは、**多くのコマンドが不正な引数をエラー文にそのまま出す**
から (`ls "$(secret-read R)"` は `ls: <値>: No such file…`)。値を受け取るのが仕事の道具だけを
並べ、足りなければここへ足す。

止めるもの (deny。正しい形が機械的に決まり、その場で打ち直せるので人を呼ばない):

  - 裸の呼び出し (`secret-read R`・`secret-read R | wc -c`) — stdout がそのまま出力に届く
  - 置換の中のパイプ・リダイレクト・複文 (`2>/dev/null` だけは可。stderr に値は出ない)
  - 変数への単独の代入 (`T=$(secret-read R)`) — 後の `echo "$T"` を止められない
  - 上の 2 か所以外での置換 (`echo "$(secret-read R)"`・リダイレクト先・バッククォート・
    クォートなしのヒアドキュメントの本文 — 本文は表示か投稿に回る)
  - `curl -v` / `--trace` (送るヘッダーを stderr に出す)、`set -x` などの xtrace
  - `sh -c` / `eval` に渡す文字列に対象の呼び出しが入っている (中のコードは判定できない)

判定しないもの: シングルクォート・クォート付きのヒアドキュメント・コメントの中の文字列
(PR の本文などで `secret-read` に言及できるように)。

**塞いでいない穴:** スクリプトファイルの中の呼び出し、Bash 以外のツールの経路、値を
受け取った道具が自分のエラー文で値を出すこと (`curl -v` 以外)、実行時に組み立てる文字列
(`eval "$(echo secret-read R)"`)。事故を止めるためのもので、回避しようとする書き方は想定しない。

環境変数:
  CLAUDE_SECRET_OUTPUT_GUARD=0  無効化する

契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
呼び出し口は settings.json の hooks.PreToolUse、テストは
claude/tests/secret_output_guard_test.py (python3 で直接実行)。
"""

import json
import os
import re
import sys

ISSUE = "setup#291"

# 値を受け取るのが仕事の道具。引数に置換を渡してよいのはこれだけ
CONSUMERS = {"curl", "wget", "gh", "http", "https", "xh"}

# 環境変数の前置でも通さない — 受け取った環境や引数を表示する
ECHOERS = {
    "echo", "printf", "print", "printenv", "env", "set", "export", "declare",
    "typeset", "local", "readonly", "cat", "tee",
}

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}

# 語の先頭で読み飛ばすもの (制御構文の予約語と、後ろのコマンドを実行するだけの前置き)
RESERVED = {
    "!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while",
    "until", "for", "case", "esac", "in", "time",
}
WRAPPERS = {"command", "builtin", "exec", "nohup", "nice", "sudo"}

# 値を出さない secret-read のオプション
QUIET_OPTIONS = {"--check", "--help", "-h", "--forget", "--warm"}

ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
XTRACE = re.compile(r"(^|[\s;&|(])set\s+(-[a-wyzA-Z]*x|-o\s+xtrace)")

# 置換の中で許すリダイレクト。stderr を捨てるのは値に触れない
QUIET_REDIRECTS = {("2>", "/dev/null")}

KEYWORDS = ("secret-read", "op", "security")


class Unparsable(Exception):
    pass


class Subst:
    """コマンド置換。`kind` は `$(`・`` ` ``・`<(`・`heredoc`。"""

    def __init__(self, kind):
        self.kind = kind
        self.commands = []  # SimpleCommand のリスト
        self.compound = False  # パイプ・複文・サブシェルを含むか
        self.owner = None  # この置換を含む語を持つ SimpleCommand
        self.place = None  # "word" / "redirect" / "heredoc"
        self.word_index = None


class Word:
    def __init__(self):
        self.parts = []  # str か Subst
        self.quoted = False

    @property
    def text(self):
        return "".join(p for p in self.parts if isinstance(p, str))

    def substs(self):
        return [p for p in self.parts if isinstance(p, Subst)]


class SimpleCommand:
    def __init__(self):
        self.words = []
        self.redirects = []  # (op, Word)
        self.heredoc_substs = []
        self.container = None  # 自分を含む Subst (トップレベルなら None)


class Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.pending_heredocs = []  # (区切り, クォート付きか, <<- か, SimpleCommand)

    # --- 字句 -------------------------------------------------------------------

    def peek(self, offset=0):
        j = self.i + offset
        return self.s[j] if j < len(self.s) else ""

    def parse_list(self, container, stop):
        """`stop` (")" か "") まで読み、SimpleCommand を container に積む。"""
        current = None
        count = 0
        while True:
            c = self.peek()
            if c == "":
                if stop:
                    raise Unparsable("置換が閉じていない")
                break
            if c in " \t":
                self.i += 1
                continue
            if c == "\\" and self.peek(1) == "\n":
                self.i += 2
                continue
            if c == "#" and current is None:
                while self.peek() not in ("", "\n"):
                    self.i += 1
                continue
            if c == "\n":
                self.i += 1
                self.read_heredocs()
                current = None
                continue
            if c == ")" and stop == ")":
                self.i += 1
                break
            if c in ";&|":
                two = self.s[self.i : self.i + 2]
                op = two if two in ("&&", "||", "|&", ";;") else c
                if op == "&" and self.peek(1) == ">":
                    # &> / &>> はリダイレクト
                    current = current or self.new_command(container)
                    self.read_redirect(current)
                    continue
                self.i += len(op)
                if container is not None:
                    container.compound = True
                current = None
                continue
            if c == "(" and current is None:
                # サブシェル。中の文は同じ文脈に並べる
                self.i += 1
                if container is not None:
                    container.compound = True
                self.parse_list(container, ")")
                continue
            if c == ")":
                raise Unparsable("対応しない )")
            if current is None:
                current = self.new_command(container)
                count += 1
            if self.at_redirect():
                self.read_redirect(current)
                continue
            word = self.read_word()
            for subst in word.substs():
                subst.owner = current
                subst.place = "word"
                subst.word_index = len(current.words)
            current.words.append(word)
        if container is not None and count > 1:
            container.compound = True

    def new_command(self, container):
        command = SimpleCommand()
        command.container = container
        self.commands.append(command)
        if container is not None:
            container.commands.append(command)
        return command

    def at_redirect(self):
        j = self.i
        while j < len(self.s) and self.s[j].isdigit():
            j += 1
        if j < len(self.s) and self.s[j] in "<>":
            # <( と >( はプロセス置換 (語の一部)
            return not (j == self.i and j + 1 < len(self.s) and self.s[j + 1] == "(")
        return self.s.startswith("&>", self.i)

    def read_redirect(self, command):
        m = re.compile(r"\d*(<<<|<<-|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)").match(self.s, self.i)
        if not m:
            raise Unparsable("リダイレクトを読めない")
        op = m.group(0)
        self.i = m.end()
        while self.peek() in (" ", "\t"):
            self.i += 1
        word = self.read_word()
        if op.lstrip("0123456789") in ("<<", "<<-"):
            quoted = word.quoted or "\\" in self.s[m.end() : self.i]
            self.pending_heredocs.append((word.text, quoted, op.endswith("-"), command))
        for subst in word.substs():
            subst.owner = command
            subst.place = "redirect"
        command.redirects.append((op, word))

    def read_heredocs(self):
        while self.pending_heredocs:
            delimiter, quoted, strip_tabs, command = self.pending_heredocs.pop(0)
            body_start = self.i
            while True:
                end = self.s.find("\n", self.i)
                line = self.s[self.i :] if end < 0 else self.s[self.i : end]
                candidate = line.lstrip("\t") if strip_tabs else line
                if candidate == delimiter:
                    body = self.s[body_start : self.i]
                    self.i = len(self.s) if end < 0 else end + 1
                    break
                if end < 0:
                    body = self.s[body_start:]
                    self.i = len(self.s)
                    break
                self.i = end + 1
            if not quoted:
                # クォートなしの本文では置換が走る。本文は表示か投稿に回る
                inner = Parser(body)
                inner.commands = self.commands
                for subst in inner.scan_expansions(stop=None):
                    subst.owner = command
                    subst.place = "heredoc"
                    command.heredoc_substs.append(subst)

    def read_word(self):
        word = Word()
        buffer = []

        def flush():
            if buffer:
                word.parts.append("".join(buffer))
                buffer.clear()

        while True:
            c = self.peek()
            if c == "" or c in " \t\n;&|)":
                break
            if c in "<>" and self.peek(1) == "(":
                flush()
                self.i += 2
                subst = Subst("<(")
                self.parse_list(subst, ")")
                word.parts.append(subst)
                continue
            if c in "<>":
                break
            if c == "(":
                # zsh のグロブ修飾子 (`*(.)`) など。語の一部として読む
                buffer.append(c)
                self.i += 1
                continue
            if c == "\\":
                buffer.append(self.peek(1))
                word.quoted = True
                self.i += 2
                continue
            if c == "'":
                end = self.s.find("'", self.i + 1)
                if end < 0:
                    raise Unparsable("シングルクォートが閉じていない")
                buffer.append(self.s[self.i + 1 : end])
                word.quoted = True
                self.i = end + 1
                continue
            if c == "$" and self.peek(1) == "'":
                self.i += 2
                while self.peek() not in ("", "'"):
                    if self.peek() == "\\":
                        self.i += 1
                    buffer.append(self.peek())
                    self.i += 1
                if self.peek() == "":
                    raise Unparsable("$'…' が閉じていない")
                self.i += 1
                word.quoted = True
                continue
            if c == '"':
                self.i += 1
                word.quoted = True
                flush()
                word.parts.extend(self.scan_expansions(stop='"', collect_text=True))
                continue
            if c in "$`":
                # read_expansion が読んだ分だけ進める。置換でなければ `$` を字として残す
                expansion = self.read_expansion()
                if expansion is not None:
                    flush()
                    word.parts.append(expansion)
                else:
                    buffer.append(c)
                continue
            buffer.append(c)
            self.i += 1
        flush()
        return word

    def read_expansion(self):
        """`$(`・`` ` ``・`${`・`$((` を読む。置換なら Subst、それ以外は None (読み進めてある)。"""
        if self.peek() == "`":
            end = self.i + 1
            while end < len(self.s) and self.s[end] != "`":
                end += 2 if self.s[end] == "\\" else 1
            if end >= len(self.s):
                raise Unparsable("バッククォートが閉じていない")
            subst = Subst("`")
            inner = Parser(self.s[self.i + 1 : end])
            inner.commands = self.commands
            inner.parse_list(subst, "")
            self.i = end + 1
            return subst
        if self.s.startswith("$((", self.i):
            depth, j = 0, self.i + 1
            while j < len(self.s):
                if self.s[j] == "(":
                    depth += 1
                elif self.s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            self.i = j + 1
            return None
        if self.s.startswith("$(", self.i):
            self.i += 2
            subst = Subst("$(")
            self.parse_list(subst, ")")
            return subst
        if self.s.startswith("${", self.i):
            self.i += 2
            # ${…} の中の置換は拾うが、どこに使われるかは分からないので語の外扱い
            for subst in self.scan_expansions(stop="}"):
                subst.place = "parameter"
            return None
        self.i += 1
        return None

    def scan_expansions(self, stop, collect_text=False):
        """ダブルクォート・ヒアドキュメント・${…} の中身を読み、置換を返す。"""
        parts = []
        text = []
        while True:
            c = self.peek()
            if c == "":
                if stop is None:
                    break
                raise Unparsable(f"{stop} が閉じていない")
            if stop is not None and c == stop:
                self.i += 1
                break
            if c == "\\":
                text.append(self.peek(1))
                self.i += 2
                continue
            if stop == "}" and c in "'\"":
                # ${X:-"…"} の中のクォート
                if c == "'":
                    end = self.s.find("'", self.i + 1)
                    if end < 0:
                        raise Unparsable("シングルクォートが閉じていない")
                    self.i = end + 1
                else:
                    self.i += 1
                    parts.extend(p for p in self.scan_expansions(stop='"') if isinstance(p, Subst))
                continue
            if c in "$`":
                expansion = self.read_expansion()
                if expansion is not None:
                    if text:
                        parts.append("".join(text))
                        text.clear()
                    parts.append(expansion)
                continue
            text.append(c)
            self.i += 1
        if text:
            parts.append("".join(text))
        if collect_text:
            return parts
        return [p for p in parts if isinstance(p, Subst)]

    def parse(self):
        self.commands = []
        self.parse_list(None, "")
        self.read_heredocs()
        return self.commands


# --- 判定 -----------------------------------------------------------------------


def command_word(command):
    """(コマンド名, 残りの語, 前置の代入の語) を返す。コマンドが無ければ名前は None。"""
    words = command.words
    i = 0
    assignments = []
    while i < len(words):
        text = words[i].text
        first = words[i].parts[0] if words[i].parts else ""
        if isinstance(first, str) and ASSIGNMENT.match(first):
            assignments.append(i)
            i += 1
            continue
        name = os.path.basename(text)
        if text in RESERVED or name in WRAPPERS:
            i += 1
            continue
        if name == "env":
            # env の後ろにコマンドがあれば、そちらが本体
            j = i + 1
            while j < len(words) and (words[j].text.startswith("-") or ASSIGNMENT.match(words[j].text)):
                if ASSIGNMENT.match(words[j].text):
                    assignments.append(j)
                j += 1
            if j < len(words):
                i = j
                continue
        return name, words[i + 1 :], assignments, i
    return None, [], assignments, None


def value_read(command):
    """値を出す呼び出しなら、その説明を返す。"""
    name, rest, _, _ = command_word(command)
    args = [w.text for w in rest]
    if name == "secret-read":
        if args and args[0] in QUIET_OPTIONS:
            return None
        return "secret-read"
    if name == "op":
        plain = [a for a in args if not a.startswith("-")]
        if plain[:1] == ["read"]:
            return "op read"
        if plain[:2] == ["item", "get"] and "--reveal" in args:
            return "op item get --reveal"
        return None
    if name == "security":
        if args and args[0] in ("find-generic-password", "find-internet-password") and (
            "-w" in args or "-g" in args
        ):
            return f"security {args[0]} -w"
    return None


def curl_verbose(rest):
    for word in rest:
        text = word.text
        if text in ("--verbose", "--trace", "--trace-ascii"):
            return True
        if re.fullmatch(r"-[A-Za-z]+", text) and "v" in text[1:]:
            return True
    return False


def string_mentions_read(words):
    text = " ".join(w.text for w in words)
    return re.search(r"(^|[^\w-])(secret-read|op\s+read)\b", text) is not None


ALLOWED_FORM = (
    "値は `$(secret-read \"$REF\")` の形で、値を受け取る道具 (curl・gh など) の引数か、"
    "環境変数の前置 (`GH_TOKEN=$(secret-read \"$REF\") gh …`) にだけ渡す。"
    "キャッシュの有無を確かめたいだけなら `secret-read --check` (値を出さない)。"
)


def judge_read(command, what):
    """値を出す呼び出し 1 つを判定し、止める理由を返す (通すなら None)。"""
    subst = command.container
    if subst is None:
        return (
            f"`{what}` の出力がそのまま Bash ツールの出力 (= トランスクリプト) に届く。"
            "パイプで数えるだけでも、zsh の MULTIOS で値が複製されることがある "
            "(`cmd >/dev/null | head` は stdout を両方へ流す)。"
        )
    if subst.kind == "`":
        return "バッククォートの置換は判定しない。`$(…)` で書き直す。"
    if subst.kind not in ("$(", "<("):
        return f"`{what}` の置換を判定できない位置で使っている。"
    if subst.compound:
        return (
            f"`{what}` を含む置換の中にパイプや複文がある。置換の中身は `{what}` だけにする "
            "(zsh の MULTIOS では `>/dev/null` と書いても stdout がパイプへも流れる)。"
        )
    for op, target in command.redirects:
        if (op, target.text) not in QUIET_REDIRECTS:
            return f"`{what}` にリダイレクト `{op}` が付いている。付けてよいのは `2>/dev/null` だけ。"
    if subst.place == "heredoc":
        return "ヒアドキュメントの本文に値を埋め込んでいる。本文は表示か投稿に回る。"
    if subst.place != "word" or subst.owner is None:
        return f"`{what}` の置換を、引数でも環境変数の前置でもない所 (リダイレクト先・${{…}} の中) で使っている。"

    owner = subst.owner
    name, rest, assignments, _ = command_word(owner)
    if subst.word_index in assignments:
        if name is None:
            return (
                "値を変数へ代入している (`T=$(secret-read …)`)。後から `echo \"$T\"` などで"
                "出力に出るのを止められない。"
            )
        if name in ECHOERS:
            return f"`{name}` は受け取った環境や引数を表示する。"
        if name in SHELLS and any(w.text.startswith("-") and "x" in w.text for w in rest):
            return "シェルを -x で起動しているので、展開した値が表示される。"
        return None
    if name not in CONSUMERS:
        return (
            f"値を `{name}` の引数に渡している。値を受け取る道具 ({', '.join(sorted(CONSUMERS))}) "
            "以外は、不正な引数をエラー文にそのまま出すことがある (`ls \"$(secret-read R)\"` は"
            "値を含むエラーを出す)。受け取るのが仕事の道具なら "
            "claude/secret-output-guard.py の CONSUMERS に足す。"
        )
    if name == "curl" and curl_verbose(rest):
        return "`curl -v` / `--trace` は送るヘッダーやフォームを stderr に出す。"
    return None


def judge(command_text):
    parser = Parser(command_text)
    commands = parser.parse()
    reasons = []
    reads = 0
    for command in commands:
        what = value_read(command)
        name, rest, _, _ = command_word(command)
        if what is None:
            if (name in SHELLS and any(w.text == "-c" for w in rest) or name == "eval") and string_mentions_read(rest):
                reasons.append(
                    f"`{name}` に渡す文字列の中で秘密を読んでいる。文字列の中のコードは判定できないので、"
                    "直接打つ形に書き直す。"
                )
            continue
        reads += 1
        reason = judge_read(command, what)
        if reason:
            reasons.append(reason)
    if reads and XTRACE.search(command_text):
        reasons.append("xtrace (`set -x`) が有効だと、展開した値がそのまま表示される。")
    return reasons


def emit_deny(reasons):
    body = "\n".join(f"- {r}" for r in dict.fromkeys(reasons))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"秘密の値が Bash ツールの出力に出る形になっている ({ISSUE})。\n{body}\n\n{ALLOWED_FORM}"
                    ),
                }
            },
            ensure_ascii=False,
        )
    )


def main():
    if os.environ.get("CLAUDE_SECRET_OUTPUT_GUARD", "1") == "0":
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not any(keyword in command for keyword in KEYWORDS):
        return
    try:
        reasons = judge(command)
    except (Unparsable, IndexError, RecursionError) as error:
        # 区切れないコマンドは本当に呼び出しがあるときだけ止める (シェルも実行できない形のはず)
        if re.search(r"(^|[^\w-])(secret-read\s+(?!--(check|help|forget|warm)\b)|op\s+read\b)", command):
            reasons = [f"コマンドを区切れず ({error})、秘密の値の行き先を確かめられない。"]
        else:
            return
    if reasons:
        emit_deny(reasons)


if __name__ == "__main__":
    main()
