#!/usr/bin/env bash
# Claude Code の PreToolUse フック: プロビジョニングを流す前に、何が変わるかを先に見る。
#
# git-safety-guard.sh は git の破壊的操作しか見ておらず、ansible のように
# **マシンの状態をまとめて書き換える**コマンドは素通りしていた。実際、宣言と実態が
# ずれたまま playbook を流し、user.email が意図せず書き換わった事故がある
# (気付いたのは changed の数を目で追っていたからで、検出が運に依存していた)。
#
# ansible は冪等性を前提にしているので --check --diff が正確な予告になる。先に
# dry-run し、変わるものが無ければ黙って通す。**人間を呼ぶのは実際に何かが変わるとき
# だけ**で、流し直すだけの実行は止めない。
#
# 予告を組み立てられない形 (複合コマンド・dry-run の失敗・時間切れ) は ask に倒す。
# 判定不能を安全側に寄せるのは git-safety-guard.sh と同じ方針。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
# 呼び出し口は settings.json の hooks.PreToolUse、テストは
# claude/tests/provisioning_preflight_test.py (python3 で直接実行)。

set -uo pipefail

# dry-run に許す秒数。settings.json 側の timeout より短くしておく
# (フックごと殺されると判断を返せないため)。実測: --tags git で約 3 秒、全体で約 13 秒。
# テストから短く上書きするためだけに環境変数を見る。
readonly DRY_RUN_LIMIT=${PROVISIONING_PREFLIGHT_LIMIT:-25}

decide() { # $1=allow|deny|ask  $2=理由
  jq -n --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r}}'
  exit 0
}

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$command" ] || exit 0

# 対象は ansible-playbook のみ。コマンド区切りで割ってから**先頭の語**を見る
# (`echo ansible-playbook` のように引数として現れるだけのものを拾わないため)。
printf '%s' "$command" | tr ';&|' '\n' |
  grep -qE '^[[:space:]]*ansible-playbook([[:space:]]|$)' || exit 0
# dry-run そのものは状態を変えないので素通しする (二重に走らせない)。
printf '%s' "$command" | grep -qE '(^|[[:space:]])(--check|-C)([[:space:]]|$)' && exit 0

# `cd <dir> && ansible-playbook …` は頻出なので、cd 先を取り出して残りを予告に回す。
workdir="."
rest="$command"
if printf '%s' "$command" | grep -qE '^[[:space:]]*cd[[:space:]]+[^&;|]+&&'; then
  workdir=$(printf '%s' "$command" |
    sed -E 's/^[[:space:]]*cd[[:space:]]+([^&;|]+)&&.*/\1/' | sed -E 's/[[:space:]]+$//')
  rest=$(printf '%s' "$command" | sed -E 's/^[[:space:]]*cd[[:space:]]+[^&;|]+&&[[:space:]]*//')
fi

# ここから先は「ansible-playbook 単体」でないと予告を組み立てられない。
if printf '%s' "$rest" | grep -qE '[;&|]'; then
  decide ask "ansible-playbook が他のコマンドと繋がっているため、変更内容を先に見ることができない。

ansible-playbook だけを単体で実行すれば、このフックが --check --diff で予告する。
そのうえで実行するなら、何が変わるのかをユーザーへ伝えて判断を仰ぐ。"
fi

# --- dry-run で予告を取る ---------------------------------------------------
report=$(mktemp)
trap 'rm -f "$report"' EXIT

(cd "$workdir" 2>/dev/null && eval "$rest --check --diff") >"$report" 2>&1 &
runner=$!

waited=0
while kill -0 "$runner" 2>/dev/null; do
  if [ "$waited" -ge "$DRY_RUN_LIMIT" ]; then
    kill -TERM "$runner" 2>/dev/null
    wait "$runner" 2>/dev/null
    decide ask "変更内容の事前確認 (--check) が ${DRY_RUN_LIMIT} 秒で終わらなかったため、何が変わるか分からない。

--tags で対象を絞れば予告が早く返る。範囲を絞れないなら、実行前にユーザーへ確認する。"
  fi
  sleep 1
  waited=$((waited + 1))
done
wait "$runner"
dry_run_status=$?

if [ "$dry_run_status" -ne 0 ]; then
  decide ask "変更内容の事前確認 (--check) が失敗したため、何が変わるか分からない。

$(tail -20 "$report")

このまま流すと予期しない変更が入りうる。原因を確かめるか、ユーザーへ確認する。"
fi

# PLAY RECAP の changed=N が予告。読めなければ判定不能として ask に倒す。
changed=$(grep -oE 'changed=[0-9]+' "$report" | tail -1 | cut -d= -f2)
if [ -z "$changed" ]; then
  decide ask "変更内容の事前確認 (--check) の結果を読み取れなかった (PLAY RECAP が無い)。

$(tail -20 "$report")"
fi

# 変わるものが無いなら人間を呼ばない (冪等な流し直し)。
[ "$changed" -eq 0 ] && exit 0

decide ask "この実行で ${changed} 件が変更される。**中身を確かめてから**実行する。

$(grep -E '^(TASK \[|changed: |--- before|\+\+\+ after|@@|[-+][^-+])' "$report" | head -60)

確かめること:
- 変更が意図したものか (宣言と実際の設定がずれていると、意図せず宣言側へ引き戻される)
- 引き戻される値が今の運用より古くないか (ずれているなら、直すべきは playbook 側かもしれない)

意図しない変更が混ざっているなら、実行せずユーザーへ伝える。"
