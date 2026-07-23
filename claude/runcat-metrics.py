#!/usr/bin/env python3
"""
RunCat Neo — Claude Code のセッション情報を Custom Metrics カードへ出す。

RunCat Neo の Custom Metrics 形式
(https://github.com/runcat-dev/RunCatNeo/blob/main/docs/CustomMetricsSchema.md)
で ~/.claude/runcat-usage.json を原子的に書き出す。上流サンプル
(runcat-dev/RunCatNeo docs/samples/claude-code/runcat-statusline.py) が土台。

入口が 2 つあり、stdin の JSON で自動判別する:

1. statusLine モード (ターミナルの `claude`)
   Claude Code が毎ターン渡すセッション JSON をそのまま使う
   (https://code.claude.com/docs/en/statusline)。全メトリクスが取れる。
   stdout にモデル名を出す (これがステータス行に表示される)。

2. hook モード (Claude デスクトップアプリ / ターミナル共通)
   statusLine はターミナル UI の機能でデスクトップアプリでは呼ばれないため、
   hook から起動する。hook の入力にはモデルもトークンも含まれない
   (https://code.claude.com/docs/en/hooks) ので、渡される transcript_path の
   JSONL 末尾を読んで組み立てる。stdout には何も出さない。

出力される行 (値が取れない行は出さない。◯=そのモードで取れる):

    行       例                                 statusLine  hook
    Model    Opus 4.8 · xhigh · think · fast        ◯    ◯ (think/fast は無し)
    Context  31% · 62.5k/200k                       ◯    ◯ (上限は 200k 固定)
    5h / 7d  23.5% · 2h41m left                     ◯    ✗ (hook 入力に無い)
    Cost     $0.42                                  ◯    ✗ (同上。トークン単価は持たない)
    Elapsed  45m · API 2m18s                        ◯    ◯ (API 時間は無し)
    Edits    +156 / -23                             ◯    ✗
    Project  setup · feature-xyz                    ◯    ◯ (worktree 名でなくブランチ名)
    Session  my-session                             ◯    ◯
    Agent    security-reviewer                      ◯    ✗
    PR       #1234 · pending                        ◯    ◯ (レビュー状態は無し)

意図的に出していないもの: session_id・prompt_id・transcript_path・cwd (カード向きでない
ID / パス)、version・output_style.name・vim.mode (常時見る価値が薄い)、
exceeds_200k_tokens (Context 行と重複。1M コンテキストのモデルでのみ差が出る)。

環境変数 RUNCAT_OUT_FILE で出力先を上書きできる (既定: ~/.claude/runcat-usage.json)。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(os.environ.get("RUNCAT_OUT_FILE", str(Path.home() / ".claude" / "runcat-usage.json")))

# hook モードでは文脈上限が入力に無いため既定値を使う (1M コンテキストのモデルでは過大に出る)
DEFAULT_CONTEXT_WINDOW = 200_000

# transcript は伸び続けるので末尾のこのサイズだけ読む (毎ツール呼び出しで走るため)
TRANSCRIPT_TAIL_BYTES = 256 * 1024


# --- 整形ヘルパー -------------------------------------------------------------

def num(value):
    """数値として扱える場合だけ float を返す (bool は数値扱いしない)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def obj(payload, *keys):
    """ネストした dict を辿る。途中が dict でなければ空 dict。"""
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def row(title, formatted, normalized=None):
    if not formatted:
        return None
    metric = {"title": title, "formattedValue": str(formatted)}
    if normalized is not None:
        metric["normalizedValue"] = round(min(max(normalized, 0.0), 1.0), 4)
    return metric


def fmt_tokens(value):
    """トークン数を 62.7k / 200k / 1.2M の形へ (末尾の .0 は落とす)。"""
    if value is None:
        return None
    for unit, scale in (("M", 1_000_000), ("k", 1_000)):
        if value >= scale:
            return f"{value / scale:.1f}".rstrip("0").rstrip(".") + unit
    return f"{value:.0f}"


