#!/bin/zsh

# MIT License
# Copyright (c) 2022 Shinya Oguri
# https://github.com/shinyaoguri/setup

######## 
# Xcode
########
echo -e "-----\nCheck Xcode"
if type "xcode-select" >/dev/null 2>&1; then
  echo -e "✅ Xcode already exist"
else
  echo -e "🙅 Xcode was not exist\n>>> Please install Xcode from AppStore."
  open "https://apps.apple.com/jp/app/xcode/id497799835"
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
  echo -e "🙅 Homebrew was not exist\nPlease install Homebrew"
  open "https://brew.sh/index_ja"
  exit
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


echo -e "-----\nAnsible Deploy"
cd ~

#メニュー
options[0]="min"
options[1]="max"

#各メニューごとに挙動を記述する
function DOIT {
    if [[ ${choices[0]} ]]; then
        echo "Option 1 selected"
    fi

    if [[ ${choices[1]} ]]; then
        echo "Option 2 selected"
    fi
}

clear

#現在フォーカスしているメニュー
current_line=0

#メニュー一覧を表示
function MENU {
    echo "移動:[↑]or[↓], 選択:[SPACE], 決定:[ENTER]"
    for NUM in ${!options[@]}; do
        if [ $NUM -eq $current_line ]; then
            echo -n ">"
        else
            echo -n " "
        fi
        echo "[${choices[NUM]:- }]"":${options[NUM]}"
    done
}


#メニュー選択のループ
function SELECT_LOOP {
    while true; do
        while MENU && IFS= read -r -n1 -s SELECTION && [[ -n "$SELECTION" ]]; do
            if [[ $SELECTION == $'\x1b' ]]; then
                read -r -n2 -s rest
                SELECTION+="$rest"
            fi  
            clear

            case $SELECTION in
                $'\x1b\x5b\x41') #up arrow
                    if [[ $current_line -ne 0 ]]; then
                        current_line=$(( current_line - 1 ))
                    else
                        current_line=$(( ${#options[@]}-1 ))
                    fi
                    ;;
                $'\x1b\x5b\x42') #down arrow
                    if [[ $current_line -ne $(( ${#options[@]}-1 )) ]]; then
                        current_line=$(( current_line + 1 ))
                    else
                        current_line=0
                    fi
                    ;;
                $'\x20') #space
                    if [[ "${choices[current_line]}" == "+" ]]; then
                        choices[current_line]=""
                    else
                        choices[current_line]="+"
                    fi
                    ;;
            esac
        done

        read -p "選べた? [Y/n]" Answer
        case $Answer in
            '' | [Yy]* )
                break;
                ;;
            [Nn]* )
                ;;
            * )
                echo "YES or NOで答えてね"
                ;;
        esac
        clear
    done
}

SELECT_LOOP
DOIT

curl -O -sfSL https://raw.githubusercontent.com/shinyaoguri/setup/main/ansible_arm64_mac.yml

if [ -f ~/ansible_arm64_mac.yml ]; then
  ansible-playbook ansible_arm64_mac.yml --ask-become-pass
  rm ansible_arm64_mac.yml
else
  echo -e "🙅 ansible-playbook was not downloaded"
fi
