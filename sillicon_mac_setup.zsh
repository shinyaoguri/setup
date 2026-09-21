#!/usr/bin/env zsh

set -e # errorで即座に終了させる

# ----- 変数初期化 -----
localmode=false
GITHUB_REPO_URL="https://github.com/shinyaoguri/setup.git"
SETUP_DIR="$HOME/.setup"
# optional (すぐに要らないもの) をどう扱うか: ask / all / none。
# 既定は ask だが、端末が無ければ選択を出せないので後で none へ倒す (issue #191)
OPTIONAL_MODE=ask

usage() {
	echo "Usage: $0 [-l] [-h] [--with-optional] [--no-optional]"
	echo "  -l              : ローカルモード (リポジトリをクローン済みの場合)"
	echo "  --with-optional : 選択を出さず、optional をすべて入れる"
	echo "  --no-optional   : 選択を出さず、optional を入れない"
	echo "  -h              : ヘルプを表示"
}

# ----- オプション解析 -----
# 長い形は getopts が扱えないので先に抜き、残りを getopts へ渡す
args=()
for a in "$@"; do
	case "$a" in
		--with-optional) OPTIONAL_MODE=all ;;
		--no-optional)   OPTIONAL_MODE=none ;;
		*) args+=("$a") ;;
	esac
done
set -- "${args[@]}"

while getopts "lh" opt; do
	case "$opt" in
		l) localmode=true;;
		h) usage; exit 0 ;;
		*) usage; exit 1 ;;
	esac
done

# 端末が無い (CI・無人実行・パイプ経由) なら選択を出せない。止まらずに進む
if [[ "$OPTIONAL_MODE" == ask ]] && [[ ! -t 0 ]]; then
	OPTIONAL_MODE=none
	NO_TTY=true
fi

echo "============================================================"
echo "  Apple Silicon Mac セットアップ"
echo "============================================================"
echo ""

########
# Step 1: Xcode Command Line Tools
########
echo "📦 Step 1: Xcode Command Line Tools のチェック"
if xcode-select -p >/dev/null 2>&1; then
	echo "   ✓ Xcode Command Line Tools はインストール済みです"
else
	echo "   ⚠️  Xcode Command Line Tools がインストールされていません"
	echo ""
	echo "   インストールを開始します..."
	xcode-select --install
	echo ""
	echo "   ⏸️  インストールが完了したら、このスクリプトを再実行してください"
	exit 0
fi

# **パスが通っていても git が動くとは限らない。** 新品の Mac は Xcode を一度も開いて
# いないので、Xcode.app が選ばれているとライセンス未同意で git / clang がまとめて
# 終了コード 69 で落ちる (`sudo xcode-select -s` で切り替えた直後も同じ)。
# `xcode-select -p` はパスを返すだけなのでここを素通りし、直後の Step 1.5 で git が
# 失敗して「別のリポジトリです」という無関係な診断になっていた (issue #266)。
# 以降はすべて git が前提なので、動くことをここで実測する。
if ! git_error=$(git --version 2>&1 >/dev/null); then
	if [[ "$git_error" == *license* ]]; then
		echo "   ⚠️  Xcode のライセンスに同意していません"
		if [[ -t 0 ]]; then
			# 同意そのものは本人がする。読ませずに accept で通さない
			echo "   同意画面を開きます (スペースで読み進め、最後に agree と入力)..."
			sudo xcodebuild -license
			if ! git_error=$(git --version 2>&1 >/dev/null); then
				echo "   ❌ 同意した後も git を実行できません"
				echo "      $git_error"
				exit 1
			fi
			echo "   ✓ ライセンスに同意しました"
		else
			# 端末が無ければ同意画面を出せない (CI・パイプ経由)
			echo "   ❌ 次を実行して同意してから、このスクリプトを再実行してください"
			echo "      sudo xcodebuild -license accept"
			exit 1
		fi
	else
		echo "   ❌ git を実行できません"
		echo "      $git_error"
		exit 1
	fi
fi
echo ""

