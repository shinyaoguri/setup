#!/usr/bin/env bash
# Claude Code の PreToolUse フック: 世代交代したリポジトリの公開面に旧世界の語が
# 出ていくのを止める汎用ガード (term-guard)。
#
# リポジトリの作り直し (世代交代) では「新リポジトリの公開面 — push されるコミット・
# Issue/PR の投稿本文 — に旧世界の名前を一切出さない」という要件が生じる。人間規約では
# 守りきれないので機械で強制する。ただし「どのリポジトリで・どの語を禁じるか」という
# ルール自体が新旧の対応関係を明かす機微情報で、この public リポジトリには書けない。
# そこで機構 (このスクリプト・汎用) とルール (ローカル管理外) を分離する。ルールの正本は
# 各プロジェクト側の private リポジトリで管理し、ローカルへ配って使う (issue #121)。
#
# 検査対象:
#   - git push                      → push 範囲のコミット (--branches --not --remotes の
#                                     メッセージ + 差分)。git commit と連結されたコマンド
#                                     ならワーキングツリーの差分も見る
#   - gh issue|pr create|comment|edit / 本文つき gh pr review
#                                   → コマンド文字列 (タイトル・インライン本文・heredoc)
#                                     と --body-file の中身。--body-file のパス表記自体は
#                                     公開されない (誤検知源) ので照合から除く
#
# 適用判定は宛先優先: 宛先リポジトリ (gh は -R/--repo → cwd の origin、push はリモート)
# が判定できるならルールの repo glob とだけ照合し、判定できないときだけ cwd をルールの
# path prefix と照合する。旧リポジトリの作業ディレクトリから新リポジトリへ投稿する形
# こそ検査が要り、逆に新リポジトリの作業ディレクトリから旧リポジトリ (記録の置き場) へ
# 旧世界の語を含む記録を書くのは正当なため、cwd では発火しない。
#
# ルール書式 (~/.claude/term-guard.d/*.rules。1 ファイル = 1 つの守り):
#   # コメント (行頭 # と空行のみ。term 行の後ろにコメントは書けない)
#   repo <宛先リポジトリの glob>     例: neworld-org/*
#   path <cwd の前方一致。~ 可>      例: ~/Repos/newrepo
#   term <禁止パターン (ERE)>        例: oldname   — 大文字小文字は区別しない
#
# ルールファイルが 1 件も無ければ何もしない (どのマシンでも安全に有効化できる)。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
# 呼び出し口は settings.json の hooks.PreToolUse、テストは
# claude/tests/term_guard_test.py (python3 で直接実行)。
set -uo pipefail

RULES_DIR="${TERM_GUARD_RULES_DIR:-$HOME/.claude/term-guard.d}"

deny() { # $1=理由
  jq -n --arg r "$1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
  exit 0
}

