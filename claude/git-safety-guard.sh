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

has() { printf '%s' "$command" | grep -qE "$1"; }

# git のサブコマンドの手前にはグローバルオプション (-C <path> など) が入りうるので、
# 「git … <サブコマンド>」の間は緩く見る。ask 止まりなので多少の過検出は許容する。
readonly GIT='(^|[;&|[:space:]])git([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+'
# サブコマンドと注目する引数の間。引数がサブコマンドの直後に来る場合もあるので丸ごと省ける
readonly ARG='([[:space:]]|$)(.*[[:space:]])?'

# --- 1. 作業ツリー・履歴を捨てる操作 ---------------------------------------
danger=""
has "${GIT}reset${ARG}--hard" &&
  danger="git reset --hard は、まだコミットしていない変更を復元できない形で捨てる"
has "${GIT}clean${ARG}(--force|-[a-zA-Z]*f)" &&
  danger="git clean -f は追跡していないファイルを削除する (ゴミ箱には入らない)"
has "${GIT}checkout${ARG}(--([[:space:]]|$)|\.([[:space:]]|$))" &&
  danger="git checkout での作業ツリーの取り消しは、その変更を復元できない"
has "${GIT}restore([[:space:]]|$)" && ! has '\-\-staged' &&
  danger="git restore は作業ツリーの変更を復元できない形で捨てる"
has "${GIT}branch${ARG}-D([[:space:]]|$)" &&
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