##########
# Step 1.5: Playbook パス設定 (cloud モードはリポジトリをクローン)
##########
# 手が付けられない状態から抜ける道。**消させない** — 何が入っているか分からないものを
# rm させる案内は、直せたはずの状態まで消す
setup_rescue_hint() {
	echo "      → 中身に心当たりが無ければ、退避してからやり直してください:"
	echo "        mv \"$SETUP_DIR\" \"$SETUP_DIR.broken.\$(date +%Y%m%d%H%M%S)\""
}
if [[ "$localmode" == true ]]; then
	SCRIPT_DIR="${0:A:h}"
	PLAYBOOK="$SCRIPT_DIR/playbook_sillicon_mac.yml"
else
	echo "📥 Step 1.5: setup リポジトリの準備 ($SETUP_DIR)"
	if [[ -d "$SETUP_DIR/.git" ]]; then
		# **git の失敗理由を捨てない。** 以前は `2>/dev/null || echo ""` で空文字へ
		# 畳んでから URL を照合していたため、ライセンス未同意・所有権の食い違い・
		# クローンの残骸まで「別のリポジトリです: (空)」になっていた。名指しされた
		# 原因が実際の原因と違うと、人はそこから先へ進めない (issue #266)。
		if ! git_error=$(git -C "$SETUP_DIR" rev-parse --git-dir 2>&1 >/dev/null); then
			echo "   ❌ $SETUP_DIR を Git リポジトリとして読めません"
			echo "      $git_error"
			setup_rescue_hint
			exit 1
		fi
		REMOTE_URL=$(git -C "$SETUP_DIR" remote get-url origin 2>/dev/null || true)
		if [[ -z "$REMOTE_URL" ]]; then
			echo "   ❌ $SETUP_DIR に origin がありません (クローンが途中で終わった可能性)"
			setup_rescue_hint
			exit 1
		elif [[ "$REMOTE_URL" != *"shinyaoguri/setup"* ]]; then
			echo "   ❌ $SETUP_DIR は別のリポジトリです: $REMOTE_URL"
			exit 1
		fi
		echo "   ✓ 既存リポジトリを更新します..."
		git -C "$SETUP_DIR" pull --ff-only || echo "   ⚠️  pull に失敗しました (ローカル変更がある可能性)"
	elif [[ -e "$SETUP_DIR" ]]; then
		echo "   ❌ $SETUP_DIR が既に存在します (Git リポジトリではない)"
		setup_rescue_hint
		exit 1
	else
		echo "   📦 リポジトリをクローンします..."
		# **配備先へ直接クローンしない。** 中断 (Ctrl-C・ネットワーク断) で半端な
		# .git が残ると、次の実行は `-d "$SETUP_DIR/.git"` が真になってクローンの
		# やり直しへ戻れない — 1 行で新しいマシンを構築するのが目的なので、最初の
		# 一歩の失敗が手作業を要求してはいけない (issue #269)。
		# 別の置き場へ作ってから移し、後始末は trap に寄せる (set -e があるので、
		# 失敗したときは後ろに並べた rm へ届かない。issue #195 と同じ形)。
		clone_tmp="$SETUP_DIR.partial.$$"
		rm -rf "$clone_tmp"
		trap 'rm -rf "$clone_tmp"' EXIT INT TERM
		git clone "$GITHUB_REPO_URL" "$clone_tmp"
		mv "$clone_tmp" "$SETUP_DIR"
		trap - EXIT INT TERM
	fi
	PLAYBOOK="$SETUP_DIR/playbook_sillicon_mac.yml"
	echo ""
fi

###########
# Step 2: Homebrew
###########
echo "🍺 Step 2: Homebrew のチェック"
if command -v brew >/dev/null 2>&1; then
	echo "   ✓ Homebrew はインストール済みです"
	BREW_VERSION=$(brew --version | head -1)
	echo "   ℹ️  $BREW_VERSION"
else
	echo "   ⚠️  Homebrew がインストールされていません"
	echo ""
	echo "   インストールを開始します..."
	brew_installer=$(mktemp)
	if ! curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$brew_installer"; then
		rm -f "$brew_installer"
		echo "   ❌ Homebrew のインストーラを取得できませんでした (ネットワークを確認してください)"
		exit 1
	fi
	/bin/bash "$brew_installer"
	rm -f "$brew_installer"

	# **インストーラは実行中シェルの PATH を変えない。** 末尾に
	# 「Run this command in your terminal to add Homebrew to your PATH」と表示するだけで、
	# /opt/homebrew/bin を通しているのは zshrc (対話シェル専用) しかない。このまま進むと
	# 直後の Step 3 の `brew install ansible` が command not found になり set -e で死ぬ
	# — Homebrew は入った後なので原因が分かりにくい形で落ちる (issue #166)。
	eval "$(/opt/homebrew/bin/brew shellenv)"
