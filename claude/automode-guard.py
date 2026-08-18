#!/usr/bin/env python3
"""Claude Code の PreToolUse フック: 権限設定 (autoMode) の書き換えを人間に返す。

`autoMode` はこのマシンで「何を確認なしに実行してよいか」を決める節で、緩めることは
エージェントが自分に権限を与えることそのものにあたる。`hard_deny` の
"Auto-Mode Self-Authorization" がそれを "Block unconditionally" と宣言しているが、
実際には Edit ツール経由の書き換えが素通しになった (setup#85)。ルール本文は
`jq` / `sed` / heredoc / `git checkout <rev> --` を列挙し "whether through Edit or Write"
とも書いているので記述漏れではなく、意味判定の層に確実に効いてほしい 1 点を預けている
こと自体が構造的な問題だった。git-safety-guard.sh が文書ルールから移したのと同じ手当てで、
決定論的に効く層へ移す。

判定は一つだけ: **編集の前後で `.autoMode` に差分が出るなら ask**。

  - 緩めたか締めたかは見ない。エントリの文言を弱める書き換えは機械に読めないので、
    `allow` への追加や `$defaults` の削除だけを見ると読めない書き換えが素通りする
  - パスも列挙しない。対象は「JSON としてパースでき `.autoMode` を持つファイル」で、
    ~/.claude/settings.json も worktree 内の .claude/settings.local.json も setup リポの
    実体も同じ判定になる。パスの列挙は環境が変わるたびに漏れる

deny ではなく ask なのは、`autoMode` の変更が正当な作業でもあるから (この節を足した
setup#86 がまさにそれ)。worktree-path-guard.sh が deny なのは「パスを直せばその場で
続行できる = 人を呼ぶ必要がない」からで、ここは人を呼ぶことそのものが目的になる。

契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
呼び出し口は settings.json の hooks.PreToolUse、テストは
claude/tests/automode_guard_test.py (python3 で直接実行)。
"""

import json
import sys
from pathlib import Path

# 表示の順序を安定させるために既知の節を先に並べる。ここに無いキーが増えても
# 検知はできる (差分は節名を問わず拾い、表示のときに後ろへ足す)
SECTIONS = ("allow", "soft_deny", "hard_deny", "environment")

# 「読めなかった」を「autoMode が無い」と区別する。壊れた JSON は設定が丸ごと無視
# される事故に直結するので、元が読めていたなら壊す変更も人間に返す
UNREADABLE = object()


def ask(reason):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.exit(0)


def automode_of(text):
    """テキストから `.autoMode` を取り出す。無ければ None、読めなければ UNREADABLE。"""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return UNREADABLE
    if not isinstance(data, dict):
        return None
    return data.get("autoMode")


def edited_text(tool, tool_input, before):
    """編集後の内容を組み立てる。組み立てられなければ None を返して素通しにする。

    置換が当たらない・入力が壊れているといった失敗は Claude Code 側がエラーにするので、
    ここで判定を捏造しない (無理に判定すると、実際には起きない編集で人を呼ぶことになる)。
    """
    if tool == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    if before is None:
        return None

    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    if not isinstance(edits, list):
        return None

    text = before
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old == "":
            return None
        if old not in text:
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def describe(value):
    if value is None:
        return "無し"
    if value is UNREADABLE:
        return "**JSON として読めない**"
    if isinstance(value, list):
        marker = "$defaults あり" if "$defaults" in value else "**$defaults なし**"
        return f"{len(value)} 件 ({marker})"
    return json.dumps(value, ensure_ascii=False)[:80]


def section_names(before, after):
    names = list(SECTIONS)
    for source in (before, after):
        if isinstance(source, dict):
            for key in source:
                if key not in names:
                    names.append(key)
    return names


def summarize(before, after, target):
    def section(source, name):
        return source.get(name) if isinstance(source, dict) else None

    lines = []
    if after is UNREADABLE:
        lines.append("  ファイル全体が JSON として読めなくなります (設定が丸ごと無視されます)")
    else:
        for name in section_names(before, after):
            was, now = section(before, name), section(after, name)
            if was == now:
                continue
            lines.append(f"  {name}: {describe(was)} → {describe(now)}")
        if not lines:
            lines.append("  (節の構成は同じで、エントリの中身が変わります)")

    body = "\n".join(lines)
    return (
        f"権限設定 (autoMode) を書き換えようとしています。\n\n"
        f"  ファイル: {target}\n\n"
        f"{body}\n\n"
        f"autoMode はこのマシンで「何を確認なしに実行してよいか」を決める節で、緩めることは\n"
        f"エージェントが自分に権限を与えることそのものにあたります。意図した変更なら承認して\n"
        f"ください (締める変更・整形だけの変更も同じくここを通ります)。"
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return

    tool = payload.get("tool_name")
    if tool not in ("Edit", "MultiEdit", "Write"):
        return

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    target = tool_input.get("file_path")
    if not isinstance(target, str) or not target:
        return

    path = Path(target)
    if not path.is_absolute():
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            path = Path(cwd) / path

    try:
        before_text = path.read_text()
    except (OSError, ValueError):
        before_text = None

    after_text = edited_text(tool, tool_input, before_text)
    if after_text is None:
        return

    before = automode_of(before_text)
    after = automode_of(after_text)

    had = before is not None and before is not UNREADABLE
    has = after is not None and after is not UNREADABLE

    if had:
        if after is UNREADABLE or before != after:
            ask(summarize(before, after, target))
        return
    if has:
        ask(summarize(before, after, target))


if __name__ == "__main__":
    main()
