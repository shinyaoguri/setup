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

性質の違う 2 つを 1 枚に詰めると区切りが要って読みにくいので、カードを 2 枚に分ける。
RunCat 側で別々のソースとして登録する (設定 → Metrics → Custom Metrics → Add
Custom Metrics Source)。どちらも毎回まとめて書き出す:

    OUT (Claude Code)              SESSIONS_OUT (Claude Sessions)
    5h        0% · 2h41m left      setup     21% · Opus 5 · 12m
    7d        18% · 4d12h left     divive    8% · Opus 5 · 3m
    7d Fable  10% · 4d12h left

Claude Code は同時に何セッションも動くので、各実行はまず自分の状態を
SESSIONS_DIR/<session_id>.json へ書き、次に生きている全セッション (最終更新が
SESSION_TTL_SECONDS 以内) を読み直してカードを組む。これで最後に走ったセッションが
他を上書きしてしまうことがなくなる。

セッション行のラベルはプロジェクト名。同じプロジェクトが複数あるときだけ
ブランチ名を添えて区別する。値に入れられるもの (◯=そのモードで取れる):

    項目      例                       statusLine  hook
    使用率    21%                          ◯    ◯ (上限はモデル名から引く)
    モデル    Opus 5 · xhigh · think       ◯    ◯ (think/fast は無し)
    経過      12m                          ◯    ◯

メニューバーへ出す値 (metricsBarValue) は、レート制限のカードが週間制限の使用率、
セッションのカードが一番文脈を使っているセッションの使用率 (圧縮が近いものが分かる)。

レート制限は Claude Code の /usage と同じ使用量エンドポイント (USAGE_URL) から取る。
statusLine が渡す rate_limits は five_hour と seven_day だけで、モデル別の週間制限
(`7d Fable` など) はそこに来ないため。応答の limits 配列を kind と scope で読むので、
枠が増えても拾える。トークンは keychain から読み、プロセスの中だけで使う。

非公式・非文書化の API なので、壊れたときは statusLine 入力 → その控え
(RATE_LIMITS_CACHE) の順に落ち、モデル別の行が消えるだけで他は出続ける。控えや
古い応答から読んだ値は、その後も使用率は上がる一方なので下限として ≥ を付ける。

文脈の上限は statusLine 入力にしか無い。hook モードではモデル名から引く
(200k なのは Haiku 4.5 だけで、Claude 5 系はいずれも 1M が既定)。

カードの幅は一番長い行に引きずられるので、可変長の値 (プロジェクト名・ブランチ名)
は MAX_VALUE_WIDTH 幅で切り詰める。

1 セッション 1 行に絞ったため、statusLine では取れるが出していないものがある: cost・
total_lines_added/removed・pr・agent.name・session_name (1 行に畳むと長くなる)。
ほかに session_id・prompt_id・transcript_path・cwd (カード向きでない ID / パス)、
version・output_style.name・vim.mode (常時見る価値が薄い)、exceeds_200k_tokens
(使用率と重複)。必要になったらセッション行の値へ足す。

出力先は環境変数で上書きできる: RUNCAT_OUT_FILE (既定 ~/.claude/runcat-usage.json)
と RUNCAT_SESSIONS_OUT_FILE (既定は同じディレクトリの runcat-sessions.json)。
レート制限の控えとセッションごとの状態も同じディレクトリに置く。

`--seed` を付けて実行すると、空のカードだけ作って終わる。RunCat へソースを登録
する時点でファイルが必要なので、セットアップ時に tasks/claude.yml から呼ぶ。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# レート制限のカード。RunCat には 2 つのソースを別々に登録する
OUT = Path(os.environ.get("RUNCAT_OUT_FILE", str(Path.home() / ".claude" / "runcat-usage.json")))

# 動いているセッションのカード (同じ名前のディレクトリはセッション状態の置き場。別物)
SESSIONS_OUT = Path(os.environ.get(
    "RUNCAT_SESSIONS_OUT_FILE", str(OUT.parent / "runcat-sessions.json")))

# statusLine で取れたレート制限の控え (usage API が使えないときのフォールバック)
RATE_LIMITS_CACHE = OUT.parent / "runcat-rate-limits.json"

