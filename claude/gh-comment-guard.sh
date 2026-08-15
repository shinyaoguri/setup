#!/usr/bin/env bash
# Claude Code の PreToolUse フック: Claude が書いた Issue / PR コメントに、Claude が
# 書いたと分かる署名が付いているかを確かめる。
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
# 判定はコメントを投稿するコマンドだけに絞る:
#   - gh issue comment / gh pr comment            → 本文に署名が要る
#   - gh pr review で本文オプションが付くもの      → 同上 (--approve だけなら本文が無い)
#   - それ以外 (view/list、gh api、--help)        → 素通し
#
# ask ではなく deny なのは、署名を足せばその場で続行できるから (人を呼ぶ必要がない)。
#
# 契約: stdin に PreToolUse の JSON。素通しは無出力 + 終了コード 0。
# 呼び出し口は settings.json の hooks.PreToolUse、テストは
# claude/tests/gh_comment_guard_test.py (python3 で直接実行)。

set -uo pipefail

# 本文末尾に足す署名。CLAUDE.md の規約と同じ文字列を持つ (変えるときは両方)。
# 検知は SIGNATURE_KEY の固定文字列で行うので、リンク先が変わっても効き続ける
readonly SIGNATURE='<sub>🤖 Assisted by [Claude Code](https://claude.com/claude-code)</sub>'
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

deny "$(cat <<EOF
コメント本文に Claude の署名がありません。

同じ Issue には人間も複数のエージェントも書き込みます。署名が無いと、後から読む人
(と、記憶がリセットされた次のセッションの自分) が発言の出どころを判別できません。

本文の末尾に次の 2 行を足してから、同じコマンドで投稿し直してください:

---
$SIGNATURE

(--body-file を使っているならファイルの末尾へ足してください。署名の文言は
~/.claude/CLAUDE.md が正本です)
EOF
)"
