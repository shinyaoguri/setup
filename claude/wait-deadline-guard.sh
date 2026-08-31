#!/usr/bin/env bash
# Claude Code の PreToolUse フック: 外の状態が変わるのを待つポーリングループに、
# 期限が付いているかを確かめる。
#
# 塞ぐ実害は issue #136。mokume の PR #644 で CI の完了を待つループを
# run_in_background へ投げたら、PR が merge された後も 1 時間 7 分走り続けた。
# merge 済みの PR に残った check run が status=IN_PROGRESS・conclusion=SUCCESS という
# 組み合わせで固まっていて、`status != COMPLETED` を停止条件にしたループは永久に
# 真にならなかった。人間に指摘されるまで気付いていない。
#
# **フォアグラウンドの Bash には上限があるが、バックグラウンドには無い。** 前者は
# harness が既定 120 秒・最大 600 秒で殺すので、条件を書き間違えても必ず終わる。
# 後者はセッションが終わるまで残り、「まだ終わっていない」と「永久に終わらない」が
# 外から区別できない。
#
# **停止条件を丁寧に書くことでは塞げない。** 待ち先が返す値は待つ側の管轄外で、
# 今回のように仕様の外の組み合わせが返ることが実際にある。だから見るのは条件の
# 正しさではなく、**越えたら殺す期限があるか**だけにしてある。
#
# 判定はポーリングループだけに絞る:
#   - while / until のいずれかがあり、かつ sleep がある  → 期限が要る
#   - for ... in {1..N} / $(seq …) のような回数の決まった繰り返し → 素通し
#   - ループの無いコマンド (ビルド・サーバの起動・単発の実行)   → 素通し
#
# 期限として認めるのは 2 つだけ。曖昧な数え上げ (自前のカウンタ変数など) は読み取ろうと
# しない — 認める形を狭く固定するほうが、差し戻しの文面がそのまま打ち直せる形になる。
#   - $SECONDS を条件に混ぜる   ループ自身が抜ける。**macOS の既定はこちら**
#   - timeout <秒> …            外から殺す。coreutils を入れた環境と CI 用
#
# **勧める順が $SECONDS なのは、macOS に timeout(1) が無いからである。** GNU coreutils
# の道具で、brew install coreutils を入れても名前は gtimeout になる。文面が先に
# timeout を出していた版は、そのまま打ち直すと command not found になった (実測)。
#
# run_in_background かどうかは見ない。フォアグラウンドは harness の上限で必ず終わるので
# 実害は薄いが、**ツール入力のどのキーに載るかへ依存しない**ほうが「黙って効かなくなる」
# 側へ倒れない (~/.claude/CLAUDE.md「フックが黙っていることを『安全である』と読まない」)。
# 期限を足す手間は 1 語なので、フォアグラウンドまで要求しても損は小さい。
#
# ask ではなく deny なのは、正しい形が機械的に決まっていてその場で打ち直せるから
# (人を呼ぶ必要がない)。
#
# やらないこと: Monitor ツールは対象外。あちらは timeout_ms が必須で、無期限にできるのは
# persistent: true を明示したときだけ — 期限が既に入力の一部になっている。
#
# 環境変数: CLAUDE_WAIT_DEADLINE_GUARD=0 で無効化する。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
# 呼び出し口は settings.json の hooks.PreToolUse、テストは
# claude/tests/wait_deadline_guard_test.py (python3 で直接実行)。

set -uo pipefail

[ "${CLAUDE_WAIT_DEADLINE_GUARD:-1}" = "0" ] && exit 0

deny() { # $1=理由
  jq -n --arg r "$1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
  exit 0
}

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$command" ] || exit 0

has() { printf '%s' "$command" | grep -qE "$1"; }

# 語の切れ目。引用符を含めるのは `bash -c '"'"'until …'"'"'` の形を見落とさないため —
# これは差し戻しの文面が勧める形そのものなので、ここが抜けると勧めた形だけが
# 検査の外に出る (期限を外した版も一緒に素通しする)
readonly EDGE="(^|[;&|(){}'\"[:space:]])"
# 語として現れる while / until。変数名やパス (`$until_at`, `./while.sh`) には反応しない
readonly LOOP="${EDGE}(while|until)[[:space:]]"
# sleep も同じく語で見る。sleep 0.5 のような小数も拾う
readonly SLEEP="${EDGE}sleep[[:space:]]+[0-9.]"

has "$LOOP" || exit 0
has "$SLEEP" || exit 0

# --- 期限があるか ----------------------------------------------------------

# timeout(1)。`timeout 900 bash -c …` / `timeout --signal=KILL 30 …` のどちらも拾う。
# macOS の GNU coreutils は gtimeout の名前で入るので、そちらも認める
readonly TIMEOUT_CMD="${EDGE}g?timeout[[:space:]]+(-|[0-9])"
# $SECONDS をループの条件に混ぜる形。$SECONDS / ${SECONDS} / (( SECONDS < … )) を拾う
readonly SECONDS_BOUND='(\$\{?SECONDS\}?|\(\([^)]*SECONDS)'

has "$TIMEOUT_CMD" && exit 0
has "$SECONDS_BOUND" && exit 0

deny "$(cat <<'EOF'
外の状態を待つループに期限がありません (while / until + sleep)。

停止条件が永久に真にならないことは実際に起きます。待ち先が返す値は待つ側の管轄外で、
仕様の外の組み合わせが返ることがあるためです (setup#136: merge 済みの PR に
status=IN_PROGRESS・conclusion=SUCCESS のまま残った check run を `status != COMPLETED` で
待って、1 時間 7 分走り続けた)。

**とくに run_in_background では上限がありません。** フォアグラウンドは harness が
既定 120 秒・最大 600 秒で殺しますが、バックグラウンドはセッションが終わるまで残り、
「まだ終わっていない」と「永久に終わらない」が外から区別できません。

次のどちらかの形で打ち直してください。

  # ループ自身が抜ける (推奨。macOS に timeout(1) は無い)
  end=$((SECONDS + 900)); until <条件> || [ "$SECONDS" -ge "$end" ]; do sleep 20; done

  # 外から殺す (timeout(1) がある環境のみ。GNU coreutils で、macOS では gtimeout)
  timeout 900 bash -c 'until <条件>; do sleep 20; done'

あわせて、停止条件が**終端状態を全部**見ているかを確かめてください。成功の印だけを
待つ条件は、失敗・中断・仕様外の状態のときに黙って回り続けます。

(このフックは ~/.claude/wait-deadline-guard.sh。CLAUDE_WAIT_DEADLINE_GUARD=0 で無効化)
EOF
)"
