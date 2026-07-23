#!/usr/bin/env python3
"""
RunCat Neo — Claude Code statusLine 連携。

上流サンプル (runcat-dev/RunCatNeo docs/samples/claude-code/runcat-statusline.py)
をベースに、statusLine が渡す JSON のうち意味のある値をすべて出すよう拡張したもの。

Claude Code は毎ターン statusLine コマンドの stdin へセッション情報の JSON を渡す
(仕様: https://code.claude.com/docs/en/statusline)。本スクリプトはそれを RunCat Neo の
Custom Metrics 形式 (仕様: https://github.com/runcat-dev/RunCatNeo/blob/main/docs/CustomMetricsSchema.md)
へ変換して ~/.claude/runcat-usage.json へ原子的に書き出し、stdout にはモデル名を出す。

出力される行 (値が取れないフィールドの行は出さない):

    Model    Opus 4.8 · xhigh · think · fast   model / effort / thinking / fast_mode
    Context  31% · 62.5k/200k                  context_window (バー付き)
    5h       23.5% · 2h41m left                rate_limits.five_hour (バー付き)
    7d       41.2% · 3d4h left                 rate_limits.seven_day (バー付き)
    Cost     $0.42                             cost.total_cost_usd
    Elapsed  45m · API 2m18s                   cost.total_duration_ms / total_api_duration_ms
    Edits    +156 / -23                        cost.total_lines_{added,removed}
    Project  claude-code · feature-xyz         workspace.repo.name (または project_dir) / worktree
    Session  my-session                        session_name
    Agent    security-reviewer                 agent.name
    PR       #1234 · pending                   pr.number / pr.review_state

意図的に出していないもの: session_id・prompt_id・transcript_path・cwd (カード向きでない ID / パス)、
version・output_style.name・vim.mode (常時見る価値が薄い)、exceeds_200k_tokens
(Context 行と重複。1M コンテキストのモデルでのみ差が出る)。

環境変数 RUNCAT_OUT_FILE で出力先を上書きできる (既定: ~/.claude/runcat-usage.json)。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(os.environ.get("RUNCAT_OUT_FILE", str(Path.home() / ".claude" / "runcat-usage.json")))


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


try:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        payload = {}
except Exception:
    payload = {}

now = datetime.now(timezone.utc)

# Model: モデル名に effort / extended thinking / fast mode のマーカーを添える
model_name = obj(payload, "model").get("display_name") or "Claude Code"
parts = [str(model_name)]
if obj(payload, "effort").get("level"):
    parts.append(str(obj(payload, "effort")["level"]))
if obj(payload, "thinking").get("enabled") is True:
    parts.append("think")
if payload.get("fast_mode") is True:
    parts.append("fast")
model_text = " · ".join(parts)

# Context: 使用率にトークン数 (現在/上限) を添える
context = obj(payload, "context_window")
ctx_pct = num(context.get("used_percentage"))
ctx_text = f"{ctx_pct:g}%" if ctx_pct is not None else None
in_tokens = num(context.get("total_input_tokens"))
out_tokens = num(context.get("total_output_tokens"))
used_tokens = (in_tokens or 0) + (out_tokens or 0) if (in_tokens is not None or out_tokens is not None) else None
size_tokens = num(context.get("context_window_size"))
if ctx_text and used_tokens is not None and size_tokens:
    ctx_text += f" · {fmt_tokens(used_tokens)}/{fmt_tokens(size_tokens)}"

# Cost / Elapsed / Edits
cost = obj(payload, "cost")
cost_usd = num(cost.get("total_cost_usd"))
cost_text = f"${cost_usd:,.2f}" if cost_usd is not None else None

total_ms = num(cost.get("total_duration_ms"))
api_ms = num(cost.get("total_api_duration_ms"))
elapsed = fmt_duration(total_ms / 1000) if total_ms is not None else None
api_elapsed = fmt_duration(api_ms / 1000) if api_ms is not None else None
if elapsed and api_elapsed:
    elapsed = f"{elapsed} · API {api_elapsed}"

added = num(cost.get("total_lines_added"))
removed = num(cost.get("total_lines_removed"))
edits_text = f"+{added:.0f} / -{removed:.0f}" if added is not None and removed is not None else None

# Project: リポジトリ名 (なければ起動ディレクトリ名) に worktree 名を添える。
# 複数セッションが同じ JSON を上書きするため、どのセッションの値かを見分ける手掛かりになる
workspace = obj(payload, "workspace")
project = obj(workspace, "repo").get("name")
if not project and workspace.get("project_dir"):
    project = os.path.basename(str(workspace["project_dir"]).rstrip("/"))
worktree = workspace.get("git_worktree") or obj(payload, "worktree").get("name")
project_text = f"{project} · {worktree}" if project and worktree else (project or worktree)

# PR: 番号にレビュー状態を添える
pr = obj(payload, "pr")
pr_text = f"#{pr['number']}" if pr.get("number") is not None else None
if pr_text and pr.get("review_state"):
    pr_text += f" · {pr['review_state']}"

snapshot = {
    "title": "Claude Code",
    "symbol": "staroflife",
    "metrics": [m for m in [
        row("Model", model_text),
        row("Context", ctx_text, ctx_pct / 100 if ctx_pct is not None else None),
        rate_limit_row("5h", obj(payload, "rate_limits", "five_hour"), now.timestamp()),
        rate_limit_row("7d", obj(payload, "rate_limits", "seven_day"), now.timestamp()),
        row("Cost", cost_text),
        row("Elapsed", elapsed),
        row("Edits", edits_text),
        row("Project", project_text),
        row("Session", payload.get("session_name")),
        row("Agent", obj(payload, "agent").get("name")),
        row("PR", pr_text),
    ] if m is not None],
    "lastUpdatedDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
}
if ctx_pct is not None:
    # メニューバーは幅が狭く長い文字列は切られるため、整数へ丸めて出す
    snapshot["metricsBarValue"] = f"{ctx_pct:.0f}%"

OUT.parent.mkdir(parents=True, exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".runcat-", dir=str(OUT.parent))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(snapshot, f, ensure_ascii=False)
os.replace(tmp, OUT)

print(model_name)
