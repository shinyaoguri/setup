#!/usr/bin/env bash
# Claude Code の PreToolUse フック: 取り返しのつかない git 操作を水際で止める。
#
# CLAUDE.md には「不可逆な操作だけは事前にユーザーへ確認する」と書いてあるが、文書ルールは
# advisory なので、実際に取りこぼす (コミットが失敗して HEAD が動いていないのに
# git reset --hard HEAD~1 を実行し、作業ツリーの変更ごと巻き戻した事故があった)。
# 決定論的に効く場所へ移した形。
#
# 四つを見る:
#   1. 捨てる操作のうち、**退避を作れなかった**もの → ask (ユーザーに判断を返す)
#   2. 可逆と確認できたブランチ操作 (掃除・切り替え) → allow (確認を挟まず通す)
#   3. 退避を作って可逆にした操作 → allow (同上)
#   4. 秘密情報らしきファイルのコミット → deny (代替を添えて止める)
#
# **判定軸は「不可逆か」ではなく「退避を作れたか」である** (setup#148)。2 週間の実測で
# ask は 178 回・人が止めたのは 0 回で、確認が判断ではなく反射になっていた。CLAUDE.md が
# 止まる理由として挙げるのは「どこにも残っていないものを壊すとき」なので、退避を作った
# 後の操作を止める理由が無い。だから順序はこうなる:
#
#   退避を作れた  → コマンド全体が読めれば allow、読めなければ素通し (分類器へ戻す)
#   作れなかった  → ask (理由に「何を退避できなかったか」を書く)
#
# **「判定不能は安全側」は、退避を作れなかったときの規律に狭める。** 退避が在るのに
# ask を返しても、押す人へ足せる情報が無い (捨てられる中身は本人にも分からない)。
# 唯一の例外は 3 のエージェント設定ファイルの巻き戻しで、あれは退避があっても通さない。
#
# 2 はその続き。素通し (無出力) は「このフックは異議なし」でしかなく、判定は
# settings.json の permissions へ戻る。allow に載せてよいのは読み取り専用のコマンド
# だけ (claude/tests/settings_test.py が CI で強制) なのでブランチ削除の許可規則は
# 置けず、可逆と確認できた掃除まで結局ユーザーを呼んでいた。確認できたものは
# ここで allow を返して打ち切る。ブランチ切り替え (checkout) も同じ理由でここに居る —
# `checkout` は 1 語で「可逆な切り替え」と「作業ツリーの破棄」の両方を指すので、
# 前方一致の allow 規則では前者だけを表現できない (setup#140)。
#
# 3 はその先。作業ツリーの取り消しは、その場の状態をいくら見ても可逆にならない —
# **捨てられるものがそこに在ること**こそが不可逆の理由なので、1 と 2 の判定方式では
# 永久に ask のまま残る。しかし人間に返しても「そのファイルの未コミット変更が何だったか」
# は判断できず、確認が判断ではなく反射になる。そこで発想を裏返し、可逆性を判定するのでは
# なく**可逆にしてから通す** — 捨てられる内容を先に object DB へ退避する (setup#142)。
#
# setup#148 でこれを 3 つへ広げた。退避の作り手は pin_object に寄せてある:
#
#   作業ツリーの未コミット変更  git stash create のコミットを固定 (setup#142)
#   ブランチの先端            git branch -D の前に refs/heads/<名前> を固定
#   HEAD                     git reset --hard の前に固定
#
# 退避は refs/claude/discarded/* に 30 日残り、`git discarded` で一覧・復元できる
# (tasks/git.yml)。**退避できないものは残っている** — 追跡外ファイルを消す
# `git clean -f` は object DB へ入れられず、-x では .build のような無視対象まで
# 入って費用が非有界になるため、ここは ask のままにしてある。
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

# 前後の空白を落とす。判定はコマンド文字列を語に分けて読むので、どの入口でも最初に通す。
trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }

# コマンドが「単純コマンド 1 つ」として読めない形か。allow はコマンド文字列**全体**に
# 効くので、allow を返す判定はどれも先にここを通す。
#
# **改行は grep の文字クラスでは見えない。** grep は行単位なので `[;&|…]` に改行を
# 足しても当たらず、`^git …` の照合も `read -a` の語分割も 1 行目だけを読む。以前は
# 区切りの検査が grep だけだったため、"git branch -d x<改行>rm -rf …" が 1 行目の形で
# allow になり、2 行目が分類器も確認も通らずに実行できた (issue #202)。
is_compound() { # $1=trim 済みのコマンド
  case "$1" in *$'\n'*) return 0 ;; esac
  printf '%s' "$1" | grep -q '[;&|<>()$`]'
}

# --- 可逆性の判定 -----------------------------------------------------------
# いずれも「可逆と確認できたときだけ真」。リポジトリの外・判定材料が足りないときは
# 偽を返し、呼び出し側で ask に落とす。

