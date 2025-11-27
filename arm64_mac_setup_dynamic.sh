#!/bin/zsh
#######################################
# MIT License
# Copyright (c) 2022 Shinya Oguri
# https://github.com/shinyaoguri/setup
#######################################

# Dynamic setup script that uses selected_packages.yml

# カラー定義
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 設定ファイルのパス
CONFIG_FILE="selected_packages.yml"

########
# Xcode
########
echo -e "-----\nCheck Xcode"
if type "xcode-select" >/dev/null 2>&1; then
  echo -e "✅ Xcode already exist"
else
  echo -e "🙅 Xcode was not exist\n>>> Please install Xcode from AppStore."
  open "https://apps.apple.com/jp/app/xcode/id497799835"
  echo -e "${YELLOW}Please install Xcode and run this script again.${NC}"
  return 2> /dev/null
  exit
fi

###########
# Homebrew
###########
echo -e "-----\nCheck Homebrew"
if [ -f ~/.zshrc ]; then
  if [ "`echo $PATH | grep '/opt/homebrew/bin'`" ]; then
    echo '✅ Homebrew PATH already exist'
  else
    echo '🙅 Homebrew PATH was not exist\n...update .zshrc'
    echo 'export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH' >> ~/.zshrc
    source ~/.zshrc
  fi
else
  echo '🙅 .zshrc was not exist\n...update .zshrc'
  echo 'export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH' >> ~/.zshrc
  source ~/.zshrc
fi

if type "brew" >/dev/null 2>&1; then
  echo -e "✅ brew already exist"
