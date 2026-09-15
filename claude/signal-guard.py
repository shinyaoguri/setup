#!/usr/bin/env python3
"""Claude Code の PreToolUse フック: 他のセッションのプロセスへシグナルを送る事故を止める。

塞ぐ実害は setup#150。mokume の作業セッションが、自分で起動した `mokume watch` を止める
つもりで `ps … | awk '…mokume…' | head -1` から PID を拾い、**別セッションの
`mokume mcp` (MCP サーバ) に SIGINT を送って止めた**。同じ Mac には常に並行セッションが
居て、同じ名前のプロセスを持つ — このとき `mokume` という名前のプロセスは 8 本あり、
自分のものは 1 本だけだった。止められたセッションは MCP ツールを失った。

**名前では持ち主が分からないが、親子関係なら分かる。** Bash ツールのシェルもフックも、
そのセッションの `claude` プロセスの子として起動される (実測)。

    mokume watch (13728) → zsh (13701, shell-snapshots/…) → claude (88906)  自分が Bash で立てた
    mokume mcp   (88920) → claude (88906)                                   自分の MCP サーバ
    mokume mcp   (5682)  → claude (5666)                                    別セッションの MCP サーバ

だから「対象の一番近い `claude` の祖先が、このフックの一番近い `claude` の祖先と同じか」を
見れば、送り先が誰のものかが決まる。session_id は claude の引数に載っていないので、
**自分のセッション = フック自身の一番近い claude の祖先**とする。

判定 (1 つのコマンドに送り先が複数あれば、いちばん強いものを返す):

  - `kill -l`                                          → 素通し
  - `pkill` / `killall` / `xargs kill` / 送り先が数字でない (`$P`・`$(…)`・`%1`)
                                                       → deny (名前や変数では持ち主を確かめられない)
  - 送り先が 0 以下 (プロセスグループ)                   → deny
  - 数字の PID:
      存在しない                                        → 素通し (kill 自身が失敗する)
      自分の claude そのもの・フックの祖先              → deny
      一番近い claude が自分と別                        → deny (他セッションのもの)
      自分の claude の直下が Bash ツールのシェル         → 素通し (自分が立てたもの)
      自分の claude の直下がそれ以外                    → ask (MCP サーバか、exec で置き換えた
                                                          自分の背面プロセスか区別できない)
      どの claude にも属さない (孤児) → **出所で見直す** (下記):
        出所にこのセッションの session_id               → allow
        出所がこのセッションの作業ディレクトリ配下       → allow / 同じ所を開いた別セッションが
                                                          居れば ask
        どちらでもない                                  → ask (人が端末から立てたもの・別の worktree)

**孤児は「他人のもの」ではなく「鎖が切れた自分のもの」であることが多い** (setup#153)。スケッチは
Bash ツールのシェルから起動されるので、そのシェルが終わると launchd に再親付けされて `ppid=1` に
なる。親子関係だけを見ていると、自分で立てたプロセスが時間の経過だけで「判定できない」側へ移り、
止めるたびに人を呼ぶことになる (実測: ask 20 件のうち 13 件がこれで、正体はすべて自分のスケッチ
だった)。

**持ち主は鎖が切れても出所のパスに残っている。** 引数か cwd に、そのセッションの scratchpad
(`/…/<session_id>/…`) か作業ディレクトリが現れる。PreToolUse の payload は `session_id` と `cwd` を
渡してくるので突き合わせられる。session_id は scratchpad が 1 セッション 1 つなので一意だが、
**作業ディレクトリは一意ではない** — 同じ worktree を複数のセッションが開くことは実在する
(実測で 1 つの worktree の scratchpad に session_id が 2 つ在った) ので、その場合は ask へ戻す。
  - 自分の claude が見つからない                        → 数字の PID は ask (黙って素通しにしない)

**シグナル 0 (`kill -0`) も例外にしない。** 何も起こさないが、例外にしないことで、このフックが
効いているかを他セッションへ本物のシグナルを打たずに確かめられる。存在確認は `ps` で足りる。

deny が多いのは、正しい形が機械的に決まってその場で打ち直せるから (人を呼ぶ必要がない):
PID を出すコマンドだけを先に打ち、確かめた数字で `kill <PID>` を送り直せばよい。
ask は持ち主を判定できないときだけで、判定できない以上は人に返す。

**自分の claude の直接の子は ask にとどめる。** このセッションの MCP サーバも、背面実行で
`exec` を使った自分のプロセス (シェルが置き換わり、シェルの印ごと消える) も、claude の直接の
子として同じ形に見える。環境変数で分けようにも、`/bin/sleep` のような OS 同梱のバイナリは
SIP により外から環境を読めない (実測で 0 件)。区別できないものを deny にすると自分の
プロセスを止められなくなり、素通しにすると自分の MCP サーバを守れないので、人に返す。
背面実行で `exec` を使わなければ、自分のプロセスはシェルの印の下に残って素通しになる。

**塞いでいない穴:** `kill` 系のコマンドを経由しない送り方 (`python3 -c 'os.kill(…)'`・
`osascript -e 'quit app …'`・`launchctl`・スクリプトファイルの中の kill) と、Bash 以外の経路。
見ているのはコマンド文字列に現れる `kill` / `pkill` / `killall` だけである。

環境変数:
  CLAUDE_SIGNAL_GUARD=0  無効化する
  SIGNAL_GUARD_PS        プロセス表をファイルから読む (テスト用。1 行 1 プロセスの
                         タブ区切り: pid, ppid, 起動時刻, comm, args, cwd)。
                         cwd の列を書くと `lsof` を呼ばずにそれを使う
  SIGNAL_GUARD_SELF      祖先を辿り始める PID (テスト用。既定はこのプロセス)

契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
呼び出し口は settings.json の hooks.PreToolUse、テストは
claude/tests/signal_guard_test.py (python3 で直接実行)。
"""