# `git branch -D` の対象がすべて「役目を終えたブランチ」か。
branch_delete_targets_are_gone() {
  local targets target
  targets=$(branch_delete_targets)
  [ -n "$targets" ] || return 1

  for target in $targets; do
    # 変数・引用符が残るものは名前を確定できない (判定不能 → 安全側)
    case "$target" in
      -*|*'$'*|*'"'*|*"'"*|*'`'*) return 1 ;;
    esac
    branch_is_spent "$target" || return 1
  done
  return 0
}

# 「マージ済みかを問わず消す」形かどうか。git は 3 通りの綴りを同じ意味に扱う:
#   -D / -d --force / --delete --force (-f は --force の短形)
# -D リテラルだけを見ていると、残り 2 つが「-d だから安全」の側へ落ちて退避も作られない
# まま allow が返る (issue #162)。判定の入口をここ 1 つに寄せる。
# 区切りで割ってからセグメントごとに見る。エイリアス定義は
# `!git gone | while read -r b; do git branch -D "$b"; done` のように削除が区切りの
# 後ろに来るので、最初の区切りで打ち切ると検出できない。フラグはセグメントごとに
# 数え直す (別々のコマンドの -d と --force を足し合わせないため)。
deletes_branch_by_force() {
  printf '%s' "${1:-$command}" |
    awk '{ gsub(/&&|\|\||[;&|]/, "\n"); print }' |
    awk '
      BEGIN { found = 0 }
      {
        del = 0; force = 0
        for (i = 1; i <= NF; i++) {
          if ($i ~ /[<>]/) break
          if ($i == "-D") { del = 1; force = 1 }
          else if ($i == "-d" || $i == "--delete") del = 1
          else if ($i == "-f" || $i == "--force") force = 1
        }
        if (del && force) found = 1
      }
      END { exit !found }'
}

# 削除対象のブランチ名を取り出す。削除フラグの後ろの語のうち、オプションでないものを集める。
# シェルの区切り・リダイレクトが現れたら打ち切る — `git branch -D x 2>&1 | tail -1` の
# "2>&1" までブランチ名として読むと、実在しない名前として判定不能に落ち、掃除のたびに
# ユーザーを呼ぶことになる。
branch_delete_targets() {
  printf '%s' "$command" | awk '{
    seen = 0
    for (i = 1; i <= NF; i++) {
      if ($i ~ /[;&|<>]/) exit
      if ($i == "-D" || $i == "-d" || $i == "--delete") { seen = 1; continue }
      if (!seen) continue
      # 削除フラグの後ろに来る --force / -f などのオプションは対象名ではない
      if ($i ~ /^-/) continue
      print $i
    }
  }'
}

# そのブランチを消しても失うものが無いと確認できるか。指標は upstream の有無で分かれる。
#
# (a) upstream があった → その消失 ([gone]) を役目終了の代理指標にする。squash merge では
#     feature ブランチのコミットが main の祖先にならないため `--merged` では判定できない
#     (グローバル CLAUDE.md のブランチ掃除運用と同じ判定)。内容は main に入っており、
#     元コミットも GitHub 側に refs/pull/<N>/head として残るため可逆。
#
# (b) upstream が無い (= まだ push していない手元だけの枝) → tip が既定ブランチの
#     リモート追跡に含まれるかを見る。含まれていればコミットは remote から取り戻せる。
#
# upstream を持つブランチに (b) を当ててはいけない。「内容が既定ブランチに入っている」だけ
# では*まだ作業中の*ブランチ (main に無い変更をまだ持っていないだけ) と区別できず、生きた
# PR のブランチまで黙って消せてしまう。マージ直後に消したいときは先に `git fetch -p` を
# 通す — [gone] になって (a) を抜ける。
#
# (b) でも「作ったばかりで、まだコミットが無いブランチ」は同じ形に見える。それを消しても
# 失うのはブランチ名だけ (git switch -c で作り直せる) なので、確認を挟むほどではないと
# 判断した — worktree セッションが残す push 前のブランチが溜まる実態のほうが重い。
branch_is_spent() {
  local track
  track=$(git for-each-ref --format='%(upstream:track)' "refs/heads/$1" 2>/dev/null)
  [ "$track" = "[gone]" ] && return 0
  branch_is_local_only_and_contained "$1"
}

# 既定ブランチのリモート追跡参照。origin/HEAD が無いリポでも動くよう名前でも探す。
default_remote_branch() {
  local ref
  ref=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
  if [ -z "$ref" ]; then
    for ref in origin/main origin/master; do
      git rev-parse --verify --quiet "$ref" >/dev/null 2>&1 && break
      ref=""
    done
  fi
  [ -n "$ref" ] || return 1
  printf '%s' "$ref"
}

