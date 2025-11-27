#!/bin/zsh
#######################################
# MIT License
# Copyright (c) 2022 Shinya Oguri
# https://github.com/shinyaoguri/setup
#######################################

echo "-----\nOS"
if [[ "$(uname)" = 'Darwin' ]] && [[ "$(uname -m)" = 'x86_64' ]]; then
  echo '- x86_64 Mac'
  echo '- Sorry, no configuration file yet.'
elif [[ "$(uname)" = 'Darwin' ]] && [[ "$(uname -m)" = 'arm64' ]]; then
  echo '- arm64 Mac'
  echo '-----\nStarting interactive setup'

  # スクリプトの実行場所を判定
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

  # ローカル実行かリモート実行かを判定
  if [[ -f "$SCRIPT_DIR/interactive_setup.sh" ]] && [[ -f "$SCRIPT_DIR/arm64_mac_setup_dynamic.sh" ]] && [[ -f "$SCRIPT_DIR/ansible_arm64_mac_dynamic.yml" ]]; then
    # ローカル実行モード
    echo "ローカルファイルを使用してセットアップを実行します..."
    cd "$SCRIPT_DIR"

    # Set environment variable for local execution
    export SETUP_LOCAL_EXEC=1

    # Run interactive setup
    ./interactive_setup.sh

    # If configuration was created successfully, run the dynamic setup
    if [[ -f "selected_packages.yml" ]]; then
      echo ""
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "設定ファイルが生成されました。続けてインストールを開始します..."
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo ""

      # 短い待機時間を設定（ユーザーが確認できるように）
      sleep 2

      # Set environment variables
      export SETUP_AUTO_CONFIRM=1  # 自動確認モード

      # Run the setup script
      echo ""
      echo "インストールを開始します..."
      echo ""
      ./arm64_mac_setup_dynamic.sh

      # Get exit status
      SETUP_STATUS=$?

      # Final message
      if [[ $SETUP_STATUS -eq 0 ]]; then
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✅ セットアップが正常に完了しました！"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "ターミナルを再起動して変更を反映させてください。"
        exit 0
      else
        echo ""
        echo "⚠️ セットアップ中にエラーが発生しました。"
        exit 1
      fi
    else
      echo 'Setup cancelled or failed.'
      exit 1
    fi
  else
    # リモート実行モード（従来通り）
    echo "GitHubからファイルをダウンロードしてセットアップを実行します..."

    # Create temporary directory for remote execution
    TEMP_DIR="/tmp/mac_setup_$(date +%s)"
    mkdir -p "$TEMP_DIR"
    cd "$TEMP_DIR"

    # Download interactive setup script
    echo 'Downloading interactive setup script...'
    curl -H 'Cache-Control: no-cache' -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/interactive_setup.sh -o interactive_setup.sh
    chmod +x interactive_setup.sh

    # Set environment variable for remote execution
    export SETUP_REMOTE_EXEC=1

    # Run interactive setup to create configuration
    ./interactive_setup.sh

    # If configuration was created successfully, run the dynamic setup
    if [[ -f "selected_packages.yml" ]]; then
      echo ""
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "設定ファイルが生成されました。続けてインストールを開始します..."
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo ""

      # 短い待機時間を設定（ユーザーが確認できるように）
      sleep 2

      # Download the dynamic setup script and related files
      echo "必要なファイルをダウンロード中..."
      curl -H 'Cache-Control: no-cache' -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/arm64_mac_setup_dynamic.sh -o arm64_mac_setup_dynamic.sh
      curl -H 'Cache-Control: no-cache' -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/ansible_arm64_mac_dynamic.yml -o ansible_arm64_mac_dynamic.yml
      curl -H 'Cache-Control: no-cache' -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/config.fish -o config.fish
      chmod +x arm64_mac_setup_dynamic.sh

      # Set environment variables for remote execution
      export SETUP_REMOTE_EXEC=1
      export SETUP_AUTO_CONFIRM=1  # 自動確認モード

      # Run the setup script
      echo ""
      echo "インストールを開始します..."
      echo ""
      ./arm64_mac_setup_dynamic.sh

      # Get exit status
      SETUP_STATUS=$?

      # Cleanup
      cd ~
      rm -rf "$TEMP_DIR"

      # Final message
      if [[ $SETUP_STATUS -eq 0 ]]; then
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✅ セットアップが正常に完了しました！"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "ターミナルを再起動して変更を反映させてください。"
        exit 0
      else
        echo ""
        echo "⚠️ セットアップ中にエラーが発生しました。"
        exit 1
      fi
    else
      echo 'Setup cancelled or failed.'
      cd ~
      rm -rf "$TEMP_DIR"
      exit 1
    fi
  fi
elif [[ "$(uname -s | cut -c1-5)" = 'Linux' ]]; then
  OS='Linux'
  echo '- Linux'
  echo '- Sorry, no configuration file yet.'
elif [[ "$(uname -s | cut -c1-10)" = 'MINGW32_NT' ]]; then
  OS='Cygwin'
  echo '- Windows'
  echo '- Sorry, no configuration file yet.'
else
  echo "Your platform ($(uname -a)) is not supported."
  exit 1
fi
