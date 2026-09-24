# macOS Setup

ワンライナーでmacOSの環境構築を自動化

## 使い方

```bash
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/shinyaoguri/setup/main/setup.zsh)"
```

無条件に入るのは**土台だけ** (git / mas / fzf / gum / gh と、1Password・Secretive・Claude Code・フォント)。
それ以外は導入状況を報告したうえで、**入れるものをその場で選ぶ** — エディタや Office、
スキャナのソフト、App Store のアプリは「要るときに入れれば自分で更新される」もので、
新しいマシンを立てるたびに待つ理由が無い ([#191](https://github.com/shinyaoguri/setup/issues/191))。

選択は fzf のチェックボックス (Tab で選び、Enter で確定、ESC で何も入れずに続行)。
**Tab で選んでいなければ、Enter を押しても何も入らない** — カーソルを合わせただけの
ものが入らないようにしてある ([#267](https://github.com/shinyaoguri/setup/issues/267))。
右のペインに `brew info` が出るので、何なのか分からないものはそこで確かめられる。

選択を出したくないときはフラグで決め打てる。**端末が無いとき (CI・パイプ経由) は
自動で「何も入れない」に倒れる**ので、無人実行でも止まらない。

```bash
zsh setup.zsh --with-optional   # 選択を出さず全部入れる (従来と同じ)
zsh setup.zsh --no-optional     # 選択を出さず土台だけ
```

後から入れたくなったら `brew install --cask <名前>` でも、`zsh setup.zsh -l` を流し直して
選び直してもよい (既に入っているものは選択肢に出ない)。

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
2. **Xcode を入れているなら、一度開いてライセンスに同意する** — 同意するまで `git` を含む開発ツールが終了コード 69 で落ちる。新品の Mac は一度も開いていないので必ず踏む。`sudo xcodebuild -license accept` でもよい。setup も Step 1 で実測して同意画面を出すが、先に済ませておけば途中で止まらない ([#266](https://github.com/shinyaoguri/setup/issues/266))
3. **setup を流す** — 上の「使い方」。途中で「何を入れるか」を聞かれる (選ばなかったものは後から入れられる)
4. **Secretive で鍵を作り、GitHub に登録する** — 手順は [`tasks/ssh.yml`](tasks/ssh.yml) の冒頭。済んだら `ansible-playbook playbook_sillicon_mac.yml --tags ssh,git`。3 の時点では鍵がまだ無いので、setup は**コミット署名の設定だけを飛ばして**最後まで進む (他の設定は適用済み)。ここで流し直すと署名が入る — 名指しで流したこの実行では、鍵が無いか要件 (鍵タイプ・承認要求の有無) を満たさなければ失敗として止まる ([#196](https://github.com/shinyaoguri/setup/issues/196), [#273](https://github.com/shinyaoguri/setup/issues/273))。4 つのうちどれが欠けているかは `ssh-key-check` が名指しで言う。**鍵がまだ無いうちに打てば、手順そのものが出る** ([#278](https://github.com/shinyaoguri/setup/issues/278))
5. **1Password にサインインし、設定 > 開発者 の「1Password CLI と連携」を有効にする**
   - **無人セッションに読ませる項目へ、タグ `secret-read/<役割>` を付ける** — 役割の一覧と、それぞれキャッシュしてよい理由は [`secret-cache-allowlist`](secret-cache-allowlist) にある (`gyazo-token` / `cosense-pat` / `mokume-app-key`)。保管庫や項目の名前は自由で、タグを付けた項目がその役割に充たる (1 つの役割につき 1 件だけ)。値は項目の `credential` フィールドから、無ければただ 1 つの添付ファイルから読む。使わない役割は付けなくてよい ([#289](https://github.com/shinyaoguri/setup/issues/289))
   - **Cosense を使うなら、エージェント専用の Personal Access Token を発行して `secret-read/cosense-pat` を付ける** — `cosense` は setup の [`bin/cosense`](bin/cosense) が包み、起動のたびに `COSENSE_PAT` として渡す。`~/.cosense/settings.json` に token を置く必要は無い。1Password に置かないなら `COSENSE_PAT` を空で設定すると差し込みが止まる
6. **秘密のキャッシュを温める** — `secret-read --warm`。セットアップの最後にも試すが、5 が済んでいないと失敗する。温めておかないと、無人セッションが初回の読み出しで 1Password の承認を待って止まる。何をキャッシュしてよいかは [`secret-cache-allowlist`](secret-cache-allowlist)。タグの付いていない役割は「取れなかった」と出る (使わない役割なら放っておいてよい)
7. **Gyazo を手でインストールする** (3 で選んだ場合) — `gyazo` cask は手動インストーラ (artifact が `installer: manual`) で、brew は `.pkg` を Caskroom に置くだけ。`/opt/homebrew/Caskroom/gyazo/*/Gyazo-*.pkg` を開いてインストールする。**`brew list --cask gyazo` は .pkg があるだけで成功を返す**ので、セットアップは「導入済み」と報告してしまう。済ませないと Gyazo MCP が無音で未登録になり、[gyazo-capture スキル](https://github.com/shinyaoguri/claude-plugins) が使えない。済んだら `ansible-playbook playbook_sillicon_mac.yml --tags claude`
8. **playbook が「Claude デスクトップアプリを再起動」と言ったら再起動する** — アプリは起動時の PATH を持ち続けるので、セットアップ前から開いていたアプリのセッションには `secret-read` が見えず、`cosense` もラッパーを通らない ([#154](https://github.com/shinyaoguri/setup/issues/154))
9. **一度ログアウトして入り直す** — キーリピート・トラックパッド・日本語入力の句読点・修飾キーの入れ替え (Caps Lock → Control / fn 無効) は `defaults` に書いても動いているプロセスには届かない。効いていないように見えても再ログイン後に効く (Dock だけは playbook が入れ直すので即時)
10. **マシン固有のシェル設定は `~/.zshrc.local` へ置く** — `~/.zshrc` はこのリポジトリへの symlink なので、インストーラ (Unity CLI・grok など) が `~/.zshrc` へ追記した行はリポジトリの変更として現れる。`git -C ~/.setup diff zshrc` に出たら `~/.zshrc.local` へ移して元へ戻す。zshrc は最後にそれを読む ([#198](https://github.com/shinyaoguri/setup/issues/198))
11. **確かめる** — `secret-read --check` で使う役割がキャッシュ済みで、充てた項目の名前が意図どおり、`ssh-key-check` が全部 ok (`ssh-add -l` は 4 つの完了条件のうち agent の常駐しか見ていない)。Claude Code のセッションで `command -v secret-read` が通り、`command -v cosense` が setup の `bin/cosense` を指す
