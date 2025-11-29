#!/usr/bin/env zsh

set -e  # エラーで即座に終了

# ----- 変数初期化 -----
localmode=false
SCRIPT_DIR="${0:A:h}"  # スクリプトのディレクトリを取得

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

# ----- ヘッダー表示 -----
echo "============================================================"
echo "  macOS 環境自動セットアップ"
echo "============================================================"
echo ""

# ----- 実行モード判定 -----
echo "📋 MODE Check"
if [[ "$localmode" == true ]]; then
	echo "   ✓ Local Mode (ローカルファイルから実行)"
	MODE="local"
else
	echo "   ✓ Cloud Mode (Web経由で実行)"
	MODE="cloud"
fi
echo ""

# ----- OS判定 -----
echo "💻 OS Check"
os=$(uname)
arch=$(uname -m)

if [[ "$os" == "Darwin" && "$arch" == "arm64" ]]; then
	echo "   ✓ Apple Silicon Mac detected"
	PLATFORM="sillicon-mac"
elif [[ "$os" == "Darwin" && "$arch" == "x86_64" ]]; then
	echo "   ⚠️  Intel Mac detected (このスクリプトはApple Silicon用です)"
	PLATFORM="intel-mac"
else
	echo "   ✗ Unsupported platform: $os $arch"
	PLATFORM="unknown"
fi
echo ""

# ----- 実行 -----
case "$MODE-$PLATFORM" in
	local-sillicon-mac)
		setup_script="$SCRIPT_DIR/sillicon_mac_setup.zsh"
		if [[ -f "$setup_script" ]]; then
			echo "🚀 Starting setup..."
			echo ""
			zsh "$setup_script"
		else
			echo "❌ Error: sillicon_mac_setup.zsh not found in $SCRIPT_DIR"
			exit 1
		fi
		;;
	cloud-sillicon-mac)
		echo "🌐 Cloud Mode: Downloading setup script..."
		# TODO: 実装が必要な場合はここに追加
		echo "❌ Cloud mode is not yet implemented"
		echo "   Please clone the repository and use -l option"
		exit 1
		;;
	*-intel-mac)
		echo "⚠️  Intel Mac はサポートされていません"
		echo "   Apple Silicon 用のスクリプトです"
		exit 1
		;;
	*)
		echo "❌ サポートされていない環境: MODE=$MODE, PLATFORM=$PLATFORM"
		exit 1
		;;
esac
