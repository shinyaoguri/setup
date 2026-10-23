#!/usr/bin/env bash
# Claude Code の PreToolUse フック: Claude が書いた Issue / PR のコメントと、新しく作る
# Issue / PR の本文に、Claude が書いたと分かる署名が付いているかを確かめる。
#
# 同じ Issue に人間と複数のエージェントが書き込むようになり、後から読む人 (と次の
# セッションの自分) が発言の出どころを判別できなくなった。ChatGPT Codex は GitHub App
# 経由で投稿するため GitHub が「commented with ChatGPT Codex Connector」を描いてくれるが、
# Claude Code はローカルの gh CLI = 本人のトークンで投稿するので、その表示は出ない
# (performed_via_github_app が null になる)。settings.json の attribution も commit /
# pr / sessionUrl だけで、コメントには効かない。
#
# 一方 Claude Code の cloud routines / Web は、投稿するコメントの末尾に帰属の 1 行を
# 既定で付けている (anthropics/claude-code#62791)。ローカル CLI にだけその経路が無い
# ので、同じことを本文の署名で埋める。
#
# 署名そのものを付けるのは Claude の仕事 (規約は claude/CLAUDE.md)。ここが見るのは
# 「付いているか」だけ — PreToolUse フックはツール入力を書き換えられない仕様なので、
# 付け忘れを deny で差し戻すのが決定論的に効かせられる唯一の形。
#
# 判定はエージェントが新しく文を書くコマンドだけに絞る:
#   - gh issue comment / gh pr comment            → 本文に署名が要る
#   - gh pr review で本文オプションが付くもの      → 同上 (--approve だけなら本文が無い)
#   - gh issue create / gh pr create / gh pr revert → 同上 (#317)。本文の旗の有無は問わない
#                                                   (--fill やテンプレートも本文を作る)
#   - それ以外 (view/list、edit、merge --body、gh api、--help) → 素通し
#
# 本文の作成を足したのは、本文の署名が覚えていれば付くだけになっていたから (#317)。
# mokume では sub-issue 11 件中 9 件・PR の本文 30 件すべてが無署名で、本文の署名で
# エージェントの起票を見分ける道具が人の起票と読んでいた (mokume-metal/mokume#3149)。
# edit を見ないのは、人が書いた本文をエージェントが直すこともあり、そこへ署名を求めると
# 出所の嘘になるからである。
#
# 差し戻しの文面に貼る 1 行は、エージェントのモデルを名乗る (#317):
#
#   <sub>🤖 Assisted by [Claude Code](https://claude.com/claude-code) (Opus 5.5, Haiku 5.5)</sub>
#
# モデルは payload の transcript_path が指すセッションの記録と、サブエージェントの記録
# (<記録>/subagents/*.jsonl) の assistant の行の message.model から読む。主が最後に使った
# モデルを先頭に、主の残り、サブエージェントにだけ出るモデルの順に並べる。括弧の中で
# Claude を繰り返さないのは、頭の [Claude Code] と重なるため (メンテナの指定)。読めなければ
# モデルは書かない — 推測で書いたモデル名は出所の嘘になる。照合はモデルに依らず
# SIGNATURE_KEY で見るので、名乗りの有無で通る / 通らないは変わらない。
#
# ask ではなく deny なのは、署名を足せばその場で続行できるから (人を呼ぶ必要がない)。
#
# 環境変数: CLAUDE_GH_COMMENT_GUARD=0 で無効化する。署名の作法はリポジトリごとに違い
# (投稿ラッパーを持ち、素の gh を先に deny するリポジトリがある)、そこでは同種の機構が
# 二重に動く。向きは「リポ側が担保して個人側を黙らせる」で固定してあるので、黙る口は
# こちら側が持つ。前例は plan-record.sh の CLAUDE_PLAN_RECORD=0。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
# 呼び出し口は settings.json の hooks.PreToolUse、テストは
# claude/tests/gh_comment_guard_test.py (python3 で直接実行)。

set -uo pipefail

# 自前で署名を担保しているリポジトリは、ここを黙らせて自分の機構だけを効かせる。
# 既定は有効で、0 以外は「無効化の意思表示ではない」と読む
[ "${CLAUDE_GH_COMMENT_GUARD:-1}" = "0" ] && exit 0

# 本文末尾に足す署名の頭。CLAUDE.md の規約と同じ文字列を持つ (変えるときは両方)。
# 後ろにモデルの名乗り「 (Opus 5.5)」と「</sub>」が続く。検知は SIGNATURE_KEY の
# 固定文字列で行うので、リンク先やモデルの名乗りが変わっても効き続ける
readonly SIGNATURE_HEAD='<sub>🤖 Assisted by [Claude Code](https://claude.com/claude-code)'
readonly SIGNATURE_KEY='Assisted by [Claude Code]'

deny() { # $1=理由
  jq -n --arg r "$1" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
  exit 0
}

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$command" ] || exit 0

