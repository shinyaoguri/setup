#!/usr/bin/env zsh

set -e  # エラーで即座に終了

# ----- 変数初期化 -----
localmode=false
SCRIPT_DIR="${0:A:h}"  # スクリプトのディレクトリを取得

usage() {
	echo "Usage: $0 [-l] [-h] [--with-optional] [--no-optional]"
	echo "  -l              : ローカルモード (リポジトリをクローン済みの場合)"
	echo "  --with-optional : 選択を出さず、optional をすべて入れる"
	echo "  --no-optional   : 選択を出さず、optional を入れない"
	echo "  -h              : ヘルプを表示"
}

# ----- オプション解析 -----
# 長い形は getopts が扱えないので先に抜き、後段へそのまま渡す (意味を解釈するのは
# sillicon_mac_setup.zsh)。ここで抜かないと getopts が `bad option: --` で弾き、README の
# `zsh setup.zsh --with-optional` が入口で落ちる (issue #194)。
# 知らない長い形は抜かずに残すので、下の getopts が弾く — 黙って無視して進まない。
forward=()
args=()
for a in "$@"; do
	case "$a" in
		--with-optional|--no-optional) forward+=("$a") ;;
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
			zsh "$setup_script" -l "${forward[@]}"
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
		# 後始末は trap に寄せる。後段が失敗すると set -e がその場で抜けるので、
		# 実行の後ろに rm を並べても届かない (issue #195)。
		trap 'rm -f "$remote_script"' EXIT
		if ! curl -H 'Cache-Control: no-cache' -fsSL \
			https://raw.githubusercontent.com/shinyaoguri/setup/main/sillicon_mac_setup.zsh \
			-o "$remote_script"; then
			echo "❌ セットアップスクリプトを取得できませんでした (ネットワークを確認してください)"
			exit 1
		fi
		# 終了コードは変数で受けない。失敗なら set -e がそのコードで抜け、成功なら
		# そのまま 0 で終わる。以前は `status=$?` で受けていたが、zsh の `status` は
		# `$?` の別名の**読み取り専用変数**で、代入が失敗して成功時も exit 1 になっていた。
		zsh "$remote_script" "${forward[@]}"
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