# Claude Code の /usage と同じ使用量エンドポイント。statusLine が渡す rate_limits は
# five_hour と seven_day だけで、モデル別の週間制限はここからしか取れない。
# 非公式・非文書化のため、壊れたら黙って statusLine の控えへ落ちる (行が消えるだけ)。
# RUNCAT_USAGE_URL はテストから file:// のスタブを差すための差し替え口。
USAGE_URL = os.environ.get("RUNCAT_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
USAGE_CACHE = OUT.parent / "runcat-usage-limits.json"

# hook は毎ツール呼び出しで走るので、この間隔より短ければ控えを使い回す
USAGE_MAX_AGE_SECONDS = 60

# hook を待たせないための上限。落ちても諦めて次の実行に任せる
USAGE_TIMEOUT_SECONDS = 3

# Claude Code が OAuth トークンを預けている keychain のサービス名
KEYCHAIN_SERVICE = "Claude Code-credentials"

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

# 可変長の値をこの表示幅で切る (全角は 2 幅)。カードが横に伸びるのを防ぐ。
# `45.5% · Opus 5 · xhigh · 11h13m` のような一番長い形がちょうど収まる幅
MAX_VALUE_WIDTH = 32

# セッション行のラベル (プロジェクト名) をこの表示幅で切る
MAX_LABEL_WIDTH = 20

# 2 枚のカードの見出しとアイコン (SF Symbol)
LIMIT_CARD_TITLE = "Claude Code"
LIMIT_CARD_SYMBOL = "staroflife"
SESSION_CARD_TITLE = "Claude Sessions"
SESSION_CARD_SYMBOL = "rectangle.stack"


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
    return "".join(kept).rstrip(" ·") + "…"  # 途中で切れた区切りは残さない


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


def snapshot(title, symbol, metrics, bar_value=None):
    result = {
        "title": title,
        "symbol": symbol,
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


# --- 使用量エンドポイント --------------------------------------------------------

def oauth_token():
    """Claude Code の OAuth トークンを keychain から読む。

    値はこのプロセスの中だけで使い、控えにもログにも書かない。
    """
    proc = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        return None
    account = obj(json.loads(proc.stdout), "claudeAiOauth")
    return account.get("accessToken") or account.get("access_token")


def fetch_usage():
    """使用量エンドポイントを叩いて生のレスポンスを返す。"""
    headers = {"anthropic-beta": "oauth-2025-04-20"}
    if not USAGE_URL.startswith("file:"):  # file: はテスト用スタブ
        token = oauth_token()
        if not token:
            return None
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(USAGE_URL, headers=headers)
    with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_SECONDS) as response:
        return json.load(response)


def usage_label(entry):
    """limits の 1 件をカードのラベルへ。読めない種別は捨てる。"""
    kind = entry.get("kind")
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "7d"
    if kind == "weekly_scoped":
        model = obj(entry, "scope", "model").get("display_name")
        return f"7d {model}" if model else None
    return None


def rows_from_usage(payload, stale=False):
    """レスポンスの limits 配列をレート制限の行へ。

    kind と scope からラベルを決めるので、モデル別の枠が増えても拾える。
    """
    rows = []
    limits = payload.get("limits") if isinstance(payload, dict) else None
    for entry in limits or []:
        if not isinstance(entry, dict):
            continue
        used = num(entry.get("percent"))
        label = usage_label(entry)
        if used is None or not label:
            continue
        resets_at = parse_ts(entry.get("resets_at"))
        rows.append({
            "label": label,
            "used_percentage": used,
            "resets_at": resets_at.timestamp() if resets_at else None,
            "stale": stale,
        })
    return rows


def usage_limits(now_epoch):
    """使用量エンドポイントから取れたレート制限の行。取れなければ空。

    hook は毎ツール呼び出しで走るため、USAGE_MAX_AGE_SECONDS の間は控えを使う。
    再取得に失敗したときは古い控えで凌ぐ (使用率は上がる一方なので ≥ を付ける)。
    """
    try:
        cached = json.loads(USAGE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cached = None
    fetched_at = num(cached.get("fetched_at")) if isinstance(cached, dict) else None
    if fetched_at is not None and now_epoch - fetched_at < USAGE_MAX_AGE_SECONDS:
        return rows_from_usage(cached.get("payload"))

    try:
        payload = fetch_usage()
    except Exception:
        payload = None
    if payload is not None:
        try:
            write_json(USAGE_CACHE, {"fetched_at": now_epoch, "payload": payload})
        except Exception:
            pass
        return rows_from_usage(payload)
    if isinstance(cached, dict) and cached.get("payload"):
        return rows_from_usage(cached["payload"], stale=True)
    return []


def rows_from_windows(windows, stale):
    """statusLine 由来の five_hour / seven_day を行へ (usage API のフォールバック)。"""
    rows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = windows.get(key) or {}
        used = num(window.get("used_percentage"))
        if used is None:
            continue
        rows.append({
            "label": label,
            "used_percentage": used,
            "resets_at": num(window.get("resets_at")),
            "stale": stale,
        })
    return rows


# --- レート制限の控え (statusLine 由来) --------------------------------------------

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
    """セッション行のラベル。

    一覧ではラベルが唯一の手掛かりなので、同じものが並ばないよう必要な分だけ
    細かくする: プロジェクト名 → ブランチを添える → 同じ worktree で複数開いて
    いるならセッション名。幅が限られるので、区別に要る分しか足さない。
    """
    def base(s):
        return s.get("project") or s.get("title") or "session"

    label = base(state)
    same = [s for s in states if base(s) == label]
    if len(same) <= 1:
        return clip(label, MAX_LABEL_WIDTH)

    branch = state.get("branch")
    if not branch:
        return clip(label, MAX_LABEL_WIDTH)
    if sum(1 for s in same if s.get("branch") == branch) <= 1:
        return clip(f"{label} · {branch}", MAX_LABEL_WIDTH)
    # プロジェクトもブランチも同じ。残る手掛かりはセッション名だけ
    return clip(state.get("title") or f"{label} · {branch}", MAX_LABEL_WIDTH)


def head(value, keep):
    """` · ` 区切りの値から先頭 keep 個だけ残す。

    Model は `Opus 5 · xhigh · think · fast`、Elapsed は `45m · API 2m18s` の
    ように詳細が続く。1 行へ畳むと長すぎるので、一覧では頭だけ使う。
    """
    parts = [p for p in str(value or "").split(" · ") if p]
    return " · ".join(parts[:keep]) or None


def session_summary(state):
    """セッション行の値。使用率・モデル・経過を 1 行へ畳む。"""
    ctx_pct = num(state.get("ctx_pct"))
    parts = [
        f"{round(ctx_pct, 1):g}%" if ctx_pct is not None else None,  # 一覧では小数第 1 位で十分
        head(state.get("model"), 2),   # モデル名と effort まで
        head(state.get("elapsed"), 1),  # API 時間は落とす
    ]
    return clip(" · ".join(p for p in parts if p)) or None


# --- カードの組み立て -----------------------------------------------------------

def build_limit_card(limit_rows, now_epoch):
    """レート制限のカード。5 時間・週間・モデル別が 1 行ずつ並ぶ。"""
    metrics = [
        rate_limit_row(limit["label"], limit, now_epoch, stale=limit.get("stale", False))
        for limit in limit_rows
    ]
    # メニューバーは週間 (全モデル) の使用率
    weekly = next((limit for limit in limit_rows if limit["label"] == "7d"), {})
    return snapshot(LIMIT_CARD_TITLE, LIMIT_CARD_SYMBOL, metrics,
                    fmt_bar_value(weekly, stale=weekly.get("stale", False)))


def build_session_card(states):
    """動いているセッションのカード。セッションごとに 1 行。"""
    metrics = []
    for state in states:
        ctx_pct = num(state.get("ctx_pct"))
        metrics.append(row(
            session_label(state, states),
            session_summary(state),
            ctx_pct / 100 if ctx_pct is not None else None,
        ))
    # メニューバーは一番文脈を使っているセッションの使用率 (圧縮が近いものが分かる)
    used = [p for p in (num(s.get("ctx_pct")) for s in states) if p is not None]
    bar_value = f"{max(used):.0f}%" if used else None
    return snapshot(SESSION_CARD_TITLE, SESSION_CARD_SYMBOL, metrics, bar_value)


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

def render(state, limit_rows, now_epoch):
    """自分の状態を保存し、2 枚のカードを書き出す。"""
    save_session(state)
    states = load_sessions(now_epoch, current_id=state.get("session_id"))
    write_json(OUT, build_limit_card(limit_rows, now_epoch))
    write_json(SESSIONS_OUT, build_session_card(states))


def resolve_limits(payload, now_epoch):
    """出すべきレート制限の行を決める。

    使用量エンドポイントが使えればそれだけで足りる (5h / 週間 / モデル別が揃う)。
    落ちているときは statusLine 入力、それも無ければ控えへ順に落ちる。
    """
    rows = usage_limits(now_epoch)
    if rows:
        return rows
    windows = {
        "five_hour": obj(payload, "rate_limits", "five_hour"),
        "seven_day": obj(payload, "rate_limits", "seven_day"),
    }
    rows = rows_from_windows(windows, stale=False)
    if rows:
        return rows
    return rows_from_windows(load_rate_limits(now_epoch), stale=True)


def seed_cards():
    """空のカードだけ先に置く (`--seed`)。

    RunCat の Custom Metrics はソースを登録する時点でファイルが存在している
    必要がある。Claude Code を一度も動かしていない新しい環境でも登録できるよう、
    セットアップ時にここで作っておく。中身は次の実行で埋まる。
    既にあるカードは触らない (走らせ直しても消さない)。
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    for path, card in ((OUT, build_limit_card([], now_epoch)),
                       (SESSIONS_OUT, build_session_card([]))):
        if not path.exists():
            write_json(path, card)


def main():
    if "--seed" in sys.argv[1:]:
        seed_cards()
        return

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
            render(state, resolve_limits({}, now_epoch), now_epoch)
        except Exception:
            pass
        return

    state, model_name = from_statusline(payload)

    # 使用量エンドポイントが落ちたときのために statusLine 由来の値を控えておく
    five_hour = obj(payload, "rate_limits", "five_hour")
    seven_day = obj(payload, "rate_limits", "seven_day")
    if five_hour or seven_day:
        save_rate_limits({"five_hour": five_hour, "seven_day": seven_day})

    render(state, resolve_limits(payload, now_epoch), now_epoch)
    print(model_name)


if __name__ == "__main__":
    main()