fi
echo ""

##########
# Step 3: Ansible
##########
echo "⚙️  Step 3: Ansible のチェック"
if command -v ansible >/dev/null 2>&1; then
	echo "   ✓ Ansible はインストール済みです"
	ANSIBLE_VERSION=$(ansible --version | head -1)
	echo "   ℹ️  $ANSIBLE_VERSION"
else
	echo "   ⚠️  Ansible がインストールされていません"
	echo ""
	echo "   Homebrew 経由でインストールします..."
	brew install ansible
	echo "   ✓ Ansible のインストールが完了しました"
fi

# 選択 UI の道具。vars/packages.yml の homebrew_packages_required に宣言してあるが、
# それを適用する playbook は Step 6 なので、選択を使う Step 5.5 より前にここで用意する
# (mas と同じ形)。fzf が一覧と preview、gum が報告と進捗を描く (issue #191)
for tool in fzf gum; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		echo "   📦 $tool をインストールします (選択 UI に使う。vars/packages.yml に宣言済み)..."
		brew install "$tool"
	fi
done
echo ""

##########
# Step 4: Ansible Collection
##########
echo "📚 Step 4: Ansible Collection のチェック"
if ansible-galaxy collection list 2>/dev/null | grep -q "community.general"; then
	echo "   ✓ community.general はインストール済みです"
	COLLECTION_VERSION=$(ansible-galaxy collection list 2>/dev/null | grep "community.general" | head -1 | awk '{print $2}')
	echo "   ℹ️  Version: $COLLECTION_VERSION"
else
	echo "   ⚠️  community.general がインストールされていません"
	echo ""
	echo "   インストールを開始します..."
	ansible-galaxy collection install community.general 2>&1 | grep -v "^Skipping" || true
	echo "   ✓ Collection のインストールが完了しました"
fi
echo ""

##########
# Step 5: sudo セッションを事前確保 (Cask によっては sudo を要求するため)
##########
echo "🔐 Step 5: sudo パスワードを事前認証 (Cask インストール用)"
echo "   一部の Cask は管理者権限を必要とします。"
sudo -v
# Ansible 実行中も sudo タイムアウトが切れないようバックグラウンドで延長
( while true; do sudo -n true 2>/dev/null || exit; sleep 50; done ) &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT INT TERM
echo "   ✓ sudo セッションを維持中 (PID: $SUDO_KEEPALIVE_PID)"
echo ""

##########
# Step 5.5 / 5.6: アプリの導入
#   Ansible 配下の subprocess は controlling TTY を持たないため、cask 内部の
#   `sudo /usr/sbin/installer ...` が credential cache を引けず失敗する
#   (tty_tickets による隔離)。bootstrap シェルの interactive TTY で先に
#   走らせることで sudo が通る前提を満たす。mas も .pkg 系で同じ事情。
#
#   **required は無条件に入れ、optional は報告したうえで選ばせる** (issue #191)。
#   宣言されている cask の 17/19 は auto_updates: true で自分で更新するため、brew の
#   役割は初回の導入だけ。要らないものまで毎回入れる理由が無い。
##########
PACKAGES_YAML="${SETUP_DIR}/vars/packages.yml"
if [[ "$localmode" == true ]]; then
	PACKAGES_YAML="${SCRIPT_DIR}/vars/packages.yml"
fi
if [[ ! -f "$PACKAGES_YAML" ]]; then
	echo "   ❌ $PACKAGES_YAML が見つかりません"
	exit 1
fi