# gh のサブコマンドの手前にはグローバルオプション (-R owner/repo など) が入りうるので、
# 「gh … <サブコマンド>」の間は緩く見る
readonly GH='(^|[;&|[:space:]])gh([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+'

is_comment_command() {
  printf '%s' "$command" | grep -qE "${GH}(issue|pr)[[:space:]]+comment([[:space:]]|$)" && return 0
  # 新しく作る本文 (#317)
  printf '%s' "$command" |
    grep -qE "${GH}(issue[[:space:]]+create|pr[[:space:]]+(create|revert))([[:space:]]|$)" && return 0
  # レビューは本文を伴うときだけ。--approve / --request-changes だけなら発言が無い
  printf '%s' "$command" | grep -qE "${GH}pr[[:space:]]+review([[:space:]]|$)" &&
    printf '%s' "$command" | grep -qE '(^|[[:space:]])(-b|--body|-F|--body-file)([[:space:]]|=)' &&
    return 0
  return 1
}

is_comment_command || exit 0

# 使い方を尋ねているだけなら投稿ではない
printf '%s' "$command" | grep -qE '(^|[[:space:]])(-h|--help)([[:space:]]|$)' && exit 0

# 本文は --body の直書き・heredoc・--body-file のどれでも来る。前の二つはコマンド文字列に
# そのまま現れるので、コマンド文字列と --body-file が指すファイルの中身を合わせて見る
haystack=$command

body_files=$(printf '%s' "$command" |
  grep -oE "(^|[[:space:]])(-F|--body-file)([[:space:]]+|=)(\"[^\"]*\"|'[^']*'|[^[:space:];&|]+)" |
  sed -E "s/^.*(-F|--body-file)([[:space:]]+|=)//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/")

while IFS= read -r file; do
  [ -n "$file" ] || continue
  # 読めないパス (これから作る、変数のまま、標準入力の -) は判断材料が無いだけ。
  # ここで落とさず、コマンド文字列側の署名を見て判断する
  [ -f "$file" ] || continue
  haystack="$haystack
$(cat "$file" 2>/dev/null)"
done <<EOF
$body_files
EOF

printf '%s' "$haystack" | grep -qF "$SIGNATURE_KEY" && exit 0

# モデルの ID を表示名へ畳む。claude-opus-5-5 → Opus 5.5、日付の接尾辞は落とす。
# 形が読めない ID はそのまま返す (知らない形を作り替えると別のモデルを名乗りかねない)
model_display_name() { # $1=モデルの ID
  local id=${1%-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]}
  if [[ $id =~ ^claude-([a-z]+)-([0-9]+)(-([0-9]{1,2}))?$ ]]; then
    local family=${BASH_REMATCH[1]}
    printf '%s%s %s' "$(tr '[:lower:]' '[:upper:]' <<<"${family:0:1}")" "${family:1}" \
      "${BASH_REMATCH[2]}${BASH_REMATCH[4]:+.${BASH_REMATCH[4]}}"
  else
    printf '%s' "$1"
  fi
}

# 記録の assistant の行の message.model を 1 行ずつ。ツールの結果や本文に映った "model" の
# 字面は拾わない (読むのは各行の message.model だけ。grep で絞るのは速さのため)
transcript_models() { # $1=記録のパス…
  local f
  for f in "$@"; do
    [ -f "$f" ] || continue
    grep -E '"type": ?"assistant"' "$f" | jq -r '.message.model // empty' 2>/dev/null
  done | grep -vxF '<synthetic>'
}

# このセッションで使ったモデルの名乗り → "Opus 5.5, Haiku 5.5" (読めなければ空)
session_models() { # $1=記録のパス
  local transcript=$1 line out=''
  [ -f "$transcript" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    line=$(model_display_name "$line")
    case ", $out, " in *", $line, "*) continue ;; esac
    out=${out:+$out, }$line
  done < <(transcript_models "$transcript" | tail -n 1
           transcript_models "$transcript" | sort | uniq -c | sort -rn | awk '{print $2}'
           transcript_models "${transcript%.jsonl}"/subagents/*.jsonl |
             sort | uniq -c | sort -rn | awk '{print $2}')
  printf '%s' "$out"
}

transcript=$(printf '%s' "$payload" | jq -r '.transcript_path // empty' 2>/dev/null)
models=$(session_models "$transcript")
signature="$SIGNATURE_HEAD${models:+ ($models)}</sub>"

deny "$(cat <<EOF
本文に Claude の署名がありません。

同じ Issue には人間も複数のエージェントも書き込みます。署名が無いと、後から読む人
(と、記憶がリセットされた次のセッションの自分) が発言の出どころを判別できません。

本文の末尾に次の 2 行を足してから、同じコマンドで投稿し直してください:

---
$signature

(--body-file を使っているならファイルの末尾へ足してください。別のエージェント・モデルと
一緒に作ったなら、括弧の後ろに「 and <名乗り> (<モデル>)」と併記します。署名の文言は
~/.claude/CLAUDE.md が正本です)
EOF
)"