# ルールが無い環境では読み込みごと省いて無音で抜ける
ls "$RULES_DIR"/*.rules >/dev/null 2>&1 || exit 0

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$command" ] || exit 0
cwd=$(printf '%s' "$payload" | jq -r '.cwd // ""' 2>/dev/null) || cwd=''

readonly SEP='(^|[;&|[:space:]])'
readonly GH="${SEP}gh([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+"

# 発火するコマンドか (push / gh 投稿) を先に判定する。それ以外は無条件で素通し
kind=''
if printf '%s' "$command" | grep -qE "${SEP}git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|-[^[:space:]]+))*[[:space:]]+push([[:space:]]|$)"; then
  kind=push
elif printf '%s' "$command" | grep -qE "${GH}(issue|pr)[[:space:]]+(create|comment|edit)([[:space:]]|$)"; then
  kind=gh
elif printf '%s' "$command" | grep -qE "${GH}pr[[:space:]]+review([[:space:]]|$)" &&
  printf '%s' "$command" | grep -qE '(^|[[:space:]])(-b|--body|-F|--body-file)([[:space:]]|=)'; then
  kind=gh
fi
[ -n "$kind" ] || exit 0

trim() { # $1=文字列 → 前後の空白を落として出力
  local s=$1
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

normalize_slug() { # $1=URL または slug → owner/repo (形が読めなければそのまま)
  printf '%s' "$1" |
    sed -E 's#^(git@[^:/]+:|ssh://(git@)?[^/]+/|https?://[^/]+/)##; s#\.git/?$##; s#^/+##; s#/+$##'
}

gh_target() { # gh の宛先 slug。-R/--repo が無ければ cwd の origin。判定不能なら失敗
  local v
  v=$(printf '%s' "$command" |
    grep -oE "(^|[[:space:]])(-R|--repo)([[:space:]]+|=)(\"[^\"]*\"|'[^']*'|[^[:space:];&|]+)" |
    head -n 1 |
    sed -E "s/^.*(-R|--repo)([[:space:]]+|=)//; s/^\"(.*)\"\$/\1/; s/^'(.*)'\$/\1/")
  if [ -n "$v" ]; then
    normalize_slug "$v"
    return 0
  fi
  v=$(git -C "$cwd" remote get-url origin 2>/dev/null) || return 1
  normalize_slug "$v"
}

push_target() { # git push の宛先 slug。リモート引数が無ければ origin。判定不能なら失敗
  local seen=false w url
  set -f
  for w in $command; do
    if $seen; then
      case "$w" in
      '&&' | ';' | '||' | '|') break ;;
      -*) : ;;
      *)
        set +f
        case "$w" in
        *://* | *@*:*)
          normalize_slug "$w"
          return 0
          ;;
        *)
          url=$(git -C "$cwd" remote get-url "$w" 2>/dev/null) || return 1
          normalize_slug "$url"
          return 0
          ;;
        esac
        ;;
      esac
    else
      [ "$w" = push ] && seen=true
    fi
  done
  set +f
  url=$(git -C "$cwd" remote get-url origin 2>/dev/null) || return 1
  normalize_slug "$url"
}

report_hit() { # $1=ルールファイル $2=パターン $3=検出箇所 $4=一致行 (最大 3 行)
  deny "$(cat <<EOF
公開面に出せない語が含まれています (term-guard)。

  ルール:   $1
  パターン: $2 (大文字小文字は区別しない)
  検出箇所: $3
$(printf '%s\n' "$4" | sed 's/^/    | /')

宛先リポジトリの公開面 (コミット・Issue/PR の本文) にこの語は出せません。該当箇所を
除去または言い換えてから、同じコマンドを再実行してください。誤検知ならルールファイル
のパターンを見直してください (ルールの正本の場所はルールファイル冒頭のコメント参照)。
EOF
)"
}

target_slug=''
case "$kind" in
push) target_slug=$(push_target) || target_slug='' ;;
gh) target_slug=$(gh_target) || target_slug='' ;;
esac

# gh の照合対象: コマンド文字列 (--body-file のパス表記は除く) + body-file の中身
stripped=$command
body_files=''
if [ "$kind" = gh ]; then
  body_file_tokens=$(printf '%s' "$command" |
    grep -oE "(^|[[:space:]])(-F|--body-file)([[:space:]]+|=)(\"[^\"]*\"|'[^']*'|[^[:space:];&|]+)")
  while IFS= read -r tok; do
    [ -n "$tok" ] || continue
    stripped=${stripped/"$tok"/ }
    body_files="$body_files$(printf '%s' "$tok" |
      sed -E "s/^.*(-F|--body-file)([[:space:]]+|=)//; s/^\"(.*)\"\$/\1/; s/^'(.*)'\$/\1/")
"
  done <<EOF
$body_file_tokens
EOF
fi

for rules in "$RULES_DIR"/*.rules; do
  [ -f "$rules" ] || continue

  applicable=false
  terms=''
  while IFS= read -r line; do
    line=$(trim "$line")
    case "$line" in '' | '#'*) continue ;; esac
    key=${line%%[[:space:]]*}
    val=$(trim "${line#"$key"}")
    [ -n "$val" ] || continue
    case "$key" in
    repo)
      if [ -n "$target_slug" ]; then
        # shellcheck disable=SC2254  # val を glob として使うのが仕様
        case "$target_slug" in $val) applicable=true ;; esac
      fi
      ;;
    path)
      if [ -z "$target_slug" ]; then
        case "$val" in "~" | "~/"*) val="$HOME${val#"~"}" ;; esac
        case "$cwd" in "$val" | "$val"/*) applicable=true ;; esac
      fi
      ;;
    term)
      terms="$terms$val
"
      ;;
    esac
  done <"$rules"

  $applicable || continue
  [ -n "$terms" ] || continue

  while IFS= read -r term; do
    [ -n "$term" ] || continue
    if [ "$kind" = push ]; then
      sample=$(git -C "$cwd" log --no-color -p --branches --not --remotes 2>/dev/null |
        grep -iE -m 3 -- "$term") &&
        report_hit "$rules" "$term" \
          "push 範囲のコミット (特定するには: git log -p --branches --not --remotes | grep -inE -- '$term')" \
          "$sample"
      if printf '%s' "$command" | grep -qE "${SEP}git([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+commit([[:space:]]|$)"; then
        sample=$(git -C "$cwd" diff --no-color HEAD 2>/dev/null | grep -iE -m 3 -- "$term") &&
          report_hit "$rules" "$term" "ワーキングツリーの差分 (このコマンドで commit → push される)" "$sample"
      fi
      sample=$(printf '%s' "$command" | grep -iE -m 3 -- "$term") &&
        report_hit "$rules" "$term" "コマンド文字列 (コミットメッセージ等)" "$sample"
    else
      sample=$(printf '%s' "$stripped" | grep -iE -m 3 -- "$term") &&
        report_hit "$rules" "$term" "コマンド文字列 (タイトル・本文)" "$sample"
      while IFS= read -r f; do
        [ -n "$f" ] && [ -f "$f" ] || continue
        sample=$(grep -iE -m 3 -- "$term" "$f" 2>/dev/null) &&
          report_hit "$rules" "$term" "本文ファイル $f" "$sample"
      done <<EOF2
$body_files
EOF2
    fi
  done <<EOF3
$terms
EOF3
done

exit 0
