#!/bin/zsh
#######################################
# MIT License
# Copyright (c) 2022 Shinya Oguri
# https://github.com/shinyaoguri/setup
#######################################

# カラー定義
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color
BOLD='\033[1m'
DIM='\033[2m'

# 設定ファイルのパス
CONFIG_FILE="selected_packages.yml"

# カーソルの位置（表示されている項目のインデックス）
cursor_position=1

# 選択状態を管理する連想配列
typeset -A selected_items

# カテゴリーの展開状態を管理
typeset -A expanded_categories

# 表示用のリスト（現在表示されているアイテムのみ）
typeset -a visible_items
typeset -a visible_types  # "category", "all", "item"
typeset -a visible_keys

# 全アイテムのデータ
typeset -A category_items  # category -> "key:description|key:description|..."

# カテゴリーと表示名
typeset -A category_names
category_names=(
    "homebrew" "📦 Homebrew Packages"
    "homebrew_cask" "🖥️  Desktop Applications"
    "appstore" "🏪 App Store Applications"
    "dev_env" "🔧 Development Environment"
    "macos_settings" "⚙️  macOS System Settings"
)

# カテゴリーの順序
typeset -a category_order
category_order=(homebrew homebrew_cask appstore dev_env macos_settings)

# 初期化
initialize_items() {
    # Homebrew パッケージ
    category_items[homebrew]="git:Git - バージョン管理|git-lfs:Git LFS - 大容量ファイル用拡張|vim:Vim - テキストエディタ|neovim:Neovim - 拡張版Vim|wget:Wget - ダウンロード|fish:Fish Shell - モダンなシェル|tree:Tree - ディレクトリ構造|trash:Trash - ゴミ箱コマンド|jq:jq - JSONプロセッサ|z:z - ディレクトリジャンプ|peco:Peco - インクリメンタルサーチ|pipenv:Pipenv - Python環境管理|anyenv:Anyenv - バージョン管理統合|deno:Deno - JS/TSランタイム|java:Java - プログラミング言語|gnupg:GnuPG - 暗号化ツール"

    # Homebrew Cask パッケージ
    category_items[homebrew_cask]="gyazo:Gyazo - スクリーンショット|google-drive:Google Drive - クラウド|1password:1Password - パスワード管理|rectangle:Rectangle - ウィンドウ管理|google-chrome:Chrome - ブラウザ|firefox:Firefox - ブラウザ|visual-studio-code:VS Code - エディタ|android-studio:Android Studio|processing:Processing|unity-hub:Unity Hub|arduino-ide:Arduino IDE|warp:Warp - ターミナル|chromedriver:ChromeDriver - ブラウザ自動化|docker:Docker - コンテナ"

    # App Store アプリ
    category_items[appstore]="497799835:Xcode - 開発環境|682658836:GarageBand - 音楽制作|424389933:Final Cut Pro|434290957:Motion|409203825:Numbers - 表計算|409201541:Pages - ワープロ|409183694:Keynote|408981434:iMovie|784801555:OneNote|823766827:OneDrive|425424353:The Unarchiver|803453959:Slack|539883307:LINE|1480068668:Messenger|747648890:Telegram"

    # 開発環境
    category_items[dev_env]="nodenv:Node.js 管理|rbenv:Ruby 管理|pyenv:Python 管理|goenv:Go 管理|phpenv:PHP 管理"

    # macOS設定
    category_items[macos_settings]="dock_autohide:Dockを自動的に隠す|dock_size:Dockサイズ調整|finder_show_extensions:拡張子を表示|finder_show_hidden:隠しファイル表示|keyboard_repeat:キーリピート速度|trackpad_tap_click:タップでクリック|screenshots_location:スクリーンショット保存先"

    # デフォルトで最初のカテゴリーだけ展開
    expanded_categories[homebrew]=1
}

