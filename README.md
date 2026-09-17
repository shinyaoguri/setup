# macOS Setup

ワンライナーでmacOSの環境構築を自動化

## 使い方

```bash
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/shinyaoguri/setup/main/setup.zsh)"
```

## ローカル実行

```bash
git clone https://github.com/shinyaoguri/setup.git
cd setup
zsh setup.zsh -l
```

## 新しいマシンで

これは自分の環境の作り方で、各リポジトリはこれを前提にしない。秘密の読み方 (1Password + Keychain キャッシュ) も SSH の鍵 (Secretive) もこのマシンの中の選択で、リポジトリ側は `MOKUME_APP_PRIVATE_KEY_CMD` のような受け口だけを持つ。

GUI 操作が要って自動化できない手順があるので、次の順に進める。詳細は各項目のリンク先が正本。

1. **App Store にサインインしておく** — mas は CLI からサインインできない
2. **setup を流す** — 上の「使い方」
3. **Secretive で鍵を作り、GitHub に登録する** — 手順は [`tasks/ssh.yml`](tasks/ssh.yml) の冒頭。済んだら `ansible-playbook playbook_sillicon_mac.yml --tags ssh,git`
4. **1Password にサインインし、設定 > 開発者 の「1Password CLI と連携」を有効にする**
5. **秘密のキャッシュを温める** — `secret-read --warm`。セットアップの最後にも試すが、4 が済んでいないと失敗する。温めておかないと、無人セッションが初回の読み出しで 1Password の承認を待って止まる。何をキャッシュしてよいかは [`secret-cache-allowlist`](secret-cache-allowlist)
6. **playbook が「Claude デスクトップアプリを再起動」と言ったら再起動する** — アプリは起動時の PATH を持ち続けるので、セットアップ前から開いていたアプリのセッションには `secret-read` が見えない ([#154](https://github.com/shinyaoguri/setup/issues/154))
7. **一度ログアウトして入り直す** — キーリピート・トラックパッド・日本語入力の句読点は `defaults` に書いても動いているプロセスには届かない。効いていないように見えても再ログイン後に効く (Dock だけは playbook が入れ直すので即時)
8. **確かめる** — `secret-read --check` で全件がキャッシュ済み、`ssh-add -l` で Secretive の鍵が見える。Claude Code のセッションで `command -v secret-read` が通る
