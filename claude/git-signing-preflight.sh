#!/usr/bin/env bash
# Claude Code の PreToolUse フック: コミット署名 (1Password の op-ssh-sign) の事前確認。
#
# 1Password の SSH agent は署名のたびに承認を求めることがあり、要求元がフォアグラウンドの
# GUI アプリでないときはダイアログを出さずメニューバーで知らせるだけになる。誰も気付かない
# まま 60 秒でタイムアウトして署名が落ちる (1Password のログ上は ssh authorization prompt
# timed out)。そのうえ失敗するたび再試行すると承認待ちが積み上がる。
#
# そこで git commit などを実行する前に、短いタイムアウトで捨て置きの署名を 1 回試す。
#   - 通れば以降の本番の署名も同じセッション承認で通る (ウォームアップ)
#   - 通らなければ 60 秒待たずにその場で止め、何をすれば直るかを伝える (即中断)
# あわせて、署名なしコミットへの逃げ道もここで塞ぐ。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0、拒否は
# permissionDecision=deny の JSON。呼び出し口は settings.json の hooks.PreToolUse、
# テストは claude/tests/git_signing_preflight_test.py (python3 で直接実行)。
#
# テスト用の差し替え口:
#   CLAUDE_SIGNING_PREFLIGHT_SIGNER   op-ssh-sign のパス (既定は gpg.ssh.program)
#   CLAUDE_SIGNING_PREFLIGHT_TIMEOUT  ウォームアップ署名の制限時間 (秒)

set -uo pipefail

readonly TIMEOUT="${CLAUDE_SIGNING_PREFLIGHT_TIMEOUT:-10}"

# deny の理由文はそのままエージェントへの指示になるので「次に何をするか」まで書く。
# ここで自動リトライを禁じておかないと、承認待ちが積み上がって状況が悪化する。
readonly NEXT_STEP='そのうえで同じコマンドをもう一度だけ実行する。それでも通らなければ手を止めてユーザーに報告し、指示を仰ぐ。同じコマンドを繰り返し試さない (承認待ちが積み上がって状況が悪化する)。署名のバイパスは最後まで選択肢にしない。'

# 1Password が終了していると agent も一緒に止まる。ソケットのファイルだけは残るので
# 「設定は正しいのに繋がらない」に見える。
readonly REMEDY_NOT_RUNNING="1Password アプリが起動していない (SSH agent はアプリと一緒に止まる)。
対処: 1Password を起動する。ウィンドウを閉じるたびに終了してしまう場合は、1Password の設定で「メニューバーに 1Password を表示し続ける」を有効にすると再発しない。
${NEXT_STEP}"

readonly REMEDY_NO_RESPONSE="1Password が承認を待っている (プロンプトが背面にいる) か、ロックされている可能性が高い。
対処は順に:
(1) メニューバーの 1Password アイコンを確認する。承認プロンプトが背面にいることがある (60 秒で失効する)
(2) 1Password を開いてロックを解除する。60 分で自動ロックされ、画面ロック中は Touch ID すら出せない
${NEXT_STEP}"

readonly REMEDY_GENERIC="対処: 1Password が起動していること、ロックが解除されていること、承認プロンプトが背面で待っていないことを順に確かめる。
${NEXT_STEP}"

deny() {
  jq -n --arg reason "$1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
  exit 0
}

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$command" ] || exit 0

# 署名なしコミットは禁止 (全コミット Verified 運用)
if printf '%s' "$command" | grep -qiE -- '--no-gpg-sign|gpgsign[[:space:]=]+(false|0)'; then
  deny "署名なしコミットは禁止 (全コミット Verified 運用)。--no-gpg-sign / commit.gpgsign=false は使わない。
${REMEDY_GENERIC}"
fi

