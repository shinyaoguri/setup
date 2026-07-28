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

カードは 1 枚しか無いのに Claude Code は同時に何セッションも動く。そこで各実行は
まず自分の状態を SESSIONS_DIR/<session_id>.json へ書き、次に生きている全セッション
(最終更新が SESSION_TTL_SECONDS 以内) を読み直してカードを組む。これで最後に走った
セッションが他を上書きしてしまうことがなくなる。

カードの形は生きているセッション数で変わる:

  1 つ  → そのセッションの詳細を 1 行 1 項目で出す (下表)
  2 つ+ → レート制限を頭に置き、以降はセッションごとに 1 行へ畳む
          (例: `setup   21% · Opus 5 · 12m`)。行数とカード幅を抑えるため。

詳細レイアウトで出る行 (値が取れない行は出さない。◯=そのモードで取れる):

    行       例                                 statusLine  hook
    Model    Opus 5 · xhigh · think · fast          ◯    ◯ (think/fast は無し)
    Context  31% · 62.5k/200k                       ◯    ◯ (上限は推定値)
    5h / 7d  23.5% · 2h41m left                     ◯    △ (statusLine の控えを使う)
    Cost     $0.42                                  ◯    ✗ (hook 入力に無い。トークン単価も持たない)
    Elapsed  45m · API 2m18s                        ◯    ◯ (API 時間は無し)
    Edits    +156 / -23                             ◯    ✗
    Project  setup · feature-xyz                    ◯    ◯ (worktree 名でなくブランチ名)
    Session  my-session                             ◯    ◯
    Agent    security-reviewer                      ◯    ✗
    PR       #1234 · pending                        ◯    ◯ (レビュー状態は無し)

メニューバーへ出す値 (metricsBarValue) は週間制限の使用率。

レート制限は hook 入力にも transcript にも無いため、statusLine モードで取れた値を
控え (RATE_LIMITS_CACHE)、hook モードではリセット時刻を過ぎるまでそれを使う。
控えは最後に取れた時点の値で実際はそれ以上なので、下限として ≥ を付けて出す。
モデル別の週間制限 (Opus / Sonnet / Fable) は Claude Code が statusLine へ渡すのが
five_hour と seven_day だけのため出せない (2.1.212 時点)。

文脈の上限は statusLine 入力にしか無い。hook モードではモデル名から引く
(200k なのは Haiku 4.5 だけで、Claude 5 系はいずれも 1M が既定)。

カードの幅は一番長い行に引きずられるので、可変長の値 (プロジェクト名・ブランチ名・
セッション名) は MAX_VALUE_WIDTH 幅で切り詰める。

意図的に出していないもの: session_id・prompt_id・transcript_path・cwd (カード向きでない
ID / パス)、version・output_style.name・vim.mode (常時見る価値が薄い)、
exceeds_200k_tokens (Context 行と重複。1M コンテキストのモデルでのみ差が出る)。

環境変数 RUNCAT_OUT_FILE で出力先を上書きできる (既定: ~/.claude/runcat-usage.json)。
レート制限の控えとセッションごとの状態は同じディレクトリに置く。
"""

import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(os.environ.get("RUNCAT_OUT_FILE", str(Path.home() / ".claude" / "runcat-usage.json")))

# statusLine で取れたレート制限の控え (hook モードから読む)
RATE_LIMITS_CACHE = OUT.parent / "runcat-rate-limits.json"

# セッションごとの状態 (<session_id>.json)。カードはここを集約して組む
SESSIONS_DIR = OUT.parent / "runcat-sessions"

# これを過ぎて更新の無いセッションは終了したとみなす。ターン間の間隔より十分長く取る
SESSION_TTL_SECONDS = 30 * 60

# 掃除の閾値。TTL を過ぎても暫くは残し、古すぎるものだけ消す
SESSION_PURGE_SECONDS = 24 * 60 * 60

# hook モードでは文脈上限が入力に無いためモデル名から引く。現行モデルで 200k なのは
# Haiku 4.5 だけで、Claude 5 系 (Opus / Sonnet / Fable) はいずれも 1M が既定
# (ベータヘッダも長文脈の追加料金も無い)
SMALL_CONTEXT_MODELS = ("haiku",)
SMALL_CONTEXT_WINDOW = 200_000
DEFAULT_CONTEXT_WINDOW = 1_000_000

# transcript は伸び続けるので末尾のこのサイズだけ読む (毎ツール呼び出しで走るため)
TRANSCRIPT_TAIL_BYTES = 256 * 1024

# 可変長の値をこの表示幅で切る (全角は 2 幅)。カードが横に伸びるのを防ぐ
MAX_VALUE_WIDTH = 28


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


def text_width(text):
    """全角 (東アジア幅 W/F) を 2 と数えた表示幅。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def clip(text, limit=MAX_VALUE_WIDTH):
    """表示幅が limit に収まるよう末尾を … で省略する。"""
    if text is None:
        return None
    text = str(text)
    if text_width(text) <= limit:
        return text
    kept, width = [], 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if width + char_width > limit - 1:  # … の 1 幅を残す
            break
        kept.append(char)
        width += char_width
    return "".join(kept).rstrip() + "…"


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
    """モデル ID を表示名へ (claude-opus-5 → Opus 5, claude-haiku-4-5-20251001 → Haiku 4.5)。"""
    if not isinstance(model_id, str) or not model_id:
        return None
    parts = [p for p in model_id.split("-") if p and p != "claude"]
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():  # 末尾の日付スナップショット
        parts = parts[:-1]
    if not parts:
        return None
    version = ".".join(parts[1:])
    return f"{parts[0].capitalize()} {version}".strip()


