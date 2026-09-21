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
echo ""

##########
# Step 1.5: Playbook パス設定 (cloud モードはリポジトリをクローン)
##########
if [[ "$localmode" == true ]]; then
	SCRIPT_DIR="${0:A:h}"
	PLAYBOOK="$SCRIPT_DIR/playbook_sillicon_mac.yml"
else
	echo "📥 Step 1.5: setup リポジトリの準備 ($SETUP_DIR)"
	if [[ -d "$SETUP_DIR/.git" ]]; then
		REMOTE_URL=$(git -C "$SETUP_DIR" remote get-url origin 2>/dev/null || echo "")
		if [[ "$REMOTE_URL" == *"shinyaoguri/setup"* ]]; then
			echo "   ✓ 既存リポジトリを更新します..."
			git -C "$SETUP_DIR" pull --ff-only || echo "   ⚠️  pull に失敗しました (ローカル変更がある可能性)"
		else
			echo "   ❌ $SETUP_DIR は別のリポジトリです: $REMOTE_URL"
			exit 1
		fi
	elif [[ -e "$SETUP_DIR" ]]; then
		echo "   ❌ $SETUP_DIR が既に存在します (Git リポジトリではない)"
		exit 1
	else
		echo "   📦 リポジトリをクローンします..."
		git clone "$GITHUB_REPO_URL" "$SETUP_DIR"
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

	printf '%s\n' "${candidates[@]}" | fzf --multi \
		--height=80% --border=rounded --layout=reverse \
		--marker='◉ ' --pointer='▸' \
		--header=$''"$title"$'\n Tab で選択 / Enter で決定 / Esc で何も入れずに進む' \
		--preview 'brew info --cask {1} 2>/dev/null || brew info {1} 2>/dev/null' \
		--preview-window=right:55%:wrap || true
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
			MAS_SELECTED=( ${(f)"$(printf '%s\n' "${MAS_LABELS[@]}" | fzf --multi \
				--height=80% --border=rounded --layout=reverse \
				--marker='◉ ' --pointer='▸' \
				--header=$'App Store アプリ (未導入のみ)\n Tab で選択 / Enter で決定 / Esc で何も入れずに進む' \
				--preview 'printf "%s\n\nApp Store ID: %s\n" {2..} {1}' \
				--preview-window=right:40%:wrap | awk '{print $1}')"} )
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
# 鍵が無いと playbook はコミット署名の設定だけを飛ばして進む (tasks/git.yml。issue #196)。
# playbook の出力は長く、途中の 1 行は埋もれるので、残っていることをここでもう一度言う
if [[ ! -s "$HOME/.ssh/git_signing_key.pub" ]]; then
	echo "  ⚠️  コミット署名はまだ設定されていません (Secretive に鍵が無いため)。"
	echo "     鍵を作って GitHub に登録したら、次を流してください (手順は tasks/ssh.yml の冒頭):"
	echo "       ansible-playbook ${PLAYBOOK} --tags ssh,git"
	echo ""
fi
echo "  次のステップ:"
echo "    1. ターミナルを再起動してください"
echo "    2. 一度ログアウトして入り直してください"
echo "       キーリピート・トラックパッド・日本語入力の句読点は、defaults に書いても"
echo "       動いているプロセスには届きません。効いていないように見えても、"
echo "       再ログイン後に効きます (Dock だけは playbook が入れ直すので即時)"
echo "    3. インストールされたアプリを起動して初期設定を行ってください"
echo "    4. 手作業が残る項目 (Secretive の鍵・1Password の CLI 連携など) は README の「新しいマシンで」を参照"
echo ""

# 入れられなかった cask。途中で止めなかったぶん、ここで必ず見える形にする
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