# push していないブランチで、tip が既定ブランチのリモート追跡に含まれているか。
branch_is_local_only_and_contained() {
  local upstream tip default_ref
  upstream=$(git for-each-ref --format='%(upstream)' "refs/heads/$1" 2>/dev/null)
  [ -z "$upstream" ] || return 1
  tip=$(git rev-parse --verify --quiet "refs/heads/$1") || return 1
  [ -n "$tip" ] || return 1
  default_ref=$(default_remote_branch) || return 1
  git merge-base --is-ancestor "$tip" "$default_ref" 2>/dev/null
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
    deletes_branch_by_force "$definition" || continue
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

# ブランチを離れても失うものが無い状態か。checkout を allow に載せる前提条件。
#
# (a) いまブランチの上に居る (detached HEAD ではない) → 離れても参照から外れる
#     コミットが無い。detached のまま積んだコミットは切り替えた時点で reflog 頼みになる
# (b) 追跡ファイルに未コミットの変更が無い → checkout が捨てられるものが無い
#
# 追跡外のファイルは (b) から外す (`--untracked-files=no`)。checkout はそれを消さず、
# 上書きになる場合は git 自身が断るので失われない。全 clean を要求すると、ビルド生成物が
# 居る実リポジトリでは一度も発火せず、機構として意味を持たなくなる。
worktree_has_nothing_to_lose() {
  git rev-parse --git-dir >/dev/null 2>&1 || return 1
  git symbolic-ref --quiet HEAD >/dev/null 2>&1 || return 1
  [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] || return 1
  return 0
}

# 名前としてそのまま扱える形か。先頭 `-` (オプション) と、引用符・変数・コマンド置換が
# 残るもの (名前を確定できない) を弾く。判定不能は安全側。
checkout_name_is_plain() {
  case "$1" in
    ''|-*|*'$'*|*'"'*|*"'"*|*'`'*|*\\*) return 1 ;;
  esac
  return 0
}

# `git checkout <name>` の <name> が、パスではなくブランチだと確認できるか。
#
# ここが判定の要になる。ブランチが実在すれば git はそちらを採るが、実在しなければ
# **`--` が無くても黙って作業ツリーを index の内容へ戻す** (`Updated 1 path from the
# index` を出して終了コード 0)。つまり ref の実在を確かめない限り、切り替えのつもりの
# 許可が破棄の許可になる。
#
# 参照は `show-ref --verify` で見る。`rev-parse` はリビジョン構文を解釈するので、
# `main@{1}` のような入力が別のコミットに解決されうる。
checkout_target_is_branch() {
  git show-ref --verify --quiet "refs/heads/$1" 2>/dev/null && return 0
  # DWIM: リモート追跡だけがある名前は、同名のローカルブランチが作られて切り替わる。
  # 同名のファイルがあると git は曖昧だと断るので、その形は判定から外す
  [ -e "$1" ] && return 1
  git show-ref --verify --quiet "refs/remotes/origin/$1" 2>/dev/null
}

# --- 捨てられるものの退避 ---------------------------------------------------
# 退避を置く名前空間。refs/heads でも refs/tags でもないので、fetch / push にも
# ブランチの一覧にも現れない。
readonly BACKUP_NS='refs/claude/discarded'
readonly BACKUP_TTL=$((30 * 24 * 3600))

# 固定した ref に付ける連番。同じ秒に 2 つ以上作る (対象が複数のブランチ削除) ときに
# 名前が衝突しないようにする。
PIN_SEQ=0

# コミットを消えない場所へ固定し、その ref を stdout に返す。**ref 名の綴りはここ
# 1 箇所**に置く — 退避の作り手が 3 つに増えたので、名前の付け方が散ると
# `git discarded` 側の読み取りと割れる。
#
# ラベルに `/` を入れてはいけない。ref はディレクトリを作るので、`branch-a` と
# `branch-a/b` が同居できなくなる (呼び手が畳んでから渡す)。
pin_object() { # $1=ラベル $2=リビジョン → ref 名
  local sha ref
  sha=$(git rev-parse --verify --quiet "$2^{commit}" 2>/dev/null) || return 1
  [ -n "$sha" ] || return 1
  PIN_SEQ=$((PIN_SEQ + 1))
  ref="${BACKUP_NS}/$1-$(date +%Y%m%d-%H%M%S)-$$-$PIN_SEQ"
  git update-ref "$ref" "$sha" 2>/dev/null || return 1
  prune_backups "$ref"
  printf '%s' "$ref"
}

# 消そうとしているブランチの先端を固定する。**1 つでも解決できなければ全体を失敗**に
# する — 一部だけ固定して通すと、固定できなかったほうが黙って失われる。
backup_branch_tips() { # $1... = ブランチ名 → "<名前> <ref>" を 1 行 1 件
  local name safe ref out=''
  [ "$#" -gt 0 ] || return 1
  for name in "$@"; do
    git show-ref --verify --quiet "refs/heads/$name" 2>/dev/null || return 1
    safe=$(printf '%s' "$name" | tr '/' '_')
    ref=$(pin_object "branch-$safe" "refs/heads/$name") || return 1
    out="$out$name $ref
"
  done
  printf '%s' "$out"
}