else
  echo -e "🙅 Homebrew was not exist\nInstalling Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  # Add Homebrew to PATH for current session
  if [[ -f /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi

##########
# Ansible
##########
echo -e "-----\nCheck Ansible"
if type "ansible" >/dev/null 2>&1; then
  echo -e "✅ Ansible already exist"
else
  echo -e "🙅 ansible was not installed"
  brew install ansible
fi

##########
# mas (Mac App Store CLI)
##########
echo -e "-----\nCheck mas (Mac App Store CLI)"
if type "mas" >/dev/null 2>&1; then
  echo -e "✅ mas already exist"
else
  echo -e "🙅 mas was not installed"
  brew install mas
fi

##########
# Configuration File Check
##########
echo -e "-----\nCheck Configuration"

# ローカル実行の場合、interactive_setup.zshを実行
if [ -z "$SETUP_REMOTE_EXEC" ]; then
  # ローカル実行モード
  echo -e "${CYAN}Running in local mode${NC}"

  if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${YELLOW}Configuration file not found. Starting interactive setup...${NC}"

    # interactive_setup.shが存在するか確認
    if [ -f "./interactive_setup.sh" ]; then
      ./interactive_setup.sh

      # 設定ファイルが生成されたか確認
      if [ ! -f "$CONFIG_FILE" ]; then
        echo -e "${RED}Setup cancelled or configuration file was not created.${NC}"
        exit 1
      fi
    else
      echo -e "${RED}interactive_setup.sh not found in current directory${NC}"
      echo -e "${YELLOW}Please ensure all setup files are in the same directory${NC}"
      exit 1
    fi
  fi
else
  # リモート実行モード（setup.sh経由）
  echo -e "${CYAN}Running in remote mode (via setup.sh)${NC}"

  if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Configuration file not found: $CONFIG_FILE${NC}"
    echo -e "${RED}This should not happen in remote mode. Exiting.${NC}"
    exit 1
  fi
fi

echo -e "${GREEN}✅ Configuration file found: $CONFIG_FILE${NC}"
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}📋 インストール予定の項目${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Homebrew packages
echo -e "${BLUE}▶ Homebrew パッケージ:${NC}"
if grep -q "^homebrew_packages:" "$CONFIG_FILE" 2>/dev/null; then
  grep "^  - name:" "$CONFIG_FILE" 2>/dev/null | sed 's/  - name: /  • /' | head -20
  HOMEBREW_COUNT=$(grep "^  - name:" "$CONFIG_FILE" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$HOMEBREW_COUNT" -gt 20 ]; then
    echo "  ... 他 $((HOMEBREW_COUNT - 20)) 項目"
  fi
else
  echo "  なし"
  HOMEBREW_COUNT=0
fi
echo ""

# Homebrew Cask packages
echo -e "${BLUE}▶ デスクトップアプリケーション (Cask):${NC}"
CASK_START=false
CASK_COUNT=0
while IFS= read -r line; do
  if [[ "$line" == "homebrew_cask_packages:" ]]; then
    CASK_START=true
  elif [[ "$CASK_START" == true ]]; then
    if [[ "$line" =~ ^[[:space:]]*-[[:space:]]name:[[:space:]](.+)$ ]]; then
      if [ $CASK_COUNT -lt 20 ]; then
        echo "  • ${BASH_REMATCH[1]}"
      fi
      ((CASK_COUNT++))
    elif [[ ! "$line" =~ ^[[:space:]] ]]; then
      CASK_START=false
    fi
  fi
done < "$CONFIG_FILE"

if [ "$CASK_COUNT" -eq 0 ]; then
  echo "  なし"
elif [ "$CASK_COUNT" -gt 20 ]; then
  echo "  ... 他 $((CASK_COUNT - 20)) 項目"
fi
echo ""

# App Store apps
echo -e "${BLUE}▶ App Store アプリ:${NC}"
APPSTORE_START=false
APPSTORE_COUNT=0
while IFS= read -r line; do
  if [[ "$line" == "appstore_apps:" ]]; then
    APPSTORE_START=true
  elif [[ "$APPSTORE_START" == true ]]; then
    if [[ "$line" =~ ^[[:space:]]*-[[:space:]]([0-9]+)$ ]]; then
      if [ $APPSTORE_COUNT -lt 10 ]; then
        # App IDから名前を推測（既知のIDのみ）
        case "${BASH_REMATCH[1]}" in
          "497799835") echo "  • Xcode" ;;
          "682658836") echo "  • GarageBand" ;;
          "424389933") echo "  • Final Cut Pro" ;;
          "409203825") echo "  • Numbers" ;;
          "409201541") echo "  • Pages" ;;
          "409183694") echo "  • Keynote" ;;
          "803453959") echo "  • Slack" ;;
          "539883307") echo "  • LINE" ;;
          *) echo "  • App ID: ${BASH_REMATCH[1]}" ;;
        esac
      fi
      ((APPSTORE_COUNT++))
    elif [[ ! "$line" =~ ^[[:space:]] ]]; then
      APPSTORE_START=false
    fi
  fi
done < "$CONFIG_FILE"

if [ "$APPSTORE_COUNT" -eq 0 ]; then
  echo "  なし"
elif [ "$APPSTORE_COUNT" -gt 10 ]; then
  echo "  ... 他 $((APPSTORE_COUNT - 10)) 項目"
fi
echo ""

# Development environments
echo -e "${BLUE}▶ 開発環境:${NC}"
DEV_START=false
DEV_ENV_COUNT=0
while IFS= read -r line; do
  if [[ "$line" == "development_environments:" ]]; then
    DEV_START=true
  elif [[ "$DEV_START" == true ]]; then
    if [[ "$line" =~ ^[[:space:]]*-[[:space:]](.+)$ ]]; then
      echo "  • ${BASH_REMATCH[1]}"
      ((DEV_ENV_COUNT++))
    elif [[ ! "$line" =~ ^[[:space:]] ]]; then
      DEV_START=false
    fi
  fi
done < "$CONFIG_FILE"

if [ "$DEV_ENV_COUNT" -eq 0 ]; then
  echo "  なし"
fi
echo ""

