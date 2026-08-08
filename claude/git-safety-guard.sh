#!/usr/bin/env bash
# Claude Code の PreToolUse フック: 取り返しのつかない git 操作を水際で止める。
#
# CLAUDE.md には「不可逆な操作だけは事前にユーザーへ確認する」と書いてあるが、文書ルールは
# advisory なので、実際に取りこぼす (コミットが失敗して HEAD が動いていないのに
# git reset --hard HEAD~1 を実行し、作業ツリーの変更ごと巻き戻した事故があった)。
# 決定論的に効く場所へ移した形。
#
# 二つを見る:
#   1. 作業ツリーや履歴を捨てる操作 → ask (ユーザーに判断を返す)
#   2. 秘密情報らしきファイルのコミット → deny (代替を添えて止める)
#
# ただし 1 は「取り返しがつかない」から止めるのであって、**その場で可逆と確認できた
# ものまで止めない**。確認は構文 (コマンド文字列のパターン) ではなくリポジトリの状態で
# 行い、確認できなかったものだけ ask に残す (判定不能は安全側)。これが無いと、
# マージ済みブランチの掃除のような日常操作まで毎回ユーザーを呼ぶことになる。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
# 呼び出し口は settings.json の hooks.PreToolUse、テストは
# claude/tests/git_safety_guard_test.py (python3 で直接実行)。

set -uo pipefail

decide() { # $1=allow|deny|ask  $2=理由
  jq -n --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r}}'
  exit 0
}

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$command" ] || exit 0

# git のサブコマンドの手前にはグローバルオプション (-C <path> など) が入りうるので、
# 「git … <サブコマンド>」の間は緩く見る。ask 止まりなので多少の過検出は許容する。
readonly GIT='(^|[;&|[:space:]])git([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+'
# サブコマンドと注目する引数の間。引数がサブコマンドの直後に来る場合もあるので丸ごと省ける
readonly ARG='([[:space:]]|$)(.*[[:space:]])?'

# エイリアスに包まれた危険操作を見えるようにする。`git gone-clean` のようなエイリアスは
# 展開しない限り `git branch -D` が文字列に現れず、検査を素通りしてしまう。
# 展開結果を元コマンドの後ろに連結し、検査だけをその合成文字列に対して行う
# (実行されるのは元コマンドのまま)。
expand_aliases() {
  local name definition expanded="$command"
  for name in $(printf '%s' "$command" | grep -oE "${GIT}[a-zA-Z][-a-zA-Z0-9_]*" |
    awk '{print $NF}' | sort -u); do
    definition=$(git config --get "alias.$name" 2>/dev/null) || continue
    [ -n "$definition" ] || continue
    # シェルエイリアス (`!cmd`) の先頭 ! を落とす。付いたままだと `!git …` となり、
    # 危険パターンが要求する「区切り文字 + git」に一致せず素通りしてしまう
    expanded="$expanded ; ${definition#!}"
  done
  printf '%s' "$expanded"
}

scan=$(expand_aliases)
has() { printf '%s' "$scan" | grep -qE "$1"; }

# --- 可逆性の判定 -----------------------------------------------------------
# いずれも「可逆と確認できたときだけ真」。リポジトリの外・判定材料が足りないときは
# 偽を返し、呼び出し側で ask に落とす。

# `git branch -D` の対象がすべて「追跡先が消えたブランチ」か。
#
# squash merge では feature ブランチのコミットが main の祖先にならないため
# `--merged` では判定できない。代わりに upstream の消失 ([gone]) を役目終了の
# 代理指標に使う (グローバル CLAUDE.md のブランチ掃除運用と同じ判定)。内容は main に
# 入っており、元コミットも GitHub 側に refs/pull/<N>/head として残るため可逆。
branch_delete_targets_are_gone() {
  local targets target track
  targets=$(printf '%s' "$command" | awk '{
    for (i = 1; i <= NF; i++)
      if ($i == "-D") { for (j = i + 1; j <= NF; j++) print $j; exit }
  }')
  [ -n "$targets" ] || return 1

  for target in $targets; do
    # 変数・引用符が残るものは名前を確定できない (判定不能 → 安全側)
    case "$target" in
      -*|*'$'*|*'"'*|*"'"*|*'`'*) return 1 ;;
    esac
    track=$(git for-each-ref --format='%(upstream:track)' "refs/heads/$target" 2>/dev/null)
    [ "$track" = "[gone]" ] || return 1
  done
  return 0
}