import json
import os
import re
import shlex
import subprocess
import sys

KILLERS = {"kill"}
BY_NAME = {"pkill", "killall"}
# 区切りの先頭で読み飛ばす語 (後ろに本物のコマンドが続くもの)
PREFIXES = {"sudo", "command", "exec", "nohup", "time", "builtin", "env"}
TOOL_SHELL_MARK = "/shell-snapshots/snapshot-"
SEPARATORS = {";", "&", "&&", "|", "||", "(", ")", "|&", ";;"}
REDIRECTS = {"<", ">", ">>", "<<", ">&", "<&", "&>", ">|"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
LOOSE_KILL = re.compile(r"(^|[\s;&|(`])(?:\S*/)?(kill|pkill|killall)(\s|$)")

RANK = {None: 0, "allow": 1, "ask": 2, "deny": 3}


class Verdict:
    def __init__(self):
        self.decision = None
        self.reasons = []

    def add(self, decision, reason):
        if RANK[decision] > RANK[self.decision]:
            self.decision = decision
        if decision:
            self.reasons.append(reason)


def emit(verdict):
    if verdict.decision is None:
        return
    reason = "\n\n".join(dict.fromkeys(verdict.reasons))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": verdict.decision,
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


# --- コマンドから送り先を拾う ---------------------------------------------------


def strip_heredocs(command):
    """heredoc の本文を落とす。本文はコマンドではない (スクリプトの中の kill は対象外)。"""
    lines = command.split("\n")
    kept, waiting = [], []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        kept.append(line)
        waiting.extend(match.group(2) for match in HEREDOC.finditer(line))
    return "\n".join(kept)


def tokens_of(command):
    text = strip_heredocs(command).replace("\\\n", " ")
    # 改行はコマンドの区切り。引用の中の改行は語の中に残るだけで区切りにはならない
    lexer = shlex.shlex(text.replace("\n", " ;\n "), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def segments_of(tokens):
    segment = []
    for token in tokens:
        if token in SEPARATORS:
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


def drop_redirects(words):
    result, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        if word in REDIRECTS:
            # `2>/dev/null` は `2` `>` `/dev/null` に割れる。直前の fd 番号は送り先ではない
            if result and result[-1] in {"0", "1", "2"}:
                result.pop()
            skip = True
            continue
        result.append(word)
    return result


def command_word(words):
    """区切りの本当のコマンドの位置 (環境変数の代入と sudo などを読み飛ばす)。"""
    index = 0
    while index < len(words):
        word = words[index]
        name = os.path.basename(word)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
            index += 1
        elif name in PREFIXES:
            index += 1
            # sudo -n / env -i のような選択肢も読み飛ばす
            while index < len(words) and words[index].startswith("-"):
                index += 1
        else:
            return index
    return None


def kill_targets(arguments):
    """kill の引数から送り先を拾う。`kill -l` なら None を返す。"""
    targets, signal_given, options_done = [], False, False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if not options_done and word == "--":
            options_done = True
        elif not options_done and word in {"-l", "-L"}:
            return None
        elif not options_done and word in {"-s", "-n"}:
            signal_given = True
            index += 1
        elif not options_done and word.startswith("-") and not signal_given:
            signal_given = True
        else:
            targets.append(word)
        index += 1
    return targets


# --- プロセス表 ------------------------------------------------------------------


def using_fixture():
    """プロセス表が差し替えられているか (テスト)。差し替え中は `lsof` を呼ばない。"""
    return bool(os.environ.get("SIGNAL_GUARD_PS", ""))


def process_table():
    """{pid: (ppid, 起動時刻, comm, args, cwd)}。cwd は引けていなければ空。"""
    fixture = os.environ.get("SIGNAL_GUARD_PS", "")
    table = {}
    if fixture:
        with open(fixture, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                pid, ppid, started, comm, args, cwd = (
                    line.rstrip("\n").split("\t") + [""] * 6
                )[:6]
                table[int(pid)] = (int(ppid), started, comm, args, cwd)
        return table
    heads = subprocess.run(
        ["ps", "-axww", "-o", "pid=,ppid=,lstart=,comm="], capture_output=True, text=True
    ).stdout
    for line in heads.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid, started, comm = parts[0], parts[1], " ".join(parts[2:7]), parts[7]
        table[int(pid)] = (int(ppid), started, comm, "", "")
    bodies = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True).stdout
    for line in bodies.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) in table:
            ppid, started, comm, _, cwd = table[int(parts[0])]
            table[int(parts[0])] = (ppid, started, comm, parts[1], cwd)
    return table


def cwds_of(table, pids):
    """{pid: cwd}。プロセス表が持っていなければ `lsof` で一度にまとめて引く。

    `ps` は cwd を出せないので、要るときだけ引く (孤児の出所を見るときだけ)。
    """
    known, unknown = {}, []
    for pid in pids:
        entry = table.get(pid)
        if entry is None:
            continue
        if entry[4]:
            known[pid] = entry[4]
        elif not using_fixture():
            unknown.append(pid)
    if not unknown:
        return known
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(pid) for pid in unknown)],
            capture_output=True,
            text=True,
        ).stdout
    except OSError:  # lsof が無い環境では cwd 無しで判定する (出所が減るだけ)
        return known
    current = None
    for line in out.splitlines():
        if line.startswith("p"):
            current = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and current is not None:
            known.setdefault(current, line[1:])
    return known