# `git reset --hard` で失われるもの (未コミットの変更と、いまの HEAD) をまとめて固定する。
# 両方作れたときだけ成功する。
backup_reset_hard() { # → "<作業ツリーの ref または空>\n<HEAD の ref>"
  local worktree_ref head_ref
  worktree_ref=$(backup_worktree) || return 1
  head_ref=$(pin_object head HEAD) || return 1
  printf '%s\n%s' "$worktree_ref" "$head_ref"
}

# 捨てられるものを消えない場所へ置き、その参照を stdout に返す。
#
# `git stash create` はコミットオブジェクトを作るだけで**スタックには積まない**。
# グローバル CLAUDE.md が禁じているのは worktree 間で共有されるスタックの push / pop の
# ほうなので、並行する他のセッションと干渉しない。参照されないコミットは gc に
# 落とされるため、作ったコミットは BACKUP_NS の下に固定する。
#
# 追跡外のファイルは stash に含まれないが、checkout / restore はそれを消さない
# (上書きになる場合は git 自身が断る) ので守る必要が無い。追跡外を消す `git clean` を
# この手で通せないのはここが理由で、あちらは対象に載せていない。
#
# 捨てられる変更が無いときは空を返して成功する (取り消しが no-op で、退避が要らない)。
# git リポジトリの外と、コミットがまだ 1 つも無いリポジトリでは失敗する。
backup_worktree() {
  git rev-parse --git-dir >/dev/null 2>&1 || return 1
  [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] || return 0

  local sha
  sha=$(git stash create "claude: 破棄の前に退避" 2>/dev/null) || return 1
  [ -n "$sha" ] || return 1
  pin_object worktree "$sha"
}

# 役目を終えた退避を落とす。放っておくと ref が際限なく増えるので、退避を作った直後に
# だけ掃除する (取り消しは頻度が低く、毎回の Bash で回すほどのものではない)。
#
# **古さは ref 名に埋めた退避の時刻で決める。固定したコミットの日付では決めない。**
# 作業ツリーの退避は `git stash create` がいま作ったコミットなので両者は一致するが、
# ブランチの先端と HEAD は**既存のコミットをそのまま固定する**。committerdate で測ると、
# 先端が 30 日より古いブランチは「退避済み」と allow を返した直後に退避が消える
# (issue #201) — 放置していたブランチこそ、消した後で惜しくなる対象である。
#
# 時刻を読めない名前は落とさない。判定できないものを消す側へ倒さない。
# いま作った ref も名指しで外す (時刻で外れるはずだが、時計が狂っていても消さない)。
backup_stamp() { # $1=ref 名 → YYYYmmddHHMMSS。読めなければ失敗
  # 末尾は pin_object が付ける `-<日付>-<時刻>-<PID>-<連番>`。連番が付く前の命名
  # (`<日付>-<時刻>-<PID>`) も読む。末尾に固定するので、ブランチ名の中に時刻らしき
  # 並びがあってもそちらは読まない。
  [[ "${1##*/}" =~ (^|-)([0-9]{8})-([0-9]{6})-[0-9]+(-[0-9]+)?$ ]] || return 1
  printf '%s%s' "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
}

prune_backups() { # $1=いま作った ref (落とさない)
  local keep="${1:-}" cutoff_epoch cutoff ref stamp
  cutoff_epoch=$(($(date +%s) - BACKUP_TTL))
  # BSD date (macOS) は -r <epoch>、GNU date は -d @<epoch>。ref 名の時刻は pin_object が
  # ローカル時刻で付けているので、こちらもローカル時刻のまま比べる
  cutoff=$(date -r "$cutoff_epoch" +%Y%m%d%H%M%S 2>/dev/null ||
    date -d "@$cutoff_epoch" +%Y%m%d%H%M%S 2>/dev/null) || return 0
  [[ "$cutoff" =~ ^[0-9]{14}$ ]] || return 0

  git for-each-ref --format='%(refname)' "$BACKUP_NS" 2>/dev/null |
    while read -r ref; do
      [ "$ref" != "$keep" ] || continue
      stamp=$(backup_stamp "$ref") || continue
      # 同じ桁数の数字列なので、文字列の比較がそのまま時刻の比較になる
      [[ "$stamp" < "$cutoff" ]] && git update-ref -d "$ref" 2>/dev/null
    done
  return 0
}

# コマンドを区切りで割り、1 セグメント 1 行で返す。引用符は解釈しないので引用の中の
# 区切りでも割れるが、割りすぎる方向にしか外れない — 後続の検査が厳しくなるだけで、
# 判定は安全側に倒れる。
command_segments() {
  trim "$command" | awk '{ gsub(/&&|\|\||[;&|]/, "\n"); print }'
}

