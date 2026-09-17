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
			zsh "$setup_script" -l
		else
			echo "❌ Error: sillicon_mac_setup.zsh not found in $SCRIPT_DIR"
			exit 1
		fi
		;;
	cloud-sillicon-mac)
		echo "🌐 Cloud Mode: Downloading setup files..."
		# `zsh -c "$(curl ...)"` は**コマンド置換が終了ステータスを捨てる**ので、取得に
		# 失敗すると空のスクリプトを実行して何事もなく終わり、途中で切断されると
		# 部分的なスクリプトを実行する。後段は sudo セッションを握って cask / mas /
		# ansible を回すので、途中実行には実害がある (issue #166)。
		# 一度ファイルへ落として、取得の成否を確かめてから実行する。
		remote_script=$(mktemp)
		if ! curl -H 'Cache-Control: no-cache' -fsSL \
			https://raw.githubusercontent.com/shinyaoguri/setup/main/sillicon_mac_setup.zsh \
			-o "$remote_script"; then
			rm -f "$remote_script"
			echo "❌ セットアップスクリプトを取得できませんでした (ネットワークを確認してください)"
			exit 1
		fi
		zsh "$remote_script"
		status=$?
		rm -f "$remote_script"
		exit $status
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