# macOS settings
echo -e "${BLUE}▶ macOS 設定変更:${NC}"
MACOS_START=false
MACOS_COUNT=0
while IFS= read -r line; do
  if [[ "$line" == "macos_settings:" ]]; then
    MACOS_START=true
  elif [[ "$MACOS_START" == true ]]; then
    if [[ "$line" =~ ^[[:space:]]*-[[:space:]](.+)$ ]]; then
      case "${BASH_REMATCH[1]}" in
        "dock_autohide") echo "  • Dockを自動的に隠す" ;;
        "dock_size") echo "  • Dockサイズ調整" ;;
        "finder_show_extensions") echo "  • ファイル拡張子を表示" ;;
        "finder_show_hidden") echo "  • 隠しファイルを表示" ;;
        "keyboard_repeat") echo "  • キーリピート速度を最速に" ;;
        "trackpad_tap_click") echo "  • タップでクリックを有効化" ;;
        "screenshots_location") echo "  • スクリーンショット保存先変更" ;;
        *) echo "  • ${BASH_REMATCH[1]}" ;;
      esac
      ((MACOS_COUNT++))
    elif [[ ! "$line" =~ ^[[:space:]] ]]; then
      MACOS_START=false
    fi
  fi
done < "$CONFIG_FILE"

if [ "$MACOS_COUNT" -eq 0 ]; then
  echo "  なし"
fi

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}合計: ${GREEN}$((HOMEBREW_COUNT + CASK_COUNT + APPSTORE_COUNT + DEV_ENV_COUNT + MACOS_COUNT))${NC} 項目"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

##########
# User Confirmation
##########
echo ""
echo -e "${YELLOW}上記の項目をインストールしてよろしいですか？${NC}"

# 確認待ち
while true; do
  echo -n "続行しますか？ (yes/no): "
  read response

  case "$response" in
    [yY][eE][sS]|[yY]|"")
      echo -e "${GREEN}インストールを開始します...${NC}"
      break
      ;;
    [nN][oO]|[nN])
      echo -e "${RED}インストールをキャンセルしました${NC}"
      exit 0
      ;;
    *)
      echo -e "${RED}yes または no で答えてください${NC}"
      ;;
  esac
done

echo ""

##########
# Ansible Deploy
##########
echo -e "-----\n${GREEN}Starting Ansible Deployment${NC}"

# Playbookの取得方法を決定
if [ -z "$SETUP_REMOTE_EXEC" ]; then
  # ローカル実行モード
  if [ -f ./ansible_arm64_mac_dynamic.yml ]; then
    echo -e "Using local ansible_arm64_mac_dynamic.yml"
    PLAYBOOK_PATH="./ansible_arm64_mac_dynamic.yml"
  else
    echo -e "${RED}ansible_arm64_mac_dynamic.yml not found in current directory${NC}"
    echo -e "${YELLOW}Please ensure all setup files are in the same directory${NC}"
    exit 1
  fi
else
  # リモート実行モード（既にダウンロード済み）
  if [ -f ./ansible_arm64_mac_dynamic.yml ]; then
    echo -e "Using downloaded ansible_arm64_mac_dynamic.yml"
    PLAYBOOK_PATH="./ansible_arm64_mac_dynamic.yml"
  else
    # フォールバック: ダウンロード
    echo -e "Downloading ansible_arm64_mac_dynamic.yml"
    curl -O -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/ansible_arm64_mac_dynamic.yml
    PLAYBOOK_PATH="./ansible_arm64_mac_dynamic.yml"
  fi
fi

if [ -f "$PLAYBOOK_PATH" ]; then
  # Install required Ansible collections
  echo -e "\nInstalling Ansible collections..."
  ansible-galaxy collection install community.general

  # Run the playbook with the configuration file
  echo -e "\n${GREEN}Running Ansible playbook...${NC}"
  ansible-playbook "$PLAYBOOK_PATH" --extra-vars "config_file=$CONFIG_FILE" --ask-become-pass

  # Clean up if we downloaded the playbook
  if [ "$PLAYBOOK_PATH" = "./ansible_arm64_mac_dynamic.yml" ] && [ ! -f ./ansible_arm64_mac_dynamic.yml ]; then
    rm "$PLAYBOOK_PATH"
  fi

  echo -e "\n${GREEN}✅ Setup completed successfully!${NC}"
  echo -e "${YELLOW}Please restart your terminal for all changes to take effect.${NC}"
else
  echo -e "${RED}🙅 ansible-playbook was not downloaded${NC}"
  exit 1
fi