# 先頭のコマンドが「リビジョンを伴わない作業ツリーの取り消し」か。通す形は 3 つに固定する:
#
#   git checkout -- <path>...
#   git checkout .
#   git restore [--] <path>...
#
# ここに無い形を載せない理由:
#   - `git checkout <rev> -- <path>` は「いまの内容を捨てる」だけでは済まず、別の版を
#     持ち込む。autoMode.hard_deny の "Auto-Mode Self-Authorization" がまさにこの形
#     (設定ファイルを過去の版へ戻す) を名指ししており、automode-guard.py は
#     Edit|Write matcher なので Bash 経由のそれを見ていない
#   - `-f` / `-p` / `--ours` / `--theirs` / `--worktree` は強制上書き・部分破棄で、
#     退避を作っても「何が起きるか」が変わる。パスは checkout_name_is_plain を通すので、
#     先頭が `-` の語が 1 つでも混ざればここで落ちる
#   - `--` も `.` も無い checkout はブランチの切り替えで、判定は
#     checkout_form_is_switch_only が持つ (こちらの対象ではない)
discard_form_is_plain_revert() {
  local -a words=()
  local count start i
  read -r -a words <<<"$(command_segments | head -1)"
  count=${#words[@]}
  [ "${words[0]:-}" = "git" ] || return 1

  case "${words[1]:-}" in
    checkout)
      if [ "${words[2]:-}" = "--" ]; then
        start=3
      elif [ "$count" -eq 3 ] && [ "${words[2]:-}" = "." ]; then
        start=2
      else
        return 1
      fi
      ;;
    restore)
      start=2
      [ "${words[2]:-}" = "--" ] && start=3
      ;;
    *) return 1 ;;
  esac

  [ "$count" -gt "$start" ] || return 1
  for ((i = start; i < count; i++)); do
    checkout_name_is_plain "${words[$i]}" || return 1
  done
  return 0
}

# リビジョンを伴う取り消しか。通す形は 3 つに固定する:
#
#   git checkout <rev> -- <path>...
#   git restore --source=<rev> [--] <path>...
#   git restore -s <rev> [--] <path>...
#
# 見つけたリビジョンとパスを REVISION_REV / REVISION_PATHS へ置く (次の関数が読む)。
#
# **`--` を必須にする。** 無いと `git checkout <rev> <path>` はブランチ切り替えとも
# 読め、`checkout_target_is_branch` のコメントが言う取り違えがそのまま起きる。
#
# setup#142 がこの形を外したのは「別の版を持ち込むから」だったが、持ち込む版が
# コミットである限り再導出できる (CLAUDE.md の「取り戻せる」に当たる)。危ないのは
# autoMode.hard_deny が名指しする**設定ファイルの巻き戻し**だけなので、形ではなく
# 対象パスで切る — それが次の関数である (setup#148)。
REVISION_REV=''
REVISION_PATHS=''

discard_form_is_revision_revert() {
  local -a words=()
  local count start i rev=''
  read -r -a words <<<"$(command_segments | head -1)"
  count=${#words[@]}
  [ "${words[0]:-}" = "git" ] || return 1

  case "${words[1]:-}" in
    checkout)
      [ "${words[3]:-}" = "--" ] || return 1
      rev="${words[2]:-}"
      start=4
      ;;
    restore)
      case "${words[2]:-}" in
        --source=*) rev="${words[2]#--source=}"; start=3 ;;
        -s|--source) rev="${words[3]:-}"; start=4 ;;
        *) return 1 ;;
      esac
      [ "${words[$start]:-}" = "--" ] && start=$((start + 1))
      ;;
    *) return 1 ;;
  esac

  checkout_name_is_plain "$rev" || return 1
  git rev-parse --verify --quiet "$rev^{commit}" >/dev/null 2>&1 || return 1
  [ "$count" -gt "$start" ] || return 1

  REVISION_PATHS=''
  for ((i = start; i < count; i++)); do
    checkout_name_is_plain "${words[$i]}" || return 1
    REVISION_PATHS="$REVISION_PATHS${words[$i]}
"
  done
  REVISION_REV="$rev"
  return 0
}

# 巻き戻しの対象に、エージェントの権限設定が含まれるか。**退避があっても通さない
# 唯一の形**で、autoMode.hard_deny の "Auto-Mode Self-Authorization" が
# `git checkout <rev> -- <settings file>` をそのまま名指ししている。automode-guard.py は
# Edit|Write matcher なので Bash 経由のこれを見ておらず、止めているのは分類器だけ。
#
# **ディレクトリや `.` を渡されても取りこぼさない** — パスを git に展開させ、
# 「いま消えるもの」(index の側) と「持ち込むもの」(rev の側) の両方を見る。
#
# 綴りは 2 通り: リポジトリ内の `.claude/settings*.json` と、setup リポが実体を持つ
# `claude/settings*.json`。~/.claude/ 配下は git 管理外なのでここには現れない。
readonly AGENT_SETTINGS_PATTERN='(^|/)\.?claude/settings[^/]*\.json$'

revision_revert_touches_agent_settings() { # REVISION_REV / REVISION_PATHS を読む
  local listed
  # shellcheck disable=SC2086  # パスは checkout_name_is_plain を通っており引用符を含まない
  listed=$(
    {
      git ls-files -- $REVISION_PATHS 2>/dev/null
      git ls-tree -r --name-only "$REVISION_REV" -- $REVISION_PATHS 2>/dev/null
    } | grep -E "$AGENT_SETTINGS_PATTERN"
  )
  [ -n "$listed" ]
}