def guess_context_window(model_name, used_tokens):
    """hook モード用の文脈上限をモデル名から引く。

    statusLine 入力と違い hook 入力には上限が無い。現行モデルで 200k なのは
    Haiku 4.5 だけなので二択に落とし、既定は 1M とする。表に無い上限のモデルでも
    使用率が 100% を超えないよう、使用量が上限を上回ったら使用量を上限とみなす。
    """
    name = str(model_name or "").lower()
    size = SMALL_CONTEXT_WINDOW if any(m in name for m in SMALL_CONTEXT_MODELS) else DEFAULT_CONTEXT_WINDOW
    if used_tokens and used_tokens > size:
        return used_tokens
    return size


def context_row(used_tokens, size_tokens, used_pct=None):
    if used_pct is None:
        if not used_tokens or not size_tokens:
            return None
        used_pct = round(used_tokens / size_tokens * 100, 1)
    text = f"{used_pct:g}%"
    if used_tokens is not None and size_tokens:
        text += f" · {fmt_tokens(used_tokens)}/{fmt_tokens(size_tokens)}"
    return row("Context", text, used_pct / 100)


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


def write_json(path, data):
    """読み手が半端な内容を読まないよう、一時ファイル経由で原子的に置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".runcat-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# --- レート制限 ----------------------------------------------------------------

def rate_limit_row(title, window, now_epoch, stale=False):
    """レート制限の行。使用率にリセットまでの残り時間を添える。

    stale は控えから読んだ値。以後も使用率は上がる一方なので下限として ≥ を付ける。
    """
    used = num(window.get("used_percentage"))
    if used is None:
        return None
    text = f"{'≥' if stale else ''}{used:g}%"
    resets_at = num(window.get("resets_at"))
    left = fmt_duration(resets_at - now_epoch) if resets_at is not None else None
    if left:
        text += f" · {left} left"
    return row(title, text, used / 100)


def fmt_bar_value(window, stale=False):
    """メニューバーは幅が狭く長い文字列は切られるため、整数へ丸めて出す。"""
    used = num(window.get("used_percentage"))
    if used is None:
        return None
    return f"{'≥' if stale else ''}{used:.0f}%"


def save_rate_limits(windows):
    """statusLine で取れたレート制限を hook モード用に控える。"""
    kept = {}
    for key, window in windows.items():
        used = num(window.get("used_percentage"))
        resets_at = num(window.get("resets_at"))
        if used is None or resets_at is None:
            continue  # リセット時刻が無い値は古さを判定できないので控えない
        kept[key] = {"used_percentage": used, "resets_at": resets_at}
    if kept:
        write_json(RATE_LIMITS_CACHE, kept)


def load_rate_limits(now_epoch):
    """控えのうち、まだリセットされていないウィンドウだけ返す。"""
    try:
        cached = json.loads(RATE_LIMITS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(cached, dict):
        return {}
    return {
        key: window for key, window in cached.items()
        if isinstance(window, dict) and (num(window.get("resets_at")) or 0) > now_epoch
    }


# --- セッションごとの状態 --------------------------------------------------------

def session_filename(session_id):
    """session_id をファイル名へ。パス区切りなどが混ざっても安全な形に落とす。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "unknown"))
    return f"{safe[:96] or 'unknown'}.json"


