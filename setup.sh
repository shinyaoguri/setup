#!/bin/bash

echo "-----\nOSの判定"
if [ "$(uname)" == 'Darwin' ] && [ "$(uname -m)" == 'x86_64' ]; then
  echo '- x86_64 Mac'
elif [ "$(uname)" == 'Darwin' ] && [ "$(uname -m)" == 'arm64' ]; then
  echo '- arm64 Mac'
  bash -c "$(curl https://raw.githubusercontent.com/shinyaoguri/setup/main/arm64_mac_setup.sh)"
elif [ "$(expr substr $(uname -s) 1 5)" == 'Linux' ]; then
  OS='Linux'
  echo '- Linux'
elif [ "$(expr substr $(uname -s) 1 10)" == 'MINGW32_NT' ]; then                                                                                           
  OS='Cygwin'
  echo '- Windows'
else
  echo "Your platform ($(uname -a)) is not supported."
  exit 1
fi