# コマンド全体が、この形 1 つだけでできているか。allow はコマンド文字列**全体**に
# 効くので、退避を作れていても後続が読めなければ allow は返せない (素通しへ落とす)。
command_is_only() { # $1=先頭に一致すべき正規表現
  local trimmed
  trimmed=$(trim "$command")
  is_compound "$trimmed" && return 1
  printf '%s' "$trimmed" | grep -qE "$1"
}

# `-D` の対象がすべて、そのまま名前として扱える形か (固定する先を確定できるか)。
branch_delete_targets_are_plain() {
  local targets target
  targets=$(branch_delete_targets)
  [ -n "$targets" ] || return 1
  for target in $targets; do
    case "$target" in
      -*|*'$'*|*'"'*|*"'"*|*'`'*) return 1 ;;
    esac
  done
  return 0
}

# 取り消しの後ろに続くものが、状態を変えないと確認できる形だけか。allow はコマンド
# 文字列**全体**に効くので、後続も読めたときにしか返せない。ここに載らない形は素通しへ
# 落とすだけで、判定が permissions と分類器へ戻る (危険側には倒れない)。
readonly READ_ONLY_SEGMENT='^(echo([[:space:]]|$)|true$|pwd$|git[[:space:]]+(status|diff|log)([[:space:]]|$))'

tail_is_read_only() {
  local segment
  # 語に分けて読めない形 (リダイレクト・コマンド置換・サブシェル) はここで落とす
  printf '%s' "$command" | grep -q '[<>()$`]' && return 1

  while IFS= read -r segment; do
    segment=$(trim "$segment")
    [ -n "$segment" ] || continue
    printf '%s' "$segment" | grep -qE "$READ_ONLY_SEGMENT" || return 1
  done < <(command_segments | tail -n +2)
  return 0
}

# --- 1. 捨てる操作のうち、退避を作れなかったもの ---------------------------
# **退避は形が対象外でも作る。** ask を返した先で承認されれば捨てられるものは同じで、
# 取り戻せる場所に置いておく意味は変わらない。作れたものは danger を立てずに下へ渡し、
# コマンド全体が読めれば allow、読めなければ素通し (分類器へ戻す) になる。
danger=""

# git reset --hard — 未コミットの変更と、いまの HEAD の両方を固定してから通す
reset_refs=""
if has "${GIT}reset${ARG}--hard" && ! reset_hard_discards_nothing; then
  if ! reset_refs=$(backup_reset_hard); then
    danger="git reset --hard は、まだコミットしていない変更と HEAD を復元できない形で捨てる (捨てる前の退避を作れなかった — git リポジトリの外か、コミットがまだ 1 つも無い)"
  fi
fi

# git clean -f — **退避を作れない唯一の口**。追跡外のファイルは object DB へ入れられず、
# -x を付けた形では .build のような無視対象まで対象に入って費用が非有界になる (setup#148)
has "${GIT}clean${ARG}(--force|-[a-zA-Z]*f)" &&
  danger="git clean -f は追跡していないファイルを削除する (ゴミ箱には入らない。追跡外のファイルは退避できないので、ここは確認が要る)"

# 作業ツリーの取り消し — 退避を作れた形だけ danger を立てず、下のセクション 3 へ渡す
backup_ref=""
backed_up=false
if has "${GIT}checkout${ARG}(--([[:space:]]|$)|\.([[:space:]]|$))" ||
  { has "${GIT}restore([[:space:]]|$)" && ! has '\-\-staged'; }; then
  if ! backup_ref=$(backup_worktree); then
    danger="git checkout / git restore での作業ツリーの取り消しは、その変更を復元できない (捨てる前の退避を作れなかった — git リポジトリの外か、コミットがまだ 1 つも無い)"
  elif discard_form_is_plain_revert; then
    backed_up=true
  elif discard_form_is_revision_revert; then
    if revision_revert_touches_agent_settings; then
      danger="エージェントの権限設定 (settings.json) を過去の版へ戻そうとしている。autoMode.hard_deny の \"Auto-Mode Self-Authorization\" が名指しする自己権限拡大の経路で、**退避があっても通さない唯一の形**である${backup_ref:+。いまの作業ツリーは $backup_ref に退避してある}"
    else
      backed_up=true
    fi
  fi
  # ここに載らない形 (強制上書き・部分破棄・展開しないと確定しない対象) は danger を
  # 立てない。退避は作れているので ask を返しても押す人へ足せる情報が無く、判定は
  # permissions と分類器へ戻る (setup#148)
fi

