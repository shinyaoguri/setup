#!/usr/bin/env zsh

set -e  # エラーで即座に終了

# ----- 変数初期化 -----
localmode=false
GITHUB_RAW_URL="https://raw.githubusercontent.com/shinyaoguri/setup/main"

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

# ----- Playbook パス設定 -----
if [[ "$localmode" == true ]]; then
	SCRIPT_DIR="${0:A:h}"
	PLAYBOOK="$SCRIPT_DIR/playbook_sillicon_mac.yml"
else
	# クラウドモード: 一時ファイルにダウンロード
	PLAYBOOK=$(mktemp)
	curl -fsSL "$GITHUB_RAW_URL/playbook_sillicon_mac.yml" -o "$PLAYBOOK"
	
	# 終了時にクリーンアップ
	cleanup() {
		rm -f "$PLAYBOOK"
	}
	trap cleanup EXIT
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
	echo "   ⏸️  インストールが完了したら、このスクリプトを再実行してください:"
	echo "      zsh $0"
	echo ""
	exit 0
fi
echo ""

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
	/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

	# Homebrew のパスを通す (Apple Silicon の場合)
	if [[ -f "/opt/homebrew/bin/brew" ]]; then
		eval "$(/opt/homebrew/bin/brew shellenv)"
		echo ""
		echo "   ✓ Homebrew のインストールが完了しました"
		echo ""
		echo "   ⚠️  シェルの設定を更新してください:"
		echo '      echo '\''eval "$(/opt/homebrew/bin/brew shellenv)"'\'' >> ~/.zshrc'
		echo ""
		echo "   このスクリプトを再実行してください:"
		echo "      zsh $0"
		exit 0
	fi
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
# Step 5: Playbook 実行
##########
echo "🎯 Step 5: Ansible Playbook の実行"
echo ""
echo "============================================================"
echo "  Playbook を実行します"
echo "============================================================"
echo ""
ansible-playbook "$PLAYBOOK"
echo ""
echo "============================================================"
echo "  ✅ セットアップが完了しました!"
echo "============================================================"
echo ""
echo "  次のステップ:"
echo "    1. ターミナルを再起動してください"
echo "    2. システム環境設定で各種設定を確認してください"
echo "    3. インストールされたアプリを起動して初期設定を行ってください"
echo ""