# エイリアス経由の `git branch -D` が「gone なブランチだけ」を対象にすると確認できるか。
# 対象がループ変数になるため名前では判定できないので、エイリアスの定義そのものを見る:
# `git gone` (= upstream:track が [gone] のものを列挙) の出力だけをループしている形なら、
# 消えるのは定義上 gone なブランチに限られる。定義を書き換えれば一致しなくなり ask に戻る。
alias_deletes_only_gone_branches() {
  local gone_definition name definition
  gone_definition=$(git config --get alias.gone 2>/dev/null) || return 1
  printf '%s' "$gone_definition" | grep -q '\[gone\]' || return 1

  for name in $(printf '%s' "$command" | grep -oE "${GIT}[a-zA-Z][-a-zA-Z0-9_]*" |
    awk '{print $NF}' | sort -u); do
    definition=$(git config --get "alias.$name" 2>/dev/null) || continue
    printf '%s' "$definition" | grep -qE '^!git gone([[:space:]]|\|)' || continue
    printf '%s' "$definition" | grep -qE 'git branch[[:space:]]+-D' || continue
    return 0
  done
  return 1
}

# `git reset --hard` で失われるものが無いか。作業ツリーに未コミットの変更が無く
# (捨てる変更が無い)、かつ現在の HEAD がリモートに存在する (コミットは remote から
# 取り戻せる) なら可逆。どちらか一方でも欠ければ ask に落とす。
reset_hard_discards_nothing() {
  git rev-parse --git-dir >/dev/null 2>&1 || return 1
  [ -z "$(git status --porcelain 2>/dev/null)" ] || return 1
  [ -n "$(git branch -r --contains HEAD 2>/dev/null)" ] || return 1
  return 0
}

# --- 1. 作業ツリー・履歴を捨てる操作 ---------------------------------------
danger=""
has "${GIT}reset${ARG}--hard" && ! reset_hard_discards_nothing &&
  danger="git reset --hard は、まだコミットしていない変更を復元できない形で捨てる"
has "${GIT}clean${ARG}(--force|-[a-zA-Z]*f)" &&
  danger="git clean -f は追跡していないファイルを削除する (ゴミ箱には入らない)"
has "${GIT}checkout${ARG}(--([[:space:]]|$)|\.([[:space:]]|$))" &&
  danger="git checkout での作業ツリーの取り消しは、その変更を復元できない"
has "${GIT}restore([[:space:]]|$)" && ! has '\-\-staged' &&
  danger="git restore は作業ツリーの変更を復元できない形で捨てる"
has "${GIT}branch${ARG}-D([[:space:]]|$)" &&
  ! branch_delete_targets_are_gone && ! alias_deletes_only_gone_branches &&
  danger="git branch -D はマージ済みかを問わずブランチを消す"
has "${GIT}push${ARG}(--force|-f([[:space:]]|$))" &&
  danger="force push は remote の履歴を書き換える (他の作業や PR に影響する)"
has "${GIT}stash${ARG}(drop|clear)" &&
  danger="git stash drop / clear は退避した変更を消す"

if [ -n "$danger" ]; then
  decide ask "${danger}。実行前にユーザーへ確認する。

先に確かめること:
- 直前のコマンドは本当に成功したか (コミットが失敗していれば HEAD は動いていない。git log -1 と git status で今の位置を確かめる)
- 捨てずに済む手はないか (退避なら git stash、ステージだけ戻すなら git restore --staged、コミットの取り消しなら git revert)
- 消す対象が本当にそれだけか (git status --short で範囲を見る)

そのうえで必要なら、何を捨てるのかを伝えてユーザーの判断を仰ぐ。"
fi

# --- 2. 秘密情報らしきファイルのコミット -----------------------------------
# .env.example のような雛形は対象外。gitignore が効いていれば下の検査には現れないので、
# ここに出てくる時点で「入れてはいけないものが漏れている」状態。
readonly SECRET_PATTERN='(^|/)\.env($|\.[^/]*$)|(^|/)id_(rsa|dsa|ecdsa|ed25519)$|\.(pem|p12|pfx|jks|keystore)$|(^|/)[^/]*_rsa$'
readonly SECRET_ALLOW='\.(example|sample|template|dist|pub)$|(^|/)\.env\.(example|sample|template)$'

secrets=""
if has "${GIT}commit([[:space:]]|$)"; then
  secrets=$(git diff --cached --name-only 2>/dev/null |
    grep -E "$SECRET_PATTERN" | grep -vE "$SECRET_ALLOW")
elif has "${GIT}add([[:space:]]|$)"; then
  # add はステージ前なので、コマンドに書かれたパスをそのまま見る
  secrets=$(printf '%s' "$command" | tr ' ' '\n' |
    grep -E "$SECRET_PATTERN" | grep -vE "$SECRET_ALLOW")
fi

if [ -n "$secrets" ]; then
  decide deny "秘密情報が入りうるファイルをコミットに含めようとしている:
$(printf '%s' "$secrets" | sed 's/^/  - /')

コミットに入れない。順に:
(1) .gitignore に追加する (漏れているから検査に引っかかっている)
(2) すでにステージ済みなら git restore --staged <file> で外す
(3) 値が必要なら .env に置いて環境変数から読む。コードやテストのデータには実物ではなくダミー値を書く

雛形として意図的に追いたいなら .env.example のように example / sample / template を付けて置き直す。"
fi

exit 0