# git branch -D — 「役目を終えた」と確認できなくても、先端を固定できれば失うものは無い
branch_refs=""
# 判定はエイリアス展開後の文字列に対して行う。`git gone-clean` のように、削除が
# 定義の中にしか現れない形を取りこぼさないため ($command には現れない)
if has "${GIT}branch([[:space:]]|$)" && deletes_branch_by_force "$scan" &&
  ! branch_delete_targets_are_gone && ! alias_deletes_only_gone_branches; then
  if branch_delete_targets_are_plain; then
    # shellcheck disable=SC2046  # 名前は branch_delete_targets_are_plain を通っている
    branch_refs=$(backup_branch_tips $(branch_delete_targets)) || branch_refs=""
  fi
  [ -n "$branch_refs" ] ||
    danger="git branch -D はマージ済みかを問わずブランチを消す (消す前に先端を固定できなかった — 名前を確定できないか、そのブランチが実在しない)"
fi

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

# --- 2. 可逆と確認できたブランチ操作 ---------------------------------------
# ここに来る時点で 1 の検査は通っている (危険と読めた -D は上で ask 済み)。
#
# allow はコマンド文字列**全体**に効くので、対象は「ブランチ削除しかしていない」と
# 読める形に限る。区切り・リダイレクト・変数展開・コマンド置換が混ざるものは素通しへ
# 落とす — 判定が permissions へ戻るだけで、危険側には倒れない。
is_reversible_branch_cleanup() {
  local trimmed
  trimmed=$(trim "$command")
  is_compound "$trimmed" && return 1

  # -d は git 自身がマージ済みかを確かめ、未マージなら断る (失うものが無い)。
  # ただし --force が付けば -D と等価になり git は確かめなくなるので、ここへは入れない
  # (issue #162 — 「-d だから安全」は force の有無を見て初めて成り立つ)
  printf '%s' "$trimmed" | grep -qE '^git[[:space:]]+branch([[:space:]]|$)' &&
    ! deletes_branch_by_force "$trimmed" &&
    printf '%s' "$trimmed" | grep -qE '^git[[:space:]]+branch[[:space:]]+(-d|--delete)([[:space:]]|$)' &&
    return 0
  # force を伴う削除は、対象すべてが「消しても失うものが無い」と確認できたときだけ
  # (追跡先が畳まれている / push 前で内容が既定ブランチに入っている)
  printf '%s' "$trimmed" | grep -qE '^git[[:space:]]+branch([[:space:]]|$)' &&
    deletes_branch_by_force "$trimmed" &&
    branch_delete_targets_are_gone && return 0
  # `git gone-clean` のような、掃除エイリアス 1 本きりの呼び出し
  printf '%s' "$trimmed" | grep -qE '^git[[:space:]]+[a-zA-Z][-a-zA-Z0-9_]*$' &&
    alias_deletes_only_gone_branches && return 0

  return 1
}