def under(path, root):
    """path が root 自身か、その配下か。文字列の前方一致ではなくパスの境界で見る。"""
    if not path or not root:
        return False
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def mentions(args, root):
    """コマンド行のどれかの語が root 自身かその配下を指しているか。"""
    return any(under(word, root) for word in args.split())


def is_claude(table, pid):
    return os.path.basename(table[pid][2]) == "claude"


def chain(table, pid):
    """pid 自身から根へ向かう祖先の列。"""
    seen = []
    while pid in table and pid not in seen and pid > 1:
        seen.append(pid)
        pid = table[pid][0]
    return seen


def nearest_claude(table, pid):
    """(一番近い claude の PID, その直下の PID)。見つからなければ (None, None)。"""
    below = None
    for ancestor in chain(table, pid):
        if is_claude(table, ancestor):
            return ancestor, below
        below = ancestor
    return None, None


def describe(table, pid):
    args = table[pid][3] or table[pid][2]
    if len(args) > 120:
        args = args[:117] + "…"
    return f"PID {pid} (`{args}`)"


HOW_TO = (
    "自分が起動したプロセスを止めたいなら、起動したときに PID を控える (`… & echo $!`) か、"
    "起動したシェルの子から引き直して (`pgrep -P <シェルの PID>`)、確かめた数字で "
    "`kill <PID>` を送る。"
)