def fmt_duration(seconds):
    """残り時間・経過時間を 3d4h / 2h41m / 45m / 12s の形へ。"""
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def fmt_model_id(model_id):
    """モデル ID を表示名へ (claude-opus-4-8 → Opus 4.8, claude-haiku-4-5-20251001 → Haiku 4.5)。"""
    if not isinstance(model_id, str) or not model_id:
        return None
    parts = [p for p in model_id.split("-") if p and p != "claude"]
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():  # 末尾の日付スナップショット
        parts = parts[:-1]
    if not parts:
        return None
    version = ".".join(parts[1:])
    return f"{parts[0].capitalize()} {version}".strip()


def context_row(used_tokens, size_tokens, used_pct=None):
    """Context 行と、メニューバーへ出す文字列を返す。"""
    if used_pct is None:
        if not used_tokens or not size_tokens:
            return None, None
        used_pct = round(used_tokens / size_tokens * 100, 1)
    text = f"{used_pct:g}%"
    if used_tokens is not None and size_tokens:
        text += f" · {fmt_tokens(used_tokens)}/{fmt_tokens(size_tokens)}"
    # メニューバーは幅が狭く長い文字列は切られるため、整数へ丸めて出す
    return row("Context", text, used_pct / 100), f"{used_pct:.0f}%"