# コマンド全体が「ブランチの切り替え」1 つだけか (状態は見ない。呼び出し側で
# worktree_has_nothing_to_lose と組にする)。
#
# 通す形は 3 つに固定し、ここに無いものはすべて素通しへ落とす。狭く固定するのは、
# `checkout` が 1 語で別物を指し、取りこぼしがそのまま「破棄の許可」になるため:
#
#   - `git checkout <rev> -- <path>` は autoMode.hard_deny の
#     "Auto-Mode Self-Authorization" が名指しする自己権限拡大の経路。automode-guard.py は
#     Edit|Write matcher なので Bash 経由のそれを見ておらず、止めているのは分類器だけ。
#     ここで allow を返すとその層ごと飛び越える
#   - `-f` / `--force` は未コミットの変更を踏み潰し、`-B` は既存ブランチの tip を捨て、
#     `-p` / `--ours` / `--theirs` は部分的に捨てる
#   - 引数がブランチでなければ `--` が無くても作業ツリーの破棄になる
#     (checkout_target_is_branch のコメント)
checkout_form_is_switch_only() {
  local trimmed count first
  trimmed=$(trim "$command")
  printf '%s' "$trimmed" | grep -qE '^git[[:space:]]+checkout([[:space:]]|$)' || return 1
  is_compound "$trimmed" && return 1

  # ヒアストリングの語分割はグロブを展開しない (`set -- $trimmed` と違い、
  # コマンドに残った `*` が手元のファイル名へ化けない)
  local -a words=()
  read -r -a words <<<"$trimmed"
  count=${#words[@]}
  first=${words[2]:-}

  # git checkout -            直前に居たブランチへ戻る
  if [ "$count" -eq 3 ] && [ "$first" = "-" ]; then
    git rev-parse --verify --quiet '@{-1}' >/dev/null 2>&1
    return
  fi

  # git checkout <branch>     実在するブランチへの切り替えだけ
  if [ "$count" -eq 3 ]; then
    checkout_name_is_plain "$first" || return 1
    checkout_target_is_branch "$first"
    return
  fi

  # git checkout -b <name> [<start>]   作れば済むので、失うものは無い
  if [ "$first" = "-b" ] && { [ "$count" -eq 4 ] || [ "$count" -eq 5 ]; }; then
    checkout_name_is_plain "${words[3]:-}" || return 1
    [ "$count" -eq 4 ] && return 0
    checkout_name_is_plain "${words[4]:-}" || return 1
    git rev-parse --verify --quiet "${words[4]}^{commit}" >/dev/null 2>&1
    return
  fi

  return 1
}

if is_reversible_branch_cleanup; then
  decide allow "消えて困るものが無いと確認できたブランチ削除 (-d は git がマージ済みかを確かめて未マージなら断る / -D と掃除エイリアスの対象は追跡先が [gone] か、push 前で内容が既定ブランチに入っているブランチだけで、コミットは remote から取り戻せる)。確認は不要。"
fi

# 先端を固定したブランチ削除。上の is_reversible_branch_cleanup を先に置いてあるので、
# 「役目を終えた」と確認できた対象はここへ来ない (固定も作らない)。
if [ -n "$branch_refs" ] &&
  command_is_only '^git[[:space:]]+branch([[:space:]]|$)' && deletes_branch_by_force; then
  decide allow "消えるブランチの先端を退避済み:
$(printf '%s' "$branch_refs" | sed 's/^/  - /')

復元は git branch <名前> <ref>、中身を見るだけなら git log <ref>。一覧は git discarded。取り戻せるので確認は不要。"
fi

# 退避を作った git reset --hard
if [ -n "$reset_refs" ] && command_is_only '^git[[:space:]]+reset([[:space:]]|$)'; then
  decide allow "捨てられるものを退避済み:
$(printf '%s' "$reset_refs" | sed -e '/^$/d' -e 's/^/  - /')

作業ツリーの復元は git checkout <ref> -- <path>、HEAD の復元は git reset --hard <ref>。一覧は git discarded。取り戻せるので確認は不要。"
fi

if checkout_form_is_switch_only && worktree_has_nothing_to_lose; then
  decide allow "失うものが無いと確認できたブランチ切り替え (いまブランチの上に居るので離れても参照から外れるコミットが無く、追跡ファイルに未コミットの変更も無い。対象は実在するブランチか -b で作る新しいブランチで、パスの破棄・-f・-B・-- <path> はこの判定に載らない)。確認は不要。"
fi

# --- 3. 退避を作って可逆にした作業ツリーの取り消し ---------------------------
# ここに来るのは、上で退避を作れた形だけ (backed_up)。捨てられる内容は取り戻せる場所に
# 在るので、確認を返す理由が無い。後ろに何か続く場合は、それも読み取り専用だと
# 確認できたときにだけ allow を返す。
if [ "$backed_up" = true ] && tail_is_read_only; then
  if [ -n "$backup_ref" ]; then
    decide allow "捨てられる変更は $backup_ref に退避済み (git stash create のコミットを ref に固定してある)。復元は git checkout $backup_ref -- <path>、中身を見るだけなら git show $backup_ref:<path>。一覧は git discarded。取り戻せるので確認は不要。"
  fi
  decide allow "この取り消しで捨てられる変更が無い (追跡ファイルに未コミットの変更が無く、作業ツリーは何も変わらない)。確認は不要。"
fi

# --- 4. 秘密情報らしきファイルのコミット -----------------------------------
# .env.example のような雛形は対象外。gitignore が効いていれば下の検査には現れないので、
# ここに出てくる時点で「入れてはいけないものが漏れている」状態。
readonly SECRET_PATTERN='(^|/)\.env($|\.[^/]*$)|(^|/)id_(rsa|dsa|ecdsa|ed25519)$|\.(pem|p12|pfx|jks|keystore)$|(^|/)[^/]*_rsa$'
readonly SECRET_ALLOW='\.(example|sample|template|dist|pub)$|(^|/)\.env\.(example|sample|template)$'

# commit と add は**両方**見る。排他にすると `git add .env && git commit -m x` で
# commit 側だけが選ばれ、PreToolUse の時点ではまだ add が走っていないので
# git diff --cached が空になって素通しする (issue #162)。モデルが最も普通に書く形なので、
# 排他のままでは実質この deny が無いのと変わらない。
secrets=""
if has "${GIT}commit([[:space:]]|$)"; then
  secrets=$(git diff --cached --name-only 2>/dev/null |
    grep -E "$SECRET_PATTERN" | grep -vE "$SECRET_ALLOW")
fi
if has "${GIT}add([[:space:]]|$)"; then
  # add はステージ前なので、コマンドに書かれたパスをそのまま見る。連結形でも拾えるよう、
  # add のセグメントだけを取り出してから語に割る (他のコマンドの引数を巻き込まない)
  staged_by_name=$(command_segments |
    grep -E "^[[:space:]]*git([[:space:]]+[^[:space:]]+)*[[:space:]]+add([[:space:]]|$)" |
    tr ' ' '\n' | grep -E "$SECRET_PATTERN" | grep -vE "$SECRET_ALLOW")
  secrets=$(printf '%s\n%s\n' "$secrets" "$staged_by_name" | grep -v '^$' | sort -u)
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