def save_session(state):
    """このセッションの状態を書き出す。カードは全セッション分から組む。"""
    write_json(SESSIONS_DIR / session_filename(state.get("session_id")), state)


def load_sessions(now_epoch, current_id=None):
    """生きているセッションの状態を、更新の新しい順に返す。

    TTL を過ぎたものは畳み、さらに古いファイルは掃除する。current_id は
    自分の状態 (今書いたばかり) で、TTL 判定に関わらず必ず含める。
    """
    states = []
    try:
        paths = sorted(SESSIONS_DIR.iterdir())
    except Exception:
        return states
    current_name = session_filename(current_id) if current_id is not None else None
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        age = now_epoch - (num(state.get("updated_at")) or 0)
        if path.name == current_name or age <= SESSION_TTL_SECONDS:
            states.append(state)
        elif age > SESSION_PURGE_SECONDS:
            try:
                path.unlink()
            except Exception:
                pass
    states.sort(key=lambda s: num(s.get("updated_at")) or 0, reverse=True)
    return states


def session_label(state, states):
    """セッション行のラベル。同じプロジェクトが複数あるときだけブランチを添える。"""
    project = state.get("project") or state.get("title") or "session"
    same = [s for s in states if (s.get("project") or s.get("title")) == project]
    if len(same) > 1 and state.get("branch"):
        project = f"{project} · {state['branch']}"
    return clip(project, 20)


def session_summary(state):
    """セッション行の値。使用率・モデル・経過を 1 行へ畳む。"""
    parts = []
    ctx_pct = num(state.get("ctx_pct"))
    if ctx_pct is not None:
        parts.append(f"{ctx_pct:g}%")
    if state.get("model"):
        parts.append(str(state["model"]))
    if state.get("elapsed"):
        parts.append(str(state["elapsed"]))
    return " · ".join(parts) or None


# --- カードの組み立て -----------------------------------------------------------

def detail_metrics(state, limits, now_epoch, stale):
    """セッションが 1 つのときのレイアウト (1 行 1 項目)。"""
    ctx_pct = num(state.get("ctx_pct"))
    ctx_row = None
    if ctx_pct is not None:
        ctx_row = context_row(state.get("ctx_used"), state.get("ctx_size"), ctx_pct)
    project = state.get("project")
    branch = state.get("branch")
    return [
        row("Model", state.get("model")),
        ctx_row,
        rate_limit_row("5h", limits.get("five_hour", {}), now_epoch, stale=stale),
        rate_limit_row("7d", limits.get("seven_day", {}), now_epoch, stale=stale),
        row("Cost", state.get("cost")),
        row("Elapsed", state.get("elapsed")),
        row("Edits", state.get("edits")),
        row("Project", clip(f"{project} · {branch}" if project and branch else (project or branch))),
        row("Session", clip(state.get("title"))),
        row("Agent", clip(state.get("agent"))),
        row("PR", state.get("pr")),
    ]


def summary_metrics(states, limits, now_epoch, stale):
    """セッションが複数のときのレイアウト (レート制限 + 1 セッション 1 行)。"""
    metrics = [
        rate_limit_row("5h", limits.get("five_hour", {}), now_epoch, stale=stale),
        rate_limit_row("7d", limits.get("seven_day", {}), now_epoch, stale=stale),
    ]
    for state in states:
        ctx_pct = num(state.get("ctx_pct"))
        metrics.append(row(
            session_label(state, states),
            session_summary(state),
            ctx_pct / 100 if ctx_pct is not None else None,
        ))
    return metrics


def build_card(states, limits, now_epoch, stale):
    """生きているセッションからカードを組む。"""
    if len(states) <= 1:
        state = states[0] if states else {}
        metrics = detail_metrics(state, limits, now_epoch, stale)
    else:
        metrics = summary_metrics(states, limits, now_epoch, stale)
    return snapshot(metrics, fmt_bar_value(limits.get("seven_day", {}), stale=stale))


# --- statusLine モード ---------------------------------------------------------