def sessions_sharing(table, own, own_cwd):
    """自分以外の生きている claude で、同じ作業ディレクトリ (かその配下) を開いているもの。"""
    others = [pid for pid in table if pid != own and is_claude(table, pid)]
    return sorted(pid for pid, cwd in cwds_of(table, others).items() if under(cwd, own_cwd))


def judge_orphan(table, own, pid, marks, verdict):
    """親子の鎖が切れたプロセスを、出所のパスで見直す (setup#153)。

    孤児の多くは他人のものではなく、**自分が立てて再親付けされたもの**である。
    """
    session_id, own_cwd = marks
    args = table[pid][3] or ""
    cwd = cwds_of(table, [pid]).get(pid, "")

    # scratchpad は 1 セッション 1 つなので、session_id が出所に在れば持ち主は一意に決まる
    if session_id and (session_id in args or session_id in cwd):
        verdict.add(
            "allow",
            f"{describe(table, pid)} は**このセッションが立てたもの** (出所にこのセッションの "
            f"scratchpad `{session_id}` が在る)。起動したシェルが終わって launchd へ再親付けされ、"
            "親子の鎖が切れているだけで持ち主は変わらない。確認は不要。",
        )
        return

    # 作業ディレクトリは一意ではないので、同じ所を開いた別セッションが居ないことまで確かめる
    if own_cwd and (under(cwd, own_cwd) or mentions(args, own_cwd)):
        sharers = sessions_sharing(table, own, own_cwd)
        if not sharers:
            verdict.add(
                "allow",
                f"{describe(table, pid)} は**このセッションの作業ディレクトリ** (`{own_cwd}`) から"
                "起きた孤児で、そこを開いている Claude Code セッションは他に居ない。確認は不要。",
            )
        else:
            listed = "・".join(f"PID {pid}" for pid in sharers)
            verdict.add(
                "ask",
                f"{describe(table, pid)} はこのセッションの作業ディレクトリ (`{own_cwd}`) から起きた"
                f"孤児だが、**同じ所を開いているセッションが他にも居る** (claude {listed})。"
                f"どちらが立てたものかは出所から決まらないので人に確認する (setup#153)。{HOW_TO}",
            )
        return

    verdict.add(
        "ask",
        f"{describe(table, pid)} はどの Claude Code セッションにも属さず (孤児か、人が端末から"
        "立てたもの)、出所もこのセッションの scratchpad・作業ディレクトリのどちらでもない。"
        "持ち主を判定できないので人に確認する (setup#150・setup#153)。",
    )