def snapshot(metrics, bar_value=None):
    result = {
        "title": "Claude Code",
        "symbol": "staroflife",
        "metrics": [m for m in metrics if m is not None],
        "lastUpdatedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if bar_value:
        result["metricsBarValue"] = bar_value
    return result


def write_snapshot(data):
    """RunCat が半端な内容を読まないよう、一時ファイル経由で原子的に置き換える。"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".runcat-", dir=str(OUT.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OUT)


# --- statusLine モード ---------------------------------------------------------

def rate_limit_row(title, window, now_epoch):
    """レート制限の行。使用率にリセットまでの残り時間を添える。"""
    used = num(window.get("used_percentage"))
    if used is None:
        return None
    text = f"{used:g}%"
    resets_at = num(window.get("resets_at"))
    left = fmt_duration(resets_at - now_epoch) if resets_at is not None else None
    if left:
        text += f" · {left} left"
    return row(title, text, used / 100)


def from_statusline(payload):
    now_epoch = datetime.now(timezone.utc).timestamp()

    # Model: モデル名に effort / extended thinking / fast mode のマーカーを添える
    model_name = obj(payload, "model").get("display_name") or "Claude Code"
    parts = [str(model_name)]
    if obj(payload, "effort").get("level"):
        parts.append(str(obj(payload, "effort")["level"]))
    if obj(payload, "thinking").get("enabled") is True:
        parts.append("think")
    if payload.get("fast_mode") is True:
        parts.append("fast")

    # Context: 使用率にトークン数 (現在/上限) を添える
    context = obj(payload, "context_window")
    ctx_pct = num(context.get("used_percentage"))
    in_tokens = num(context.get("total_input_tokens"))
    out_tokens = num(context.get("total_output_tokens"))
    used_tokens = (in_tokens or 0) + (out_tokens or 0) if (in_tokens is not None or out_tokens is not None) else None
    ctx_row, bar_value = (None, None)
    if ctx_pct is not None:
        ctx_row, bar_value = context_row(used_tokens, num(context.get("context_window_size")), ctx_pct)

    # Cost / Elapsed / Edits
    cost = obj(payload, "cost")
    cost_usd = num(cost.get("total_cost_usd"))
    total_ms = num(cost.get("total_duration_ms"))
    api_ms = num(cost.get("total_api_duration_ms"))
    elapsed = fmt_duration(total_ms / 1000) if total_ms is not None else None
    api_elapsed = fmt_duration(api_ms / 1000) if api_ms is not None else None
    if elapsed and api_elapsed:
        elapsed = f"{elapsed} · API {api_elapsed}"
    added = num(cost.get("total_lines_added"))
    removed = num(cost.get("total_lines_removed"))

    # Project: リポジトリ名 (なければ起動ディレクトリ名) に worktree 名を添える。
    # 複数セッションが同じ JSON を上書きするため、どのセッションの値かを見分ける手掛かりになる
    workspace = obj(payload, "workspace")
    project = obj(workspace, "repo").get("name")
    if not project and workspace.get("project_dir"):
        project = os.path.basename(str(workspace["project_dir"]).rstrip("/"))
    worktree = workspace.get("git_worktree") or obj(payload, "worktree").get("name")

    # PR: 番号にレビュー状態を添える
    pr = obj(payload, "pr")
    pr_text = f"#{pr['number']}" if pr.get("number") is not None else None
    if pr_text and pr.get("review_state"):
        pr_text += f" · {pr['review_state']}"

    metrics = [
        row("Model", " · ".join(parts)),
        ctx_row,
        rate_limit_row("5h", obj(payload, "rate_limits", "five_hour"), now_epoch),
        rate_limit_row("7d", obj(payload, "rate_limits", "seven_day"), now_epoch),
        row("Cost", f"${cost_usd:,.2f}" if cost_usd is not None else None),
        row("Elapsed", elapsed),
        row("Edits", f"+{added:.0f} / -{removed:.0f}" if added is not None and removed is not None else None),
        row("Project", f"{project} · {worktree}" if project and worktree else (project or worktree)),
        row("Session", payload.get("session_name")),
        row("Agent", obj(payload, "agent").get("name")),
        row("PR", pr_text),
    ]
    return snapshot(metrics, bar_value), str(model_name)


# --- hook モード ---------------------------------------------------------------

def read_transcript(path):
    """transcript JSONL の先頭 1 行と末尾チャンクを読み、パースできた行だけ返す。"""
    with open(path, "rb") as f:
        first_line = f.readline()
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
        tail = f.read()
    chunks = tail.split(b"\n")
    if size > TRANSCRIPT_TAIL_BYTES:
        chunks = chunks[1:]  # 途中で切れた先頭行は捨てる
    entries = []
    for raw in [first_line] + chunks:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def last_with(entries, key):
    for entry in reversed(entries):
        if entry.get(key) is not None:
            return entry[key]
    return None


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def from_transcript(path):
    entries = read_transcript(path)
    assistants = [e for e in entries if e.get("type") == "assistant" and not e.get("isSidechain")]
    last = assistants[-1] if assistants else {}
    message = obj(last, "message")

    # Model: transcript にはモデル ID しか無いので表示名へ整形し、effort を添える
    parts = [fmt_model_id(message.get("model")) or "Claude Code"]
    if last.get("effort"):
        parts.append(str(last["effort"]))

    # Context: 直近レスポンスの usage (キャッシュ分を含む) の合計が現在の文脈量
    usage = obj(message, "usage")
    used_tokens = sum(
        num(usage.get(k)) or 0
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    ) or None
    ctx_row, bar_value = context_row(used_tokens, DEFAULT_CONTEXT_WINDOW)

    # Elapsed: transcript の最初と最後のエントリの時刻差
    start = parse_ts(entries[0].get("timestamp")) if entries else None
    end = parse_ts(last.get("timestamp"))
    elapsed = fmt_duration((end - start).total_seconds()) if start and end else None

    # Project: リポジトリ名 (PR 情報があればそちら、なければ cwd 名) にブランチ名を添える
    repository = last_with(entries, "prRepository")
    cwd = last_with(entries, "cwd")
    project = repository.split("/")[-1] if isinstance(repository, str) and repository else None
    if not project and isinstance(cwd, str):
        project = os.path.basename(cwd.rstrip("/"))
    branch = last_with(entries, "gitBranch")

    pr_number = last_with(entries, "prNumber")
    title = last_with(entries, "customTitle") or last_with(entries, "aiTitle")

    metrics = [
        row("Model", " · ".join(parts)),
        ctx_row,
        row("Elapsed", elapsed),
        row("Project", f"{project} · {branch}" if project and branch else (project or branch)),
        row("Session", title),
        row("PR", f"#{pr_number}" if pr_number is not None else None),
    ]
    return snapshot(metrics, bar_value)


# --- エントリポイント -----------------------------------------------------------

def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    if payload.get("hook_event_name"):
        # hook を止めないよう、失敗しても黙って終了する
        try:
            write_snapshot(from_transcript(payload["transcript_path"]))
        except Exception:
            pass
        return

    data, model_name = from_statusline(payload)
    write_snapshot(data)
    print(model_name)


if __name__ == "__main__":
    main()
