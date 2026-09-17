#!/usr/bin/env zsh

set -e # errorで即座に終了させる

# ----- 変数初期化 -----
localmode=false
GITHUB_REPO_URL="https://github.com/shinyaoguri/setup.git"
SETUP_DIR="$HOME/.setup"

# ----- オプション解析 -----
while getopts "lh" opt; do
	case "$opt" in
		l) localmode=true;;
		h)
			echo "Usage: $0 [-l] [-h]"
			echo "  -l : ローカルモード (リポジトリをクローン済みの場合)"
			echo "  -h : ヘルプを表示"
			exit 0
			;;
		*)
			echo "Usage: $0 [-l] [-h]"
			exit 1
			;;
	esac
done

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
# Step 5.5: Homebrew Cask アプリのインストール
#   Ansible 配下の subprocess は controlling TTY を持たないため、cask 内部の
#   `sudo /usr/sbin/installer ...` が credential cache を引けず失敗する
#   (tty_tickets による隔離)。bootstrap シェルの interactive TTY で先に
#   走らせることで sudo が通る前提を満たす。
##########
echo "🍺 Step 5.5: Homebrew Cask アプリのインストール"
PACKAGES_YAML="${SETUP_DIR}/vars/packages.yml"
if [[ "$localmode" == true ]]; then
	PACKAGES_YAML="${SCRIPT_DIR}/vars/packages.yml"
fi
if [[ ! -f "$PACKAGES_YAML" ]]; then
	echo "   ❌ $PACKAGES_YAML が見つかりません"
	exit 1
fi
# vars/packages.yml の homebrew_cask_packages を抜き出し (依存追加なしで awk 解析)
CASKS=( $(awk '
	/^homebrew_cask_packages:/ { flag=1; next }
	/^[^ #]/                   { flag=0 }
	flag && /^[[:space:]]*-[[:space:]]/ {
		sub(/^[[:space:]]*-[[:space:]]*/,"")
		print
	}
' "$PACKAGES_YAML") )
if (( ${#CASKS[@]} == 0 )); then
	echo "   ⚠️  homebrew_cask_packages が空です。スキップします。"
else
	for cask in "${CASKS[@]}"; do
		if brew list --cask "$cask" >/dev/null 2>&1; then
			echo "   ✓ $cask は既にインストール済みです"
		else
			echo "   → $cask をインストール中..."
			brew install --cask "$cask"
		fi
	done
fi
echo ""

##########
# Step 5.6: App Store アプリのインストール (mas)
#   mas install も Xcode など .pkg 系で内部的に sudo /usr/sbin/installer を呼ぶため、
#   Cask と同じく Ansible 配下では TTY 不足でコケる。bootstrap の interactive TTY で先に走らせる。
##########
echo "🛍  Step 5.6: App Store アプリのインストール (mas)"
echo "   ℹ️  事前に App Store.app でサインインしておいてください (mas は CLI から sign-in できません)"
if ! command -v mas >/dev/null 2>&1; then
	echo "   📦 mas をインストールします..."
	brew install mas
fi
# vars/packages.yml の appstore_apps を id<TAB>name 形式で抜き出し
APPSTORE_LINES=$(awk '
	/^appstore_apps:/  { flag=1; next }
	/^[^ #]/           { flag=0 }
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
' "$PACKAGES_YAML")
INSTALLED_IDS=$(mas list 2>/dev/null | awk '{print $1}')
if [[ -z "$APPSTORE_LINES" ]]; then
	echo "   ⚠️  appstore_apps が空です。スキップします。"
else
	while IFS=$'\t' read -r app_id app_name; do
		[[ -z "$app_id" ]] && continue
		if echo "$INSTALLED_IDS" | grep -qx "$app_id"; then
			echo "   ✓ $app_name ($app_id) は既にインストール済みです"
		else
			echo "   → $app_name ($app_id) をインストール中..."
			if ! mas install "$app_id"; then
				echo "   ⚠️  $app_name のインストールに失敗しました。"
				echo "      App Store.app でサインインしているか確認してください。"
			fi
		fi
	done <<< "$APPSTORE_LINES"
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
echo "  次のステップ:"
echo "    1. ターミナルを再起動してください"
echo "    2. 一度ログアウトして入り直してください"
echo "       キーリピート・トラックパッド・日本語入力の句読点は、defaults に書いても"
echo "       動いているプロセスには届きません。効いていないように見えても、"
echo "       再ログイン後に効きます (Dock だけは playbook が入れ直すので即時)"
echo "    3. インストールされたアプリを起動して初期設定を行ってください"
echo "    4. 手作業が残る項目 (Secretive の鍵・1Password の CLI 連携など) は README の「新しいマシンで」を参照"
echo ""
