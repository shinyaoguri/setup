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
#                                   → gh へ渡る引数 (タイトル・インライン本文・ラベル等)、
#                                     heredoc とヒア文字列の本文、--body-file の中身
#
# 照合するのは「実際に外へ出るもの」だけで、コマンド文字列全体ではない (issue #128)。
# 連結された別コマンド (cat > <パス> / cd <パス> / mkdir …)・リダイレクト先・
# --body-file のパス表記は公開されないので落とす — スクラッチパッドのパスはセッションの
# cwd から組まれるため、旧リポジトリで作業している限り必ず旧世界の語を含み、本文が清潔でも
# 止まっていた。パス表記を 1 つずつ除く denylist ではなく、切り出す側を構造で絞る。
# 切り出しは引用を意識して行う: 引用の中の | や && で区切ると、markdown テーブルを含む
# 本文が照合から落ちて漏れになる。
#
# 残る穴: 同じコマンドの中で heredoc・ヒア文字列以外の手段で本文を作る形
# (echo "…" > body.md && gh … --body-file body.md、パイプで stdin へ流す形) は、本文を
# 作る側のセグメントが照合から落ちる。フック実行時点ではファイルがまだ無く中身も読めない
# (書いた後の投稿なら --body-file の中身として読める)。issue #145
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

# 関係ないコマンドでは切り分けごと省く (フックは Bash の呼び出しごとに走る)。
# ここは発火条件より広い粗い網で、絞り込みは kind 判定が行う
printf '%s' "$command" | grep -qE "${SEP}(git|gh)([[:space:]]|$)" || exit 0