# vars/packages.yml の <キー> から `- 値` を抜き出す (依存追加なしで awk 解析)
yaml_list() {
	awk -v key="$1" '
		$0 ~ "^" key ":" { flag=1; next }
		/^[^ #]/         { flag=0 }
		flag && /^[[:space:]]*-[[:space:]]/ {
			sub(/^[[:space:]]*-[[:space:]]*/,"")
			print
		}
	' "$PACKAGES_YAML"
}

# appstore_apps 系は id<TAB>name で抜く
yaml_appstore() {
	awk -v key="$1" '
		$0 ~ "^" key ":" { flag=1; next }
		/^[^ #]/         { flag=0 }
		flag && /[[:space:]]-[[:space:]]/ {
			name=""; id=""
			if (match($0, /name:[[:space:]]*"[^"]*"/)) {
				name = substr($0, RSTART, RLENGTH)
				sub(/^name:[[:space:]]*"/, "", name); sub(/"$/, "", name)
			}
			if (match($0, /id:[[:space:]]*"[^"]*"/)) {
				id = substr($0, RSTART, RLENGTH)
				sub(/^id:[[:space:]]*"/, "", id); sub(/"$/, "", id)
			}
			print id "\t" name
		}
	' "$PACKAGES_YAML"
}

# 見出し。gum があれば飾る (無い環境でも読める形に落ちる)
banner() {
	if command -v gum >/dev/null 2>&1; then
		gum style --border rounded --padding "0 1" --border-foreground 212 "$1"
	else
		echo "== $1 =="
	fi
}

# 導入済みの印。報告を人が読む形にするためだけのもの
mark() { [[ "$1" == yes ]] && echo "✓" || echo "·"; }

echo "🍺 Step 5.5: Homebrew Cask アプリ"
banner "インストール状況"

CASKS_REQUIRED=( $(yaml_list homebrew_cask_packages_required) )
CASKS_OPTIONAL=( $(yaml_list homebrew_cask_packages_optional) )

# --- 報告 -----------------------------------------------------------------
echo "   [必須]"
for c in "${CASKS_REQUIRED[@]}"; do
	if brew list --cask "$c" >/dev/null 2>&1; then echo "     ✓ $c"; else echo "     · $c (未導入)"; fi
done
echo "   [任意] — 要るものだけ選べます"
CASK_MISSING=()
for c in "${CASKS_OPTIONAL[@]}"; do
	if brew list --cask "$c" >/dev/null 2>&1; then
		echo "     ✓ $c"
	else
		echo "     · $c (未導入)"
		CASK_MISSING+=("$c")
	fi
done
echo ""

# --- cask の導入 ----------------------------------------------------------
# **1 本の失敗で setup 全体を止めない。** このスクリプトは set -e なので、素の
# `brew install --cask` を並べると、失敗した 1 本の後ろ (残りの cask・App Store・
# playbook・秘密のキャッシュ) が丸ごと実行されない。失敗は名前を控えて続け、最後に
# まとめて報告する。必須が欠けたときだけ、最後まで進めたうえで非ゼロで終える。
#
# **--adopt は、手で入れてあるアプリを brew の管理下へ取り込む。** 公式サイトから
# 入れたアプリは `brew list --cask` が偽を返す (brew は知らない) のに、素の install は
# "It seems there is already an App at …" で失敗する。required の 4 本はどれも手で
# 入れがちで、新品でないマシンへ流すとここで止まっていた (issue #199)。
# 取り込めるのは中身が同じときだけで、版が違えば失敗する — その場合も上の扱いで続く。
CASK_FAILED_REQUIRED=()
CASK_FAILED_OPTIONAL=()
install_cask() {  # $1=名前 $2=required|optional
	if brew install --cask --adopt "$1"; then
		return 0
	fi
	echo "   ⚠️  $1 を入れられませんでした。続行します (最後にまとめて報告します)"
	if [[ "$2" == required ]]; then
		CASK_FAILED_REQUIRED+=("$1")
	else
		CASK_FAILED_OPTIONAL+=("$1")
	fi
	return 0
}
# --- cask の導入ここまで ---------------------------------------------------

# --- 必須を入れる ---------------------------------------------------------
for c in "${CASKS_REQUIRED[@]}"; do
	if ! brew list --cask "$c" >/dev/null 2>&1; then
		echo "   → $c をインストール中 (必須)..."
		install_cask "$c" required
	fi
done

# --- 任意を選ばせる -------------------------------------------------------
# 選択 UI は**ここ 1 か所**から出す。見た目と決定の作法を 2 か所へ写すと、片方だけ
# 直した瞬間に挙動が分かれる (App Store の選択がそうなっていた)。
#
# **fzf は --multi でも、マークが 0 件のまま Enter を押すとカーソル位置の 1 件を返す。**
# チェックボックスのつもりで押しただけで、選んでいないアプリが入っていた (issue #267)。
# 選択数を見て 0 件なら abort (= Esc と同じ「何も入れずに進む」) へ倒す。
# FZF_SELECT_COUNT は fzf 0.52 以降。持たない版では空 → `:-1` で accept へ倒れ、
# 従来どおり動く (キーが無反応になる側へは倒さない)。
fzf_select() {  # $1=見出し, $2=preview コマンド, $3=preview-window。候補は標準入力から
	fzf --multi \
		--height=80% --border=rounded --layout=reverse \
		--marker='◉ ' --pointer='▸' \
		--header=$''"$1"$'\n Tab で選択 / Enter で決定 (選んでいなければ何も入れません) / Esc で何も入れずに進む' \
		--bind 'enter:transform:[ "${FZF_SELECT_COUNT:-1}" -eq 0 ] && echo abort || echo accept' \
		--preview "$2" \
		--preview-window="${3:-right:55%:wrap}" || true
}

# 選ばれたものを install_selected へ入れる。fzf は行の 1 語目を名前として渡し、
# preview に brew info を出す (版・説明・ホームページ・auto_updates が読める)
select_optional() {  # $1=見出し, 残り=候補
	local title="$1"; shift
	local -a candidates=("$@")
	(( ${#candidates[@]} == 0 )) && return 0

	case "$OPTIONAL_MODE" in
		all)  printf '%s\n' "${candidates[@]}"; return 0 ;;
		none) return 0 ;;
	esac

	printf '%s\n' "${candidates[@]}" | fzf_select "$title" \
		'brew info --cask {1} 2>/dev/null || brew info {1} 2>/dev/null' right:55%:wrap
}

if (( ${#CASK_MISSING[@]} > 0 )); then
	if [[ "$OPTIONAL_MODE" == none ]]; then
		if [[ -n "${NO_TTY:-}" ]]; then
			echo "   ℹ️  端末が無いので選択は出しません。要るときは次で入れられます:"
		else
			echo "   ℹ️  任意のアプリは入れません。要るときは次で入れられます:"
		fi
		echo "      brew install --cask ${CASK_MISSING[*]}"
	else
		SELECTED=( ${(f)"$(select_optional '任意の GUI アプリ (未導入のみ)' "${CASK_MISSING[@]}")"} )
		if (( ${#SELECTED[@]} == 0 )); then
			echo "   ℹ️  任意のアプリは選ばれませんでした"
		else
			for c in "${SELECTED[@]}"; do
				[[ -z "$c" ]] && continue
				echo "   → $c をインストール中..."
				install_cask "$c" optional
			done
		fi
	fi
fi
echo ""

##########
# Step 5.5b: コマンドラインツール (optional の formula)
#   required の formula は playbook (tasks/homebrew.yml) が入れる。optional はここで選ばせる。
#   #191 で「optional は選ばせる」形にしたとき formula の選択が抜け、
#   homebrew_packages_optional はどこからも読まれていなかった (issue #240)。
#   **playbook より前に置く** — fnm が入っていないと、その回の tasks/fnm.yml は
#   「fnm が無ければ skip」へ倒れ、Node.js が黙って入らない。
##########
echo "🧰 Step 5.5b: コマンドラインツール"
banner "インストール状況"

FORMULAE_OPTIONAL=( $(yaml_list homebrew_packages_optional) )
echo "   [任意] — 要るものだけ選べます"
FORMULA_MISSING=()
for f in "${FORMULAE_OPTIONAL[@]}"; do
	if brew list --formula "$f" >/dev/null 2>&1; then
		echo "     ✓ $f"
	else
		echo "     · $f (未導入)"
		FORMULA_MISSING+=("$f")
	fi
done
echo ""

# --- formula の導入 -------------------------------------------------------
# cask と同じ扱い。1 本の失敗で setup 全体を止めず、名前を控えて続ける (issue #199)
FORMULA_FAILED=()
install_formula() {  # $1=名前
	if brew install "$1"; then
		return 0
	fi
	echo "   ⚠️  $1 を入れられませんでした。続行します (最後にまとめて報告します)"
	FORMULA_FAILED+=("$1")
	return 0
}
# --- formula の導入ここまで ------------------------------------------------

if (( ${#FORMULA_MISSING[@]} > 0 )); then
	if [[ "$OPTIONAL_MODE" == none ]]; then
		echo "   ℹ️  任意のツールは入れません。要るときは次で入れられます:"
		echo "      brew install ${FORMULA_MISSING[*]}"
	else
		SELECTED=( ${(f)"$(select_optional '任意のコマンドラインツール (未導入のみ)' "${FORMULA_MISSING[@]}")"} )
		if (( ${#SELECTED[@]} == 0 )); then
			echo "   ℹ️  任意のツールは選ばれませんでした"
		else
			for f in "${SELECTED[@]}"; do
				[[ -z "$f" ]] && continue
				echo "   → $f をインストール中..."
				install_formula "$f"
			done
		fi
	fi
fi
echo ""

##########
# Step 5.6: App Store アプリ (mas)
#   必須は無い。RunCatNeo も選択式でよい — tasks/claude.yml の seed はカードの JSON を
#   書くだけで、RunCat が入っていなくても失敗しない (issue #191)。
##########
echo "🛍  Step 5.6: App Store アプリ (mas)"
echo "   ℹ️  事前に App Store.app でサインインしておいてください (mas は CLI から sign-in できません)"
# mas は homebrew_packages_required に宣言してあるが、それを適用する playbook は
# この後 (Step 6) なので自前で用意する。**導入状況を報告する**のにも要る (issue #177)
if ! command -v mas >/dev/null 2>&1; then
	echo "   📦 mas をインストールします (vars/packages.yml に宣言済み)..."
	brew install mas
fi

APPSTORE_LINES=$(yaml_appstore appstore_apps_optional)
INSTALLED_IDS=$(mas list 2>/dev/null | awk '{print $1}')
if [[ -z "$APPSTORE_LINES" ]]; then
	echo "   ⚠️  appstore_apps_optional が空です。スキップします。"
else
	banner "インストール状況"
	MAS_MISSING=()
	MAS_LABELS=()
	while IFS=$'\t' read -r app_id app_name; do
		[[ -z "$app_id" ]] && continue
		if echo "$INSTALLED_IDS" | grep -qx "$app_id"; then
			echo "     ✓ $app_name"
		else
			echo "     · $app_name (未導入)"
			MAS_MISSING+=("$app_id")
			MAS_LABELS+=("$app_id $app_name")
		fi
	done <<< "$APPSTORE_LINES"
	echo ""

	if (( ${#MAS_MISSING[@]} > 0 )); then
		MAS_SELECTED=()
		if [[ "$OPTIONAL_MODE" == none ]]; then
			echo "   ℹ️  App Store アプリは入れません。要るときは次で入れられます:"
			for l in "${MAS_LABELS[@]}"; do echo "      mas install ${l%% *}   # ${l#* }"; done
		elif [[ "$OPTIONAL_MODE" == all ]]; then
			MAS_SELECTED=( "${MAS_MISSING[@]}" )
		else
			# 行は "<id> <名前>"。fzf には名前ごと見せ、選ばれた行から id を取る
			MAS_SELECTED=( ${(f)"$(printf '%s\n' "${MAS_LABELS[@]}" | fzf_select \
				'App Store アプリ (未導入のみ)' \
				'printf "%s\n\nApp Store ID: %s\n" {2..} {1}' right:40%:wrap \
				| awk '{print $1}')"} )
		fi

		if (( ${#MAS_SELECTED[@]} == 0 )) && [[ "$OPTIONAL_MODE" != none ]]; then
			echo "   ℹ️  App Store アプリは選ばれませんでした"
		fi
		for app_id in "${MAS_SELECTED[@]}"; do
			[[ -z "$app_id" ]] && continue
			echo "   → $app_id をインストール中..."
			if ! mas install "$app_id"; then
				echo "   ⚠️  インストールに失敗しました ($app_id)。"
				echo "      App Store.app でサインインしているか確認してください。"
			fi
		done
	fi
fi
echo ""

##########
# Step 6: Playbook 実行
##########
echo "🎯 Step 6: Ansible Playbook の実行"
echo ""
echo "============================================================"
echo "  Playbook を実行します"
echo "============================================================"
echo ""
ansible-playbook -i "localhost," "$PLAYBOOK"
echo ""

##########
# Step 7: 秘密のキャッシュを温める
##########
# 無人セッション (Claude の hook・scheduled task) は初回の読み出しで 1Password の承認を
# 待って止まる。人がいるここで承認を済ませ、Keychain に入れておく (setup#154)。
# 1Password へのサインインと CLI 連携は GUI 操作なので、まだなら失敗する — 止めずに案内する
echo "🔑 Step 7: 秘密のキャッシュを温める (1Password の承認が出ます)"
if "${PLAYBOOK:h}/bin/secret-read" --warm; then
	echo "   ✓ キャッシュ済み"
else
	echo "   ⚠️  温められなかった参照があります。1Password にサインインして"
	echo "      設定 > 開発者 の「1Password CLI と連携」を有効にしてから打ち直してください:"
	echo "        ${PLAYBOOK:h}/bin/secret-read --warm"
fi
echo ""
echo "============================================================"
echo "  ✅ セットアップが完了しました!"
echo "============================================================"
echo ""
# 鍵が使えないと playbook はコミット署名の設定だけを飛ばして進む (tasks/git.yml。issue #196)。
# 飛ばす理由は「鍵が無い」だけではなく、鍵タイプや承認要求の残りも含む (issue #273) ので、
# 何が欠けているかは ssh-key-check に言わせる。playbook の出力は長く途中の 1 行は埋もれる
# ので、残っていることをここでもう一度言う
if [[ ! -s "$HOME/.ssh/git_signing_key.pub" ]]; then
	echo "  ⚠️  コミット署名はまだ設定されていません (Secretive の鍵が要件を満たしていないため)。"
	echo "     何が足りないかを見る:"
	echo "       ${PLAYBOOK:h}/bin/ssh-key-check"
	echo "     直したら、次を流してください (手順は tasks/ssh.yml の冒頭):"
	echo "       ansible-playbook ${PLAYBOOK} --tags ssh,git"
	echo ""
fi
echo "  次のステップ:"
echo "    1. ターミナルを再起動してください"
echo "    2. 一度ログアウトして入り直してください"
echo "       キーリピート・トラックパッド・日本語入力の句読点・修飾キーの入れ替えは、"
echo "       defaults に書いても動いているプロセスには届きません。"
echo "       効いていないように見えても、"
echo "       再ログイン後に効きます (Dock だけは playbook が入れ直すので即時)"
echo "    3. インストールされたアプリを起動して初期設定を行ってください"
echo "    4. 手作業が残る項目 (Secretive の鍵・1Password の CLI 連携など) は README の「新しいマシンで」を参照"
echo ""

# 入れられなかった cask。途中で止めなかったぶん、ここで必ず見える形にする
if (( ${#FORMULA_FAILED[@]} > 0 )); then
	echo "  ⚠️  入れられなかったツール (任意): ${FORMULA_FAILED[*]}"
	echo "     後から入れるには: brew install ${FORMULA_FAILED[*]}"
	echo ""
fi
if (( ${#CASK_FAILED_OPTIONAL[@]} > 0 )); then
	echo "  ⚠️  入れられなかったアプリ (任意): ${CASK_FAILED_OPTIONAL[*]}"
	echo "     後から入れるには: brew install --cask --adopt ${CASK_FAILED_OPTIONAL[*]}"
	echo ""
fi
if (( ${#CASK_FAILED_REQUIRED[@]} > 0 )); then
	echo "  ❌ 入れられなかったアプリ (必須): ${CASK_FAILED_REQUIRED[*]}"
	echo "     鍵まわりと playbook の前提です。手で入れてある版が cask の版と違うと --adopt は"
	echo "     取り込めません。アプリを最新にしてから次を流してください:"
	echo "       brew install --cask --adopt ${CASK_FAILED_REQUIRED[*]}"
	echo ""
	exit 1
fi