def from_statusline(payload):
    """statusLine 入力からこのセッションの状態を組む。"""
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

    # Project: リポジトリ名 (なければ起動ディレクトリ名) に worktree 名を添える
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

    state = {
        "session_id": payload.get("session_id"),
        "updated_at": datetime.now(timezone.utc).timestamp(),
        "model": " · ".join(parts),
        "ctx_pct": ctx_pct,
        "ctx_used": used_tokens,
        "ctx_size": num(context.get("context_window_size")),
        "cost": f"${cost_usd:,.2f}" if cost_usd is not None else None,
        "elapsed": elapsed,
        "edits": f"+{added:.0f} / -{removed:.0f}" if added is not None and removed is not None else None,
        "project": project,
        "branch": worktree,
        "title": payload.get("session_name"),
        "agent": obj(payload, "agent").get("name"),
        "pr": pr_text,
    }
    return state, str(model_name)


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


def project_from_cwd(cwd):
    """起動ディレクトリからプロジェクト名を取る。

    worktree の中では末尾が worktree 名になり、どのリポジトリか分からなくなる。
    ブランチ名は別に出しているので、ここは元のリポジトリ名の方が役に立つ。
    """
    marker = "/.claude/worktrees/"
    if marker in cwd:
        cwd = cwd.split(marker)[0]
    return os.path.basename(cwd.rstrip("/")) or None


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def from_transcript(path, session_id=None):
    """transcript からこのセッションの状態を組む (hook 入力に無い項目は落とす)。"""
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
    ctx_size = guess_context_window(message.get("model"), used_tokens)
    ctx_pct = round(used_tokens / ctx_size * 100, 1) if used_tokens else None

    # Elapsed: transcript の最初と最後のエントリの時刻差
    start = parse_ts(entries[0].get("timestamp")) if entries else None
    end = parse_ts(last.get("timestamp"))
    elapsed = fmt_duration((end - start).total_seconds()) if start and end else None

    # Project: リポジトリ名 (PR 情報があればそちら、なければ cwd 名) にブランチ名を添える
    repository = last_with(entries, "prRepository")
    cwd = last_with(entries, "cwd")
    project = repository.split("/")[-1] if isinstance(repository, str) and repository else None
    if not project and isinstance(cwd, str):
        project = project_from_cwd(cwd)
    branch = last_with(entries, "gitBranch")

    pr_number = last_with(entries, "prNumber")
    title = last_with(entries, "customTitle") or last_with(entries, "aiTitle")

    return {
        "session_id": session_id or last_with(entries, "sessionId"),
        "updated_at": datetime.now(timezone.utc).timestamp(),
        "model": " · ".join(parts),
        "ctx_pct": ctx_pct,
        "ctx_used": used_tokens,
        "ctx_size": ctx_size if used_tokens else None,
        "cost": None,
        "elapsed": elapsed,
        "edits": None,
        "project": project,
        "branch": branch,
        "title": title,
        "agent": None,
        "pr": f"#{pr_number}" if pr_number is not None else None,
    }


# --- エントリポイント -----------------------------------------------------------

def render(state, limits, now_epoch, stale):
    """自分の状態を保存し、生きている全セッションからカードを組んで書き出す。"""
    save_session(state)
    states = load_sessions(now_epoch, current_id=state.get("session_id"))
    write_json(OUT, build_card(states, limits, now_epoch, stale))


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    now_epoch = datetime.now(timezone.utc).timestamp()

    if payload.get("hook_event_name"):
        # hook を止めないよう、失敗しても黙って終了する
        try:
            state = from_transcript(payload["transcript_path"], payload.get("session_id"))
            # レート制限は hook 入力に無いので statusLine モードの控えを使う
            render(state, load_rate_limits(now_epoch), now_epoch, stale=True)
        except Exception:
            pass
        return

    state, model_name = from_statusline(payload)

    # レート制限: hook モード (デスクトップアプリ) では取れないのでここで控えておく
    five_hour = obj(payload, "rate_limits", "five_hour")
    seven_day = obj(payload, "rate_limits", "seven_day")
    if five_hour or seven_day:
        save_rate_limits({"five_hour": five_hour, "seven_day": seven_day})

    render(state, {"five_hour": five_hour, "seven_day": seven_day}, now_epoch, stale=False)
    print(model_name)


if __name__ == "__main__":
    main()