# 署名が走るサブコマンドだけを対象にする。git のグローバルオプションには値を別の語で取る
# ものがある (git -C <path> commit) ので、正規表現ではなく語を順に見てサブコマンドを決める。
needs_signing() {
  local raw="$1" segment
  set -f # サブコマンドの判定にファイル名展開は要らない
  while IFS= read -r segment; do
    set -- $segment
    # コマンド名の手前に付くもの (VAR=1 git ... / env git ...) を読み飛ばす
    while [ "$#" -gt 0 ]; do
      case "$1" in
      *=* | env | sudo | command | nohup) shift ;;
      *) break ;;
      esac
    done
    case "${1:-}" in
    git | */git) shift ;;
    *) continue ;;
    esac
    while [ "$#" -gt 0 ]; do
      case "$1" in
      -C | -c | --git-dir | --work-tree | --exec-path | --namespace)
        shift
        [ "$#" -gt 0 ] && shift
        ;;
      -*) shift ;;
      commit | merge | revert | cherry-pick | rebase | tag)
        set +f
        return 0
        ;;
      *) break ;;
      esac
    done
  done <<EOF
$(printf '%s' "$raw" | tr ';&|\n' '\n\n\n\n')
EOF
  set +f
  return 1
}

needs_signing "$command" || exit 0

signer="${CLAUDE_SIGNING_PREFLIGHT_SIGNER:-$(git config --get gpg.ssh.program 2>/dev/null)}"
# 1Password 以外で署名している環境ではこのフックの出番はない
case "$signer" in
*op-ssh-sign) ;;
*) exit 0 ;;
esac
[ -x "$signer" ] || deny "コミット署名の署名プログラムが見つからない: ${signer}
gpg.ssh.program が指す op-ssh-sign が実行できない。1Password がインストールされているか確認する。"

key=$(git config --get user.signingkey 2>/dev/null)
[ -n "$key" ] && [ -r "$key" ] || deny "コミット署名の公開鍵が読めない: ${key:-(user.signingkey が未設定)}
ansible-playbook playbook_sillicon_mac.yml --tags git で設定し直せる (setup リポジトリ)。"

# 署名の置き場は空のディレクトリにする。op-ssh-sign は署名先の名前を渡した名前から
# 組み立てる (拡張子を .sig に差し替える) ので、決め打ちにすると取りこぼす。
tmpdir=$(mktemp -d -t signing-preflight) || exit 0
trap 'rm -rf "$tmpdir"' EXIT
tmp="$tmpdir/payload"
out="$tmpdir/output"
printf 'preflight' >"$tmp"

# 捨て置きの署名を 1 回試す。承認プロンプトが出たまま返ってこない場合に備えて自前で打ち切る
# (macOS の素の環境に timeout(1) はない)。
"$signer" -Y sign -n git -f "$key" "$tmp" >"$out" 2>&1 &
signer_pid=$!
timed_out=""
waited=0
while kill -0 "$signer_pid" 2>/dev/null; do
  if [ "$waited" -ge "$((TIMEOUT * 10))" ]; then
    kill -TERM "$signer_pid" 2>/dev/null
    timed_out=1
    break
  fi
  sleep 0.1
  waited=$((waited + 1))
done
wait "$signer_pid"
signer_status=$?

if [ -n "$timed_out" ]; then
  deny "コミット署名の事前確認が ${TIMEOUT} 秒で応答しなかった。
${REMEDY_NO_RESPONSE}"
fi

signature=$(find "$tmpdir" -name '*.sig' -size +0c 2>/dev/null | head -1)
if [ "$signer_status" -ne 0 ] || [ -z "$signature" ]; then
  signer_output=$(head -c 500 "$out" 2>/dev/null)
  # agent へ繋がらないのは 1Password が動いていないとき。原因が確定するので個別に案内する
  case "$signer_output" in
  *"Could not connect to socket"* | *"Is the agent running"*) remedy="$REMEDY_NOT_RUNNING" ;;
  *) remedy="$REMEDY_GENERIC" ;;
  esac
  deny "コミット署名の事前確認に失敗した (終了コード ${signer_status})。
op-ssh-sign の出力: ${signer_output}
${remedy}"
fi

# ここまで来れば署名は通る。本番のコミットは同じセッション承認に乗る
exit 0