def judge_pid(table, own, own_ancestors, pid, verdict, marks=("", "")):
    if pid not in table:
        return
    if own is None:
        verdict.add(
            "ask",
            f"{describe(table, pid)} へのシグナル: このフックが Claude Code のセッションの下で"
            "動いておらず、送り先が誰のものかを判定できない (setup#150)。",
        )
        return
    if pid == own or pid in own_ancestors:
        verdict.add("deny", f"{describe(table, pid)} はこのセッション自身 (またはその祖先) である。")
        return
    owner, below = nearest_claude(table, pid)
    if owner is None:
        judge_orphan(table, own, pid, marks, verdict)
    elif owner != own:
        verdict.add(
            "deny",
            f"{describe(table, pid)} は**別の Claude Code セッション** (claude PID {owner}・"
            f"起動 {table[owner][1] or '不明'}) のプロセス。止めるとそのセッションの作業や MCP サーバが"
            f"切れる (setup#150 で実際に起きた)。{HOW_TO}",
        )
    elif below is None or TOOL_SHELL_MARK not in table[below][3]:
        verdict.add(
            "ask",
            f"{describe(table, pid)} はこのセッションの Claude Code が直接持つ子である。MCP サーバなら"
            "止めるとこのセッションのツールが切れる。背面実行で `exec` を使って立てた自分のプロセスも"
            "同じ形に見えるので区別できず、人に確認する (setup#150)。",
        )


def main():
    if os.environ.get("CLAUDE_SIGNAL_GUARD", "1") == "0":
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "kill" not in command:
        return

    verdict = Verdict()
    try:
        tokens = tokens_of(command)
    except ValueError:
        if LOOSE_KILL.search(strip_heredocs(command)):
            verdict.add(
                "ask",
                "コマンドを区切れず (引用が閉じていない)、kill の送り先を確かめられない (setup#150)。",
            )
        emit(verdict)
        return

    literal = []
    for segment in segments_of(tokens):
        words = drop_redirects(segment)
        index = command_word(words)
        if index is None:
            continue
        name = os.path.basename(words[index])
        rest = words[index + 1 :]
        if name == "xargs" and any(os.path.basename(word) in KILLERS | BY_NAME for word in rest):
            verdict.add(
                "deny",
                "`xargs kill` は送り先が実行するまで決まらず、持ち主を確かめられない (setup#150)。"
                "PID を出すコマンドだけを先に打ち、確かめた数字で `kill <PID>` を送り直す。",
            )
        elif name in BY_NAME:
            verdict.add(
                "deny",
                f"`{name}` は名前で送り先を選ぶので、同じ名前を持つ**別セッションのプロセスまで**止める"
                "(setup#150 では同じ名前のプロセスが 8 本あり、自分のものは 1 本だった)。"
                f"`pgrep` で PID を出して持ち主を確かめ、数字で `kill <PID>` を送る。",
            )
        elif name in KILLERS:
            targets = kill_targets(rest)
            if targets is None:
                continue
            for target in targets:
                if re.fullmatch(r"-?\d+", target) and int(target) <= 0:
                    verdict.add("deny", f"`kill {target}` はプロセスグループへ送るので、持ち主を確かめられない。")
                elif target.isdigit():
                    literal.append(int(target))
                else:
                    verdict.add(
                        "deny",
                        f"kill の送り先 `{target}` が数字の PID ではない。変数やコマンド置換の中身は"
                        "実行するまで決まらないので、持ち主を確かめられない (setup#150 はこの形で"
                        "別セッションの MCP サーバを止めた)。PID を出すコマンドだけを先に打ち、"
                        "確かめた数字で `kill <PID>` を送り直す。",
                    )

    if literal:
        table = process_table()
        self_pid = int(os.environ.get("SIGNAL_GUARD_SELF", "") or os.getpid())
        own, _ = nearest_claude(table, self_pid)
        own_ancestors = set(chain(table, self_pid))
        marks = (payload.get("session_id") or "", payload.get("cwd") or "")
        for pid in literal:
            judge_pid(table, own, own_ancestors, pid, verdict, marks)

    emit(verdict)


if __name__ == "__main__":
    main()