# 表示リストを更新
update_visible_items() {
    visible_items=()
    visible_types=()
    visible_keys=()

    for cat in $category_order; do
        # カテゴリーヘッダー
        local cat_name="${category_names[$cat]}"
        local is_expanded="${expanded_categories[$cat]}"
        local icon="▶"
        [[ -n "$is_expanded" ]] && icon="▼"

        # カテゴリー内の選択数をカウント
        local selected_count=0
        local total_count=0
        local items="${category_items[$cat]}"
        for item in ${(s:|:)items}; do
            ((total_count++))
            local key="${item%%:*}"
            [[ -n "${selected_items[${cat}_${key}]}" ]] && ((selected_count++))
        done

        # カテゴリーヘッダー
        if [[ $selected_count -gt 0 ]]; then
            visible_items+=("$icon $cat_name ${GREEN}($selected_count/$total_count)${NC}")
        else
            visible_items+=("$icon $cat_name ${DIM}($total_count)${NC}")
        fi
        visible_types+=("category")
        visible_keys+=("$cat")

        # 展開されている場合はアイテムを表示
        if [[ -n "$is_expanded" ]]; then
            # 全選択オプション
            visible_items+=("    [ ] すべて選択/解除")
            visible_types+=("all")
            visible_keys+=("${cat}_ALL")

            # 各アイテム
            for item in ${(s:|:)items}; do
                local key="${item%%:*}"
                local desc="${item#*:}"
                local full_key="${cat}_${key}"

                local checkbox="[ ]"
                [[ -n "${selected_items[$full_key]}" ]] && checkbox="[${GREEN}✓${NC}]"

                visible_items+=("    $checkbox $key - ${DIM}$desc${NC}")
                visible_types+=("item")
                visible_keys+=("$full_key")
            done
        fi
    done
}

# カテゴリー全選択/解除
toggle_category_selection() {
    local category=$1
    local items="${category_items[$category]}"
    local all_selected=true

    # 全選択されているか確認
    for item in ${(s:|:)items}; do
        local key="${item%%:*}"
        if [[ -z "${selected_items[${category}_${key}]}" ]]; then
            all_selected=false
            break
        fi
    done

    # 切り替え
    for item in ${(s:|:)items}; do
        local key="${item%%:*}"
        local full_key="${category}_${key}"
        if [[ "$all_selected" == true ]]; then
            unset "selected_items[$full_key]"
        else
            selected_items[$full_key]=1
        fi
    done
}