# --- コマンド文字列の切り分け -------------------------------------------------
# コマンド文字列を 4 つの見方で切り出す:
#   shell    … heredoc 本文を除いたシェルコード (発火判定と宛先判定に使う。本文に
#              引用された --repo を宛先の材料にしないため)
#   heredoc  … heredoc とヒア文字列の本文。本文の常態で、cat > <パス> <<EOF の形では
#              フック実行時点でファイルがまだ無い (--body-file の中身が読めない) ので、
#              ここが唯一の手掛かりになる
#   gh       … gh 呼び出しのセグメントで gh へ渡る引数だけ (リダイレクト先と
#              --body-file のパスは公開されないので落とす)
#   bodyfile … gh へ渡る --body-file / -F の値 (中身は後で読む)
#
# 切り出しは awk に寄せている: 実機と CI は bash 3.2 前提 (read -N が無い) で、文字単位の
# 走査は awk の substr が素直なため。bash 3.2 は $( ) の中の heredoc も解けないので、
# プログラム本体は関数で包んでから取り出す
awk_prog() {
  cat <<'AWK'
BEGIN { delim = ""; dash = 0; qn = 0; qi = 0; code = "" }

{
  if (delim != "") {                       # heredoc 本文の途中
    cmp = $0
    if (dash) sub(/^[ \t]+/, "", cmp)      # <<- は終端語のインデントを許す
    if (cmp == delim) advance()
    else if (mode == "heredoc") print
    next
  }
  code = code $0 "\n"
  if (mode == "heredoc") here_strings($0)
  markers($0)
}

END {
  if (mode == "shell") printf "%s", code
  else if (mode == "gh" || mode == "bodyfile") segments(code)
}

function push(d, isdash) {                 # 終端語を待ち行列へ積む
  qn++; q[qn] = d; qd[qn] = isdash
  if (delim == "") { qi = qn; delim = d; dash = isdash }
}

function advance() {                       # 次の heredoc へ移る (無ければ本文の外へ)
  if (qi < qn) { qi++; delim = q[qi]; dash = qd[qi] } else { delim = "" }
}

# ヒア文字列 (<<<) の中身は本文そのもの (リダイレクト先のパスではない)。gh の stdin へ
# 渡る形 (--body-file -) も、ファイルへ書いてから渡す形もあるので本文として照合する
function here_strings(line,   t) {
  t = line
  while (match(t, /<<<[ \t]*("[^"]*"|'[^']*'|[^ \t;&|<>]+)/)) {
    print substr(t, RSTART, RLENGTH)
    t = substr(t, RSTART + RLENGTH)
  }
}

# 行に現れる heredoc の終端語を順に積む (<<< はヒア文字列なので heredoc ではない)
function markers(line,   t, tok, d, c, isdash) {
  t = line
  gsub(/<<</, "\002", t)
  while (match(t, /<<-?[ \t]*("[^"]*"|'[^']*'|\\?[A-Za-z_][A-Za-z0-9_]*)/)) {
    tok = substr(t, RSTART, RLENGTH)
    t = substr(t, RSTART + RLENGTH)
    isdash = (substr(tok, 3, 1) == "-")
    d = tok
    sub(/^<<-?[ \t]*/, "", d)
    c = substr(d, 1, 1)
    if (c == "\"" || c == "'") d = substr(d, 2, length(d) - 2)
    else if (c == "\\") d = substr(d, 2)
    push(d, isdash)
  }
}

# シェルコードを引用を意識して区切り、gh 呼び出しのセグメントだけを扱う。
# リダイレクト (> >> < 2>&1) は演算子ごと宛先を落とす — 宛先のパスは公開されない
function segments(s,   i, n, ch, quote, seg) {
  quote = ""; seg = ""; n = length(s)
  for (i = 1; i <= n; i++) {
    ch = substr(s, i, 1)
    if (quote != "") {
      seg = seg ch
      if (ch == "\\" && quote == "\"") { i++; seg = seg substr(s, i, 1) }
      else if (ch == quote) quote = ""
      continue
    }
    if (ch == "\\") { seg = seg ch; i++; seg = seg substr(s, i, 1); continue }
    if (ch == "\"" || ch == "'") { quote = ch; seg = seg ch; continue }
    if (ch == "#" && (seg == "" || seg ~ /[ \t]$/)) {   # 行末コメントは公開されない
      while (i <= n && substr(s, i, 1) != "\n") i++
      i--                                    # 改行はセグメントの区切りとして処理させる
      continue
    }
    if (ch == ">" || ch == "<") {
      sub(/[0-9]+$/, "", seg)              # 2> の fd 番号も落とす
      i = skip_redirect(s, i)
      continue
    }
    if (ch == ";" || ch == "|" || ch == "&" || ch == "\n") { flush(seg); seg = ""; continue }
    seg = seg ch
  }
  flush(seg)
}

function skip_redirect(s, i,   n, ch, quote) {
  n = length(s)
  while (i <= n && (substr(s, i, 1) == ">" || substr(s, i, 1) == "<" || substr(s, i, 1) == "&")) i++
  while (i <= n && (substr(s, i, 1) == " " || substr(s, i, 1) == "\t")) i++
  quote = ""
  while (i <= n) {
    ch = substr(s, i, 1)
    if (quote != "") { if (ch == quote) quote = ""; i++; continue }
    if (ch == "\"" || ch == "'") { quote = ch; i++; continue }
    if (ch == " " || ch == "\t" || ch == "\n" || ch == ";" || ch == "|" || ch == "&") break
    i++
  }
  return i - 1                             # 呼び出し側の for が ++ する
}

function flush(seg) {
  if (seg !~ /(^|[ \t])gh([ \t]|$)/) return
  if (mode == "gh") emit_args(seg)
  else if (mode == "bodyfile") emit_bodyfiles(seg)
}

function emit_args(seg,   i, n, t, want) {
  n = tokenize(seg); want = 0
  for (i = 1; i <= n; i++) {
    t = tk[i]
    if (want) { want = 0; continue }       # --body-file の値 = パス表記なので落とす
    if (t == "-F" || t == "--body-file") { want = 1; continue }
    if (t ~ /^--body-file=/) continue
    if (substr(t, 1, 2) == "-F" && length(t) > 2) continue
    print t
  }
}

function emit_bodyfiles(seg,   i, n, t, v, want) {
  n = tokenize(seg); want = 0
  for (i = 1; i <= n; i++) {
    t = tk[i]
    if (want) { want = 0; print dequote(t); continue }
    if (t == "-F" || t == "--body-file") { want = 1; continue }
    if (t ~ /^--body-file=/) { v = t; sub(/^--body-file=/, "", v); print dequote(v); continue }
    if (substr(t, 1, 2) == "-F" && length(t) > 2) print dequote(substr(t, 3))
  }
}

# セグメントをトークンへ割る (引用の中の空白では切らない)。引用符は落とさない —
# 照合には無害で、報告する一致行は打った形のまま見せたい
function tokenize(seg,   i, n, ch, quote, cur, count) {
  n = length(seg); quote = ""; cur = ""; count = 0
  for (i = 1; i <= n; i++) {
    ch = substr(seg, i, 1)
    if (quote != "") {
      if (ch == "\\" && quote == "\"") { cur = cur ch substr(seg, i + 1, 1); i++; continue }
      if (ch == quote) quote = ""
      cur = cur ch
      continue
    }
    if (ch == "\\") { cur = cur ch substr(seg, i + 1, 1); i++; continue }
    if (ch == "\"" || ch == "'") { quote = ch; cur = cur ch; continue }
    if (ch == " " || ch == "\t" || ch == "\n") {
      if (cur != "") { tk[++count] = cur; cur = "" }
      continue
    }
    cur = cur ch
  }
  if (cur != "") tk[++count] = cur
  return count
}

function dequote(v) { gsub(/["']/, "", v); return v }
AWK
}

awk_slice=$(awk_prog)

slice() { # $1=mode → コマンド文字列から切り出した文字列
  printf '%s' "$command" | awk -v mode="$1" "$awk_slice"
}

shell_code=$(slice shell)

readonly GH="${SEP}gh([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+"

# 発火するコマンドか (push / gh 投稿) を先に判定する。それ以外は無条件で素通し。
# 見るのは heredoc 本文の外だけ — 本文に引用したコマンド例では発火させない
kind=''
if printf '%s' "$shell_code" | grep -qE "${SEP}git([[:space:]]+(-C[[:space:]]+[^[:space:]]+|-[^[:space:]]+))*[[:space:]]+push([[:space:]]|$)"; then
  kind=push
elif printf '%s' "$shell_code" | grep -qE "${GH}(issue|pr)[[:space:]]+(create|comment|edit)([[:space:]]|$)"; then
  kind=gh
elif printf '%s' "$shell_code" | grep -qE "${GH}pr[[:space:]]+review([[:space:]]|$)" &&
  printf '%s' "$shell_code" | grep -qE '(^|[[:space:]])(-b|--body|-F|--body-file)([[:space:]]|=)'; then
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
  v=$(printf '%s' "$shell_code" |
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
  for w in $shell_code; do
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

# gh の照合対象: gh へ渡る引数 + heredoc 本文 + --body-file の中身
gh_args=''
heredocs=''
body_files=''
if [ "$kind" = gh ]; then
  gh_args=$(slice gh)
  # gh セグメントを取り切れない書き方 (想定外の引用等) では全体へ倒す。漏れよりは誤検知
  [ -n "$gh_args" ] || gh_args=$shell_code
  heredocs=$(slice heredoc)
  body_files=$(slice bodyfile)
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
        # ここの "~" は **case のパターン**で、展開させたくない (展開すると
        # ルール側が書いた ~ と照合できない)。直後に $HOME へ置き換えている
        # shellcheck disable=SC2088
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
      sample=$(printf '%s' "$gh_args" | grep -iE -m 3 -- "$term") &&
        report_hit "$rules" "$term" "gh へ渡る引数 (タイトル・本文・ラベル等)" "$sample"
      sample=$(printf '%s' "$heredocs" | grep -iE -m 3 -- "$term") &&
        report_hit "$rules" "$term" "heredoc・ヒア文字列の本文 (--body / --body-file へ渡る本文)" "$sample"
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
