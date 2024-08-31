# one command setting my pc

[コマンド一発でAnsible含めて自動でMacの環境構築する](https://zenn.dev/shinyaoguri/articles/5caeeeea21b0c2)

## Getting Started

```
curl -L setup.shinyaoguri.com | sh -s
```
or
```
curl https://raw.githubusercontent.com/shinyaoguri/setup/main/setup.sh | sh -s
```

`setup.shinyaoguri.com` is redirect `https://raw.githubusercontent.com/shinyaoguri/setup/main/setup.sh` using CloudFlare Workers

## Manual Setup

```
$ ansible-playbook ansible_arm64_mac.yml
```

## どうしても手動で設定する内容
- ターミナルでシェルのフォントを