# メニュー表示
display_menu() {
    clear

    echo -e "${BOLD}${CYAN}╔════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}║              macOS Automated Setup - Package Selector              ║${NC}"
    echo -e "${BOLD}${CYAN}╚════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${YELLOW}操作方法:${NC}"
    echo -e "  ${GREEN}↑/↓${NC}: 移動  ${GREEN}Enter/→${NC}: 展開  ${GREEN}Space${NC}: 選択"
    echo -e "  ${GREEN}a${NC}: 全選択  ${GREEN}d${NC}: 全解除  ${GREEN}c${NC}: 決定  ${GREEN}q${NC}: 終了"
    echo -e "${CYAN}─────────────────────────────────────────────────────────────────────${NC}"

    update_visible_items

    # 表示（スクロールを考慮）
    local max_display_lines=15  # 表示する最大行数
    local start_index=1
    local end_index=$#visible_items

    # カーソル位置が画面外の場合、スクロール
    if [[ $cursor_position -gt $max_display_lines ]]; then
        start_index=$((cursor_position - max_display_lines + 1))
        end_index=$cursor_position
    fi

    if [[ $end_index -gt $#visible_items ]]; then
        end_index=$#visible_items
    fi

    # スクロールインジケーター（上）
    if [[ $start_index -gt 1 ]]; then
        echo -e "${DIM}    ▲ 上にさらに項目があります${NC}"
    else
        echo ""
    fi

    # アイテム表示
    for ((i=$start_index; i<=$end_index && i<=$#visible_items; i++)); do
        local item="${visible_items[$i]}"
        local type="${visible_types[$i]}"

        if [[ $i -eq $cursor_position ]]; then
            # カテゴリーの場合は背景色を変更
            if [[ "$type" == "category" ]]; then
                echo -e "${GREEN}▶${NC} ${BOLD}$item${NC}"
            else
                echo -e "${GREEN}▶${NC} $item"
            fi
        else
            if [[ "$type" == "category" ]]; then
                echo -e "  ${BOLD}$item${NC}"
            else
                echo -e "  $item"
            fi
        fi
    done

    # スクロールインジケーター（下）
    if [[ $end_index -lt $#visible_items ]]; then
        echo -e "${DIM}    ▼ 下にさらに項目があります${NC}"
    else
        echo ""
    fi

    echo -e "${CYAN}─────────────────────────────────────────────────────────────────────${NC}"

    # 選択数の総計
    local total_selected=${#selected_items[@]}
    echo -e "${BOLD}選択済み: ${GREEN}$total_selected${NC} アイテム${NC}"

    # ヒント表示
    local current_type="${visible_types[$cursor_position]}"
    if [[ "$current_type" == "category" ]]; then
        echo -e "${DIM}ヒント: Enter/→ で展開/折りたたみ${NC}"
    elif [[ "$current_type" == "all" ]]; then
        echo -e "${DIM}ヒント: Space でカテゴリー全選択/解除${NC}"
    else
        echo -e "${DIM}ヒント: Space で選択/解除${NC}"
    fi
}

# 設定ファイル生成
generate_config() {
    {
        echo "---"
        echo "# Generated by interactive_setup.zsh"
        echo "# Date: $(date '+%Y-%m-%d %H:%M:%S')"
        echo ""
    } > "$CONFIG_FILE"

    # カテゴリーごとに出力
    for cat in $category_order; do
        local has_items=false
        local items="${category_items[$cat]}"

        # 該当カテゴリーのアイテムを収集
        local output_items=""
        for item in ${(s:|:)items}; do
            local key="${item%%:*}"
            local full_key="${cat}_${key}"
            if [[ -n "${selected_items[$full_key]}" ]]; then
                has_items=true
                output_items="${output_items}  - "

                # カテゴリーに応じた形式で出力
                if [[ "$cat" == "homebrew" ]] || [[ "$cat" == "homebrew_cask" ]]; then
                    output_items="${output_items}name: ${key}\n"
                else
                    output_items="${output_items}${key}\n"
                fi
            fi
        done

        # カテゴリーごとの出力
        if [[ "$has_items" == true ]]; then
            case "$cat" in
                "homebrew")
                    echo "homebrew_packages:" >> "$CONFIG_FILE"
                    ;;
                "homebrew_cask")
                    echo "homebrew_cask_packages:" >> "$CONFIG_FILE"
                    ;;
                "appstore")
                    echo "appstore_apps:" >> "$CONFIG_FILE"
                    ;;
                "dev_env")
                    echo "development_environments:" >> "$CONFIG_FILE"
                    ;;
                "macos_settings")
                    echo "macos_settings:" >> "$CONFIG_FILE"
                    ;;
            esac
            echo -e "$output_items" >> "$CONFIG_FILE"
        fi
    done
}

# キー読み取り
read_key() {
    local key
    read -k 1 key

    if [[ "$key" == $'\e' ]]; then
        read -k 2 -t 0.1 key
        case "$key" in
            '[A') echo 'UP' ;;
            '[B') echo 'DOWN' ;;
            '[C') echo 'RIGHT' ;;
            '[D') echo 'LEFT' ;;
            *) echo 'ESC' ;;
        esac
    else
        echo "$key"
    fi
}

# メイン処理
main() {
    # 初期化
    initialize_items
    update_visible_items

    # 端末設定
    stty -echo 2>/dev/null || true
    tput civis 2>/dev/null || true

    # 終了時処理
    cleanup() {
        tput cnorm 2>/dev/null || true
        stty echo 2>/dev/null || true
        # clearは完全に削除（過去のログを保持）
    }
    trap "cleanup" INT TERM

    while true; do
        display_menu

        local key=$(read_key)
        local current_type="${visible_types[$cursor_position]}"
        local current_key="${visible_keys[$cursor_position]}"

        case "$key" in
            'UP')
                ((cursor_position--))
                [[ $cursor_position -lt 1 ]] && cursor_position=$#visible_items
                ;;
            'DOWN')
                ((cursor_position++))
                [[ $cursor_position -gt $#visible_items ]] && cursor_position=1
                ;;
            'RIGHT'|$'\n'|$'\r')
                if [[ "$current_type" == "category" ]]; then
                    # カテゴリーの展開/折りたたみ
                    if [[ -n "${expanded_categories[$current_key]}" ]]; then
                        unset "expanded_categories[$current_key]"
                    else
                        expanded_categories[$current_key]=1
                    fi
                    update_visible_items
                fi
                ;;
            'LEFT')
                if [[ "$current_type" != "category" ]]; then
                    # カテゴリーを折りたたむ
                    for cat in $category_order; do
                        for ((i=1; i<=$#visible_keys; i++)); do
                            if [[ "${visible_keys[$i]}" == "$current_key" ]] || [[ "${visible_keys[$i]}" == "${cat}_ALL" ]]; then
                                if [[ -n "${expanded_categories[$cat]}" ]]; then
                                    unset "expanded_categories[$cat]"
                                    update_visible_items
                                    # カーソルをカテゴリーに移動
                                    for ((j=1; j<=$#visible_keys; j++)); do
                                        if [[ "${visible_keys[$j]}" == "$cat" ]]; then
                                            cursor_position=$j
                                            break
                                        fi
                                    done
                                fi
                                break 2
                            fi
                        done
                    done
                fi
                ;;
            ' ')
                if [[ "$current_type" == "all" ]]; then
                    # カテゴリー全選択/解除
                    local cat="${current_key%_ALL}"
                    toggle_category_selection "$cat"
                elif [[ "$current_type" == "item" ]]; then
                    # 個別選択/解除
                    if [[ -n "${selected_items[$current_key]}" ]]; then
                        unset "selected_items[$current_key]"
                    else
                        selected_items[$current_key]=1
                    fi
                elif [[ "$current_type" == "category" ]]; then
                    # カテゴリーの展開/折りたたみ（スペースでも動作）
                    if [[ -n "${expanded_categories[$current_key]}" ]]; then
                        unset "expanded_categories[$current_key]"
                    else
                        expanded_categories[$current_key]=1
                    fi
                    update_visible_items
                fi
                ;;
            'a'|'A')
                # 現在のカテゴリー内を全選択
                if [[ "$current_type" != "category" ]]; then
                    for cat in $category_order; do
                        if [[ "$current_key" == "${cat}_"* ]] || [[ "$current_key" == "${cat}_ALL" ]]; then
                            local items="${category_items[$cat]}"
                            for item in ${(s:|:)items}; do
                                local key="${item%%:*}"
                                selected_items[${cat}_${key}]=1
                            done
                            break
                        fi
                    done
                fi
                ;;
            'd'|'D')
                # 現在のカテゴリー内を全解除
                if [[ "$current_type" != "category" ]]; then
                    for cat in $category_order; do
                        if [[ "$current_key" == "${cat}_"* ]] || [[ "$current_key" == "${cat}_ALL" ]]; then
                            local items="${category_items[$cat]}"
                            for item in ${(s:|:)items}; do
                                local key="${item%%:*}"
                                unset "selected_items[${cat}_${key}]"
                            done
                            break
                        fi
                    done
                fi
                ;;
            'c'|'C')
                # 確定
                if [[ ${#selected_items[@]} -eq 0 ]]; then
                    echo ""
                    echo -e "${YELLOW}何も選択されていません。続行しますか？ (y/N)${NC}"
                    read -k 1 confirm
                    if [[ "$confirm" != "y" ]] && [[ "$confirm" != "Y" ]]; then
                        continue
                    fi
                fi

                generate_config

                # clearの代わりに改行を追加して区切りを明確に
                echo ""
                echo ""
                echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
                echo -e "${GREEN}✅ 設定ファイルを生成しました: ${BOLD}$CONFIG_FILE${NC}"
                echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
                echo ""

                # 生成されたファイルの内容を表示
                echo -e "${YELLOW}生成されたファイルの内容:${NC}"
                echo -e "${DIM}ファイルパス: $(pwd)/$CONFIG_FILE${NC}"
                echo ""

                # ファイル内容を色付きで表示
                if [[ -f "$CONFIG_FILE" ]]; then
                    echo -e "${CYAN}--- ファイル内容 開始 ---${NC}"
                    while IFS= read -r line; do
                        if [[ "$line" =~ ^#.* ]]; then
                            # コメント行は薄く表示
                            echo -e "${DIM}$line${NC}"
                        elif [[ "$line" =~ ^[a-z_]+:$ ]]; then
                            # カテゴリーヘッダーは青で表示
                            echo -e "${BLUE}${BOLD}$line${NC}"
                        elif [[ "$line" =~ ^[[:space:]]+-[[:space:]] ]]; then
                            # リストアイテムは緑で表示
                            echo -e "${GREEN}$line${NC}"
                        else
                            echo "$line"
                        fi
                    done < "$CONFIG_FILE"
                    echo -e "${CYAN}--- ファイル内容 終了 ---${NC}"
                    echo ""

                    # ファイルサイズとパーミッション情報
                    local file_size=$(ls -lh "$CONFIG_FILE" | awk '{print $5}')
                    local file_perms=$(ls -l "$CONFIG_FILE" | awk '{print $1}')
                    echo -e "${DIM}ファイルサイズ: $file_size${NC}"
                    echo -e "${DIM}パーミッション: $file_perms${NC}"
                    echo ""
                fi

                # 選択されたパッケージのサマリー
                if [[ ${#selected_items[@]} -gt 0 ]]; then
                    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
                    echo -e "${YELLOW}選択されたパッケージのサマリー:${NC}"
                    echo ""

                    for cat in $category_order; do
                        local cat_count=0
                        local items="${category_items[$cat]}"
                        for item in ${(s:|:)items}; do
                            local key="${item%%:*}"
                            if [[ -n "${selected_items[${cat}_${key}]}" ]]; then
                                ((cat_count++))
                            fi
                        done

                        if [[ $cat_count -gt 0 ]]; then
                            echo -e "${BOLD}${category_names[$cat]}: ${GREEN}$cat_count${NC} アイテム"
                        fi
                    done
                    echo ""
                    echo -e "合計: ${GREEN}${#selected_items[@]}${NC} アイテムが選択されました"
                else
                    echo -e "${YELLOW}警告: パッケージが選択されていません${NC}"
                fi

                echo ""
                echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"

                # リモート実行モードかどうかで表示を変更
                if [ -n "$SETUP_REMOTE_EXEC" ]; then
                    # リモート実行モード
                    echo -e "${GREEN}設定が完了しました。${NC}"
                    echo -e "${DIM}設定ファイル: ${CONFIG_FILE}${NC}"
                else
                    # ローカル実行モード
                    echo -e "${YELLOW}次のステップ:${NC}"
                    echo -e "  ${BOLD}./arm64_mac_setup_dynamic.sh${NC} を実行してインストールを開始します"
                    echo ""
                    echo -e "${DIM}設定ファイルは ${CONFIG_FILE} に保存されました${NC}"
                    echo -e "${DIM}このファイルを編集して手動で調整することも可能です${NC}"
                fi

                # 正常終了時のクリーンアップ
                cleanup success
                exit 0
                ;;
            'q'|'Q')
                echo ""
                echo ""
                echo -e "${RED}キャンセルされました${NC}"
                cleanup
                exit 1
                ;;
            'ESC')
                echo ""
                echo ""
                echo -e "${RED}キャンセルされました${NC}"
                cleanup
                exit 1
                ;;
        esac
    done
}

# 実行
if [[ "${(%):-%N}" == "${0}" ]]; then
    main
fi