#!/usr/bin/env python3
"""tasks/*.yml のうち、判定を間違えると黙って何もしなくなるものを固定する。

ansible のタスクは「実行されなかった」と「実行して変更が無かった」が結果として
同じ緑に見える。when の条件が永久に偽になっていても気付けないので、条件の根拠に
している外部コマンドの挙動ごと検証する (issue #172)。

    python3 claude/tests/ansible_tasks_test.py
"""

import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env
from ssh_key_check_test import (
    ECDSA_KEY,
    FAKE_GH,
    FAKE_SSH_ADD,
    GITHUB_KEY,
    MLDSA_KEY,
    bind_agent_socket,
    install_fake,
)

TASKS = Path(__file__).resolve().parent.parent.parent / "tasks"

ABSENT_PKG = "com.example.definitely.not.installed"

# 配備先の checkout の経路。クローン (fetch) は HTTPS、push だけ SSH (issue #272)
HTTPS_URL = "https://github.com/shinyaoguri/setup.git"
SSH_URL = "git@github.com:shinyaoguri/setup.git"


def without_comments(path):
    """コメント行を落とした本文。

    コメントは「なぜその形にしたか」を書くのに**悪い形や機構の名前をそのまま引用する**
    ので、素の文字列一致で検査するとコメントに当たって常に緑になる (実際に 3 回踏んだ)。
    タスクの中身を見る検査は必ずここを通す。
    """
    return "\n".join(
        line for line in path.read_text().split("\n")
        if not line.lstrip().startswith("#")
    )


class RosettaDetectionTest(unittest.TestCase):
    """Rosetta の存在チェック。誤ると新しいマシンに Rosetta が入らない。"""

    def setUp(self):
        self.body = without_comments(TASKS / "rosetta.yml")

    def test_uses_a_form_that_reports_absence(self):
        """`pkgutil --pkgs <id>` はスペース区切りだと引数を無視して必ず rc=0 を返す。

        この形で書くと when が永久に偽になり、インストールが一度も走らないまま
        タスクは緑で通る。
        """
        self.assertNotRegex(
            self.body, r"pkgutil\s+--pkgs\s+[^=\s]",
            "pkgutil --pkgs <id> はスペース区切りだと常に rc=0 (存在確認にならない)",
        )
        self.assertRegex(self.body, r"pkgutil\s+--pkg-info\s+com\.apple\.pkg\.")

    def test_install_is_guarded_by_the_check(self):
        """チェック結果を実際に使っていること (register だけして見ない形を防ぐ)。"""
        self.assertIn("rosetta_check.rc != 0", self.body)

    @unittest.skipUnless(shutil.which("pkgutil"), "pkgutil が無い環境")
    def test_pkgutil_forms_behave_as_assumed(self):
        """前提にしている pkgutil の挙動そのもの。OS 側が変われば赤くなる。"""
        spaced = subprocess.run(
            ["pkgutil", "--pkgs", ABSENT_PKG], capture_output=True, text=True
        )
        self.assertEqual(
            spaced.returncode, 0,
            "--pkgs <id> が存在確認として働くようになった (タスクを見直せる)",
        )

        info = subprocess.run(
            ["pkgutil", "--pkg-info", ABSENT_PKG], capture_output=True, text=True
        )
        self.assertNotEqual(
            info.returncode, 0, "--pkg-info が不在を rc で伝えなくなった"
        )


class FontIdempotencyTest(unittest.TestCase):
    """フォントの導入が毎回 changed を返さないこと (issue #174)。

    homebrew_cask は `"--force" not in install_options and _current_cask_is_installed()`
    で導入済みを判定するので、force があると早期 return を飛ばして毎回入れ直す。
    changed の数は provisioning-preflight.sh が予告に使う指標なので、常にノイズが
    乗ると本当に変わるものが埋もれる。
    """

    def setUp(self):
        self.body = without_comments(TASKS / "fonts.yml")

    def test_does_not_force_reinstall(self):
        self.assertNotIn(
            "force", self.body,
            "install_options: force は導入済みの判定を飛ばし、毎回 changed を返す",
        )

    def test_is_not_greedy(self):
        """version: latest の cask は greedy を付けると常に outdated になる。

        force を外しても greedy を付ければ同じ状態に戻るので、両方を固定する。
        """
        self.assertNotIn("greedy", self.body)


class OverwriteBackupTest(unittest.TestCase):
    """人の設定を上書きするタスクは、退避を取ってから上書きする (issue #175)。

    ~/.ssh/config は ansible が所有する設計だが、既にファイルがあるマシン
    (バックアップから復元した環境・setup より先に手で設定した環境) では、その中身を
    黙って捨てることになる。
    """

    def test_ssh_config_is_backed_up_before_overwrite(self):
        body = without_comments(TASKS / "ssh.yml")
        self.assertIn(
            "backup: true", body,
            "~/.ssh/config を退避なしで全置換している",
        )

    def test_overwriting_tasks_declare_a_backup(self):
        """dest が既存ファイルを差しうる copy は、退避の宣言を持つこと。

        content: を持つ copy = ファイルを丸ごと書き出す形。symlink を張る file や
        src: を持つ copy (退避そのもの) は対象外。
        """
        missing = []
        for path in sorted(TASKS.glob("*.yml")):
            body = without_comments(path)
            for block in re.split(r"\n(?=- name:)", body):
                if "ansible.builtin.copy:" not in block or "content: |" not in block:
                    continue
                if "backup: true" not in block:
                    missing.append(path.name)
        self.assertEqual(
            missing, [], "内容を丸ごと書き出す copy に backup の宣言が無い"
        )


class DefaultsTakeEffectTest(unittest.TestCase):
    """`defaults write` は保存値を書くだけで、動いているプロセスには届かない。

    実測 (issue #176): Dock の autohide を 1 → 0 にしても、System Events が返す
    生の状態は true のままだった。反映には Dock の入れ直しが要る。
    """

    def setUp(self):
        self.body = (TASKS / "macos.yml").read_text()
        self.tasks_only = without_comments(TASKS / "macos.yml")

    def test_dock_is_restarted_after_the_setting_changes(self):
        """コメントを落とした本文で見る。

        以前は素の本文を見ていた。tasks/macos.yml のコメントは `killall` も
        `dock_autohide.changed` も引用しているので、**タスクを消しても緑のまま**だった
        (このファイルの冒頭が「必ず without_comments を通す」と定めている、その当の穴。#218)。
        """
        self.assertIn("killall", self.tasks_only, "Dock を入れ直していない")
        self.assertIn(
            "when: dock_autohide.changed", self.tasks_only,
            "Dock の再起動が条件付きになっていない",
        )

    def test_the_comments_alone_would_have_satisfied_the_old_check(self):
        """上の検査が意味を持つことの確認 — コメントだけでも同じ語が揃っている。"""
        comments = "\n".join(
            line for line in self.body.split("\n") if line.lstrip().startswith("#")
        )
        self.assertIn("killall", comments)

    def test_the_restart_is_not_unconditional(self):
        """無条件に打つと毎回 changed を返し、#174 で直した非冪等を再発させる。"""
        for block in re.split(r"\n(?=- name:)", self.tasks_only):
            if "killall" not in block:
                continue
            self.assertRegex(
                block, r"when:\s*\S+\.changed",
                "プロセスを入れ直すタスクに changed の条件が無い",
            )

    def test_signal_target_is_scoped_to_the_user(self):
        """送り先を名前だけで決めない (claude/signal-guard.py と同じ理由)。"""
        self.assertRegex(
            self.tasks_only, r"killall\s+-u\s",
            "killall が全ユーザーのプロセスを対象にしている",
        )

    def test_relogin_is_documented_where_it_cannot_be_fixed(self):
        """直せないものは「効かない」と正直に書く。

        反映機構を確認できていない設定について、プロセスを殺す根拠は無い。
        """
        setup = (TASKS.parent / "sillicon_mac_setup.zsh").read_text()
        self.assertIn("ログアウト", setup, "再ログインの案内が完了メッセージに無い")
        readme = (TASKS.parent / "README.md").read_text()
        self.assertIn("ログアウト", readme, "再ログインの案内が README に無い")


class DeclaredDependencyTest(unittest.TestCase):
    """playbook 自身が要求するものは vars/packages.yml に宣言する (issue #177)。

    宣言が無いと、新しいマシンでそのタスクが実行ファイル不在で失敗するか、
    受け手が居ないまま黙って空振りする。
    """

    def setUp(self):
        self.packages = without_comments(TASKS.parent / "vars" / "packages.yml")
        self.claude_task = without_comments(TASKS / "claude.yml")

    def test_claude_code_is_declared(self):
        """tasks/claude.yml が `claude mcp add` を打つ。

        required 側にあることは bootstrap_selection_test が見る (optional へ落ちると
        選ばなかったマシンで Step 6 が失敗するため)。
        """
        self.assertIn("claude mcp add", self.claude_task)
        self.assertIn("- claude-code", self.packages)

    def required_formulae(self):
        block = self.packages.split("homebrew_packages_required:")[1].split("\n\n")[0]
        return re.findall(r"^\s+-\s+(\S+)", block, re.M)

    def test_gh_is_declared_as_required(self):
        """配っているフックが `gh` を打つ (issue #200)。

        plan-record.sh は PR / Issue へプランを投稿し、gh-comment-guard.sh と term-guard.sh は
        gh のコマンドを検査する。グローバル CLAUDE.md の運用 (Issue・PR を置き場にする) と
        README 手順 3 の `gh auth refresh` も gh が前提。macOS は gh を同梱していないので、
        宣言が無いと新しいマシンで配った設定がそのまま動かない。
        """
        callers = [
            path.name for path in sorted((TASKS.parent / "claude").glob("*.sh"))
            if re.search(r"(^|[\s(|;&])gh\s+(pr|issue|api|repo)\b", without_comments(path), re.M)
        ]
        self.assertTrue(callers, "gh を打つフックが見つからない (この検査の前提が変わった)")
        self.assertIn("gh", self.required_formulae())

    def test_direnv_is_declared(self):
        """zshrc が direnv の hook を読む。無くても壊れない (存在チェック付き) ので optional。"""
        self.assertIn("direnv hook zsh", (TASKS.parent / "zshrc").read_text())
        self.assertIn("- direnv", self.packages)

    def test_mas_is_declared(self):
        """sillicon_mac_setup.zsh が App Store の導入に使う。"""
        self.assertIn("- mas", self.packages)

    def test_runcat_is_declared(self):
        """tasks/claude.yml が seed する Custom Metrics カードの受け手。

        必須ではない (seed はカードの JSON を書くだけで、RunCat が入っていなくても
        失敗しない。issue #191) が、台帳からは消さない。
        """
        self.assertIn("runcat-metrics.py --seed", self.claude_task)
        self.assertIn("RunCatNeo", self.packages)

    def test_gyazo_manual_installer_is_surfaced(self):
        """gyazo cask は installer: manual で、brew は .pkg を置くだけ。

        `brew list --cask gyazo` は .pkg があるだけで成功を返すので、黙って
        スキップすると新しいマシンで Gyazo MCP が無音で未登録になる。
        """
        claude = self.claude_task
        self.assertIn(
            "brew list --cask gyazo", claude,
            "cask の導入有無を見ていない (バイナリ不在との切り分けができない)",
        )
        self.assertIn("gyazo_cask_installed", claude)
        readme = (TASKS.parent / "README.md").read_text()
        self.assertIn("Gyazo を手でインストールする", readme)


class GitTaskTestCase(unittest.TestCase):
    """tasks/git.yml を、HOME を一時ディレクトリへ向けて**実際に流す**土台。

    when の条件を文字列で見ても、実際に止まるか止まらないかは分からない (git config
    --global も鍵の探索も HOME の下で完結するので、本当に流せる)。ansible が無い環境
    では skip — CI で流す件は issue #220。

    署名の可否は bin/ssh-key-check が決めるようになった (issue #273) ので、その検査が
    見るもの — SecretAgent の socket・ssh-add・gh — も一時ディレクトリの中に作る。
    偽物の定義は ssh_key_check_test と共有する (2 箇所に持つと片方だけ直る)。
    """

    PLAYBOOK = TASKS.parent / "playbook_sillicon_mac.yml"

    def setUp(self):
        if shutil.which("ansible-playbook") is None:
            self.skipTest("ansible が無い環境")
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.home = Path(self.workdir.name)

        self.secretive = self.home / "Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data"
        (self.secretive / "PublicKeys").mkdir(parents=True)
        self.addCleanup(bind_agent_socket(self.secretive).close)

        self.fake_bin = self.home / "fake-bin"
        self.fake_bin.mkdir()
        install_fake(self.fake_bin / "ssh-add", FAKE_SSH_ADD)
        install_fake(self.fake_bin / "gh", FAKE_GH)
        self.set_github_keys(auth=[GITHUB_KEY], signing=[GITHUB_KEY])

    def other_tags(self):
        tags = re.findall(r"^\s*tags:\s*(\S+)", self.PLAYBOOK.read_text(), re.M)
        self.assertIn("git", tags)
        return [t for t in tags if t != "git"]

    def run_git_tasks(self, explicit, agent="ok", sign="ok", gh_auth="ok", gh_signing="ok"):
        """explicit=True は `--tags git` (人が名指しで流した)。False は全体実行の再現で、
        git 以外を --skip-tags で外す (重い brew / cask を流さないため)。どちらの形でも
        走るのは tasks/git.yml だけだが、ansible_run_tags は前者が ['git']、後者が ['all']。
        """
        selection = ["--tags", "git"] if explicit else ["--skip-tags", ",".join(self.other_tags())]
        env = clean_env(HOME=str(self.home), XDG_CONFIG_HOME=None, ANSIBLE_NOCOLOR="1")
        # 偽物を先に引かせる。ansible 自身は本物の PATH から引く必要があるので足すだけ
        env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        env["FAKE_SSH_ADD_LOG"] = str(self.home / "ssh-add.log")
        env["FAKE_GH_LOG"] = str(self.home / "gh.log")
        env["FAKE_GH_AUTH_FILE"] = str(self.github_auth)
        env["FAKE_GH_SIGNING_FILE"] = str(self.github_signing)
        env["SSH_KEY_CHECK_LIMIT"] = "2"
        env["SSH_KEY_CHECK_GH_LIMIT"] = "2"
        env["FAKE_SSH_ADD_LIST"] = agent
        env["FAKE_SSH_ADD_SIGN"] = sign
        env["FAKE_GH_AUTH"] = gh_auth
        env["FAKE_GH_SIGNING"] = gh_signing
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(self.PLAYBOOK), *selection],
            capture_output=True, text=True, timeout=300, cwd=self.home, env=env,
        )

    def git_config(self, key):
        result = subprocess.run(
            ["git", "config", "--file", str(self.home / ".gitconfig"), "--get", key],
            capture_output=True, text=True, env=clean_env(HOME=str(self.home)),
        )
        return result.stdout.strip()

    def place_key(self, key=ECDSA_KEY):
        (self.secretive / "PublicKeys" / "test.pub").write_text(key + "\n")

    def place_checkout(self):
        """配備先の checkout を偽の HOME に作る。origin は本番と同じ HTTPS。"""
        checkout = self.home / ".setup"
        env = clean_env(HOME=str(self.home))
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin", HTTPS_URL],
            check=True, env=env,
        )
        return checkout

    def repo_config(self, checkout, key):
        result = subprocess.run(
            ["git", "-C", str(checkout), "config", "--get", key],
            capture_output=True, text=True, env=clean_env(HOME=str(self.home)),
        )
        return result.stdout.strip()

    def set_github_keys(self, auth=None, signing=None):
        self.github_auth = self.home / "github-auth-keys"
        self.github_signing = self.home / "github-signing-keys"
        self.github_auth.write_text("".join(f"{k}\n" for k in (auth or [])))
        self.github_signing.write_text("".join(f"{k}\n" for k in (signing or [])))


class FirstRunWithoutKeyTest(GitTaskTestCase):
    """新しいマシンの初回実行が、Secretive の鍵が無いことで止まらない (issue #196)。

    Secretive は同じ実行の中で入ったばかりで、鍵を作るのは GUI 操作 — つまり初回は
    **必ず**鍵が無い。tasks/git.yml がそこで fail していたため、playbook は順序で後ろに
    ある fonts / terminal / zshrc / claude / fnm を一切適用せず、bootstrap も set -e で
    Step 7 と最後の案内へ届かなかった。「鍵がまだ無い」は途中経過であって失敗ではない。
    """

    def test_full_run_without_a_key_does_not_stop(self):
        """全体実行では止まらず、鍵と無関係な設定は入り、署名だけが入らない。"""
        result = self.run_git_tasks(explicit=False)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertEqual(self.git_config("user.name"), "Shinya Oguri")
        self.assertTrue(self.git_config("alias.gone"), "鍵と無関係な alias まで skip された")
        # 鍵が無いのに署名を有効にすると、以後のコミットがすべて失敗する
        self.assertEqual(self.git_config("commit.gpgsign"), "")
        self.assertEqual(self.git_config("user.signingkey"), "")
        # 何が残っているかと、済ませた後に打つコマンドを出していること
        self.assertIn("--tags ssh,git", result.stdout)

    def test_explicit_git_tag_without_a_key_still_fails(self):
        """名指しで流したのに鍵が無いなら、それは失敗 (README 手順 3 の「済んだら」の後)。

        ここまで skip にすると、署名が未適用のまま緑で終わる — 1Password 版が
        そうなっていて、tasks/git.yml が fail を置いた元々の理由。
        """
        result = self.run_git_tasks(explicit=True)
        self.assertNotEqual(result.returncode, 0, "鍵が無いのに成功として終わった")
        self.assertEqual(self.git_config("commit.gpgsign"), "")

    def test_signing_is_configured_once_the_key_exists(self):
        """鍵があれば署名まで入る (skip の条件が永久に真になっていないこと)。"""
        self.place_key()
        result = self.run_git_tasks(explicit=False)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertEqual(self.git_config("commit.gpgsign"), "true")
        self.assertEqual(self.git_config("gpg.format"), "ssh")
        signing_key = self.home / ".ssh/git_signing_key.pub"
        self.assertEqual(signing_key.read_text().strip(), ECDSA_KEY)
        self.assertIn(ECDSA_KEY, (self.home / ".config/git/allowed_signers").read_text())


class UnusableKeyTest(GitTaskTestCase):
    """鍵は在るが要件を満たさないとき (issue #273)。

    **「鍵が無い」と同じ扱いにする。** 要件を満たさない鍵で commit.gpgsign=true にすると
    以後のコミットが全部失敗し、症状 (「コミットできない」) は原因 (「鍵タイプが ML-DSA」)
    から最も遠いところに出る。これは #196 で直した「鍵が無いだけで止まる」の裏返しで、
    方針は同じ — **未完成の状態で playbook を止めない。ただし未完成のまま緑で終わらせない**。
    """

    def test_broken_key_does_not_stop_the_full_run(self):
        """全体実行は止まらず、鍵と無関係な設定は入り、署名だけが入らない。"""
        self.place_key(MLDSA_KEY)
        result = self.run_git_tasks(explicit=False)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertTrue(self.git_config("alias.gone"), "鍵と無関係な alias まで skip された")
        self.assertEqual(self.git_config("commit.gpgsign"), "")
        self.assertEqual(self.git_config("user.signingkey"), "")

    def test_explicit_git_tag_with_a_broken_key_fails(self):
        """名指しで流したのに使えない鍵なら、それは失敗 (README 手順 3 の「済んだら」の後)。"""
        self.place_key(MLDSA_KEY)
        result = self.run_git_tasks(explicit=True)
        self.assertNotEqual(result.returncode, 0, "使えない鍵なのに成功として終わった")
        self.assertEqual(self.git_config("commit.gpgsign"), "")

    def test_the_message_says_which_requirement_is_missing(self):
        """「鍵が無い」しか言えない固定文では、鍵が在って壊れている場合に役に立たない。"""
        self.place_key(MLDSA_KEY)
        result = self.run_git_tasks(explicit=False)
        self.assertIn("ssh-mldsa65", result.stdout, "何が欠けているかを言っていない")

    def test_a_stopped_agent_also_skips_signing(self):
        """agent が応えないなら「署名できる」と証明できていない。鍵の形だけでは通さない。"""
        self.place_key()
        result = self.run_git_tasks(explicit=False, agent="noagent")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(self.git_config("commit.gpgsign"), "")


class GithubRegistrationTest(GitTaskTestCase):
    """GitHub への登録は、ローカルで署名できることの前提ではない (issue #273)。"""

    def test_signing_is_configured_even_when_the_key_is_not_on_github(self):
        self.place_key()
        self.set_github_keys(auth=[], signing=[])
        self.run_git_tasks(explicit=False)
        self.assertEqual(self.git_config("commit.gpgsign"), "true")

    def test_explicit_git_tag_fails_when_the_key_is_not_on_github(self):
        """名指しで流したなら、登録まで済んでいるはず (README 手順 3)。"""
        self.place_key()
        self.set_github_keys(auth=[], signing=[])
        result = self.run_git_tasks(explicit=True)
        self.assertNotEqual(result.returncode, 0, "未登録なのに成功として終わった")
        # ローカルの要件は満たしているので、署名の設定自体は入っている
        self.assertEqual(self.git_config("commit.gpgsign"), "true")

    def test_an_unreachable_github_does_not_break_the_run(self):
        """gh のスコープ不足・オフラインは「判定不能」で、マシンの状態ではない。"""
        self.place_key()
        result = self.run_git_tasks(explicit=True, gh_signing="scope")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertEqual(self.git_config("commit.gpgsign"), "true")
        self.assertIn("admin:ssh_signing_key", result.stdout, "打つ手を出していない")


class SetupCheckoutPushUrlTest(GitTaskTestCase):
    """配備先の checkout の push だけ SSH へ寄せる (issue #272)。"""

    def test_the_setup_checkout_pushes_over_ssh(self):
        """配備先の push だけ SSH へ寄せる (issue #272)。

        `~/.setup` は配備先と開発用の checkout を兼ねている (issue #222) ので、
        ここで push するのは例外ではなく日常。クローンは HTTPS なので push が OAuth
        トークン経由になり、`workflow` スコープを持たないトークンでは `.github/workflows`
        に触れていないブランチまで弾かれる。fetch の経路は初回クローンが成立している
        HTTPS のまま残す。
        """
        checkout = self.place_checkout()
        self.place_key()
        result = self.run_git_tasks(explicit=False)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertEqual(self.repo_config(checkout, "remote.origin.pushurl"), SSH_URL)
        self.assertEqual(self.repo_config(checkout, "remote.origin.url"), HTTPS_URL)

    def test_a_missing_checkout_is_not_an_error(self):
        """配備先がそこに無い環境でも止まらない (別の場所から流したとき)。"""
        self.place_key()
        result = self.run_git_tasks(explicit=False)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertFalse((self.home / ".setup").exists(), "無いはずの配備先が作られた")


class SigningGateShapeTest(unittest.TestCase):
    """署名設定のゲートの形。**ansible が無い環境 (CI) でも見られる**検査。

    上の 3 クラスは実際に playbook を流すので CI では skip される (issue #220)。
    ゲートそのものが外れたことだけは、形を見るだけでも拾えるようにしておく。
    """

    def setUp(self):
        self.body = without_comments(TASKS / "git.yml")

    def test_signing_is_gated_on_the_checker(self):
        """検査結果を register だけして見ない形を防ぐ。"""
        self.assertIn("git_signing_key_usable", self.body)
        self.assertIn("ssh_key_local.rc == 0", self.body)
        self.assertNotIn("git_signing_key_available", self.body)

    def test_the_checker_is_called_by_absolute_path(self):
        """bin/ を PATH へ通すのは zshenv で、symlink する tasks/zshrc.yml は git より後ろ。

        PATH 頼みで呼ぶと、新しいマシンの初回実行でだけ検査器が見つからない。
        """
        self.assertIn("{{ playbook_dir }}/bin/ssh-key-check", self.body)

    def test_the_signing_key_comes_from_the_checker(self):
        """検査した鍵と書き出す鍵を同じにする (`cat *.pub | head -1` へ戻らないこと)。"""
        self.assertIn("--print-key", self.body)
        self.assertNotIn("head -1", self.body)

    def test_the_checks_are_not_counted_as_changes(self):
        """読むだけのタスクが毎回 changed と出ると、冪等性を目で追う運用が壊れる。"""
        for block in self.body.split("\n- name:"):
            if "ssh-key-check" in block:
                self.assertIn("changed_when: false", block, block)


class NodeVersionTest(unittest.TestCase):
    """新しいマシンの既定の Node.js は LTS にする (issue #223)。

    `fnm ls-remote | tail -n 1` は**その時点の最新版**で、奇数メジャー (Current) のことが
    ある。立てた時期によって既定が LTS だったり半年で EOL になる系列だったりし、実際に
    このマシンの既定は v25 系になっていた。ネットワークが要るので実行はせず、形を見る。
    """

    def setUp(self):
        self.body = without_comments(TASKS / "fnm.yml")

    def test_installs_the_latest_lts(self):
        self.assertIn("fnm install --lts", self.body)
        # --lts で入れると fnm が lts-latest の別名を張る。版番号を自前で引かない
        self.assertIn("fnm default lts-latest", self.body)

    def test_does_not_pick_the_newest_release(self):
        self.assertNotIn("ls-remote", self.body, "最新版 (非 LTS でありうる) を選んでいる")
        self.assertNotIn("--latest", self.body)


class WorktreeSourceTest(unittest.TestCase):
    """worktree から流した playbook に、~/ 配下の symlink を張らせない (issue #209)。

    tasks/claude.yml と tasks/zshrc.yml は symlink の先を playbook_dir から組む。worktree
    (.claude/worktrees/*) から流すと ~/.claude/* と ~/.zshrc が worktree を指し、worktree が
    掃除された時点でリンクが全部切れる。フックと autoMode は fail-open なので、切れた状態では
    **安全装置が無音で全部外れる**。このリポジトリの開発は worktree で行うのが常で、
    「確かめるために --tags claude を流す」で踏みうる。
    """

    LINKING = ("claude.yml", "zshrc.yml")

    def test_the_guard_comes_before_any_symlink(self):
        """ansible が無い環境 (CI) でも見られる形の検査。"""
        for name in self.LINKING:
            with self.subTest(name):
                body = without_comments(TASKS / name)
                self.assertIn("state: link", body, "この検査の前提 (symlink を張る) が変わった")
                guard = body.find("/.claude/worktrees/")
                self.assertNotEqual(guard, -1, "worktree からの実行を止める検査が無い")
                first_task = body.index("- name:")
                self.assertLess(
                    body.index("ansible.builtin.assert"), body.index("- name:", first_task + 1),
                    "検査が先頭のタスクになっていない (止まる前に何かが適用される)",
                )

    def run_from(self, checkout, *extra):
        home = Path(self.workdir.name) / "home"
        home.mkdir(exist_ok=True)
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(checkout / "playbook_sillicon_mac.yml"),
             "--tags", "zshrc", "--check", *extra],
            capture_output=True, text=True, timeout=300, cwd=checkout,
            env=clean_env(HOME=str(home), XDG_CONFIG_HOME=None, ANSIBLE_NOCOLOR="1"),
        )
        return result

    def copy_repo_to(self, destination):
        repo = TASKS.parent
        shutil.copytree(
            repo, destination,
            ignore=shutil.ignore_patterns(".git", ".claude", "__pycache__"),
        )
        return destination

    def test_running_from_a_worktree_is_refused(self):
        """--check で流す (oh-my-zsh の取得を走らせないため)。assert は check でも評価される。"""
        if shutil.which("ansible-playbook") is None:
            self.skipTest("ansible が無い環境")
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        root = Path(self.workdir.name)

        plain = self.copy_repo_to(root / "plain" / "setup")
        worktree = self.copy_repo_to(root / "main" / ".claude" / "worktrees" / "some-task")

        # 対照。worktree でなければ通る (下の失敗が別の理由でないこと)
        ok = self.run_from(plain)
        self.assertEqual(ok.returncode, 0, ok.stdout[-2000:] + ok.stderr[-500:])

        refused = self.run_from(worktree)
        self.assertNotEqual(refused.returncode, 0, "worktree からの実行が通った")
        self.assertIn("worktree", refused.stdout)

        # 分かったうえで流す口 (HOME を一時ディレクトリへ向けた結合テストなど)
        forced = self.run_from(worktree, "-e", "allow_worktree_source=true")
        self.assertEqual(forced.returncode, 0, forced.stdout[-2000:])


class ModifierMappingTest(unittest.TestCase):
    """修飾キーの入れ替え (Caps Lock → Control / fn 無効。issue #276)。

    この設定は書き損じが**エラーにならない**。置き場を間違えても型を間違えても
    `defaults` は成功を返し、ただキーが効かないだけになる。タスクの形と、
    前提にしている `defaults` の挙動の両方を固定する。
    """

    KEY = "com.apple.keyboard.modifiermapping.0-0-0"
    # Caps Lock → 左 Control / 内蔵 fn → 無効 / 外付け fn → 無効
    SRCS = ("30064771129", "1095216660483", "280379760050179")

    def setUp(self):
        self.body = without_comments(TASKS / "macos.yml")
        self.block = next(
            (b for b in re.split(r"\n(?=- name:)", self.body)
             if self.KEY in b),
            None,
        )
        self.assertIsNotNone(self.block, "修飾キーを書くタスクが見当たらない")

    def test_writes_to_the_per_host_domain(self):
        """`-currentHost` を落とすと別の場所へ書いて、黙って効かなくなる。

        システム設定が読むのは ByHost の .GlobalPreferences。
        """
        calls = re.findall(r"/usr/bin/defaults\s+(\S+)", self.block)
        self.assertTrue(calls, "defaults を呼んでいない")
        self.assertEqual(
            [c for c in calls if c != "-currentHost"], [],
            "-currentHost の無い defaults 呼び出しがある (別の場所を読み書きする)",
        )

    def test_value_is_written_as_typed_xml(self):
        """旧形式で書くと数字が**文字列**になり、型が食い違ったまま効かない。"""
        self.assertIn(
            "<integer>", self.block,
            "値を XML plist で渡していない (旧形式は数字を文字列として書き込む)",
        )
        self.assertNotRegex(
            self.block, r"HIDKeyboardModifierMappingSrc\s*=",
            "旧形式 (Src=123;) で書いている",
        )

    def test_all_three_mappings_are_declared(self):
        """この pref は辞書の配列ひとつ。一部だけ書くと残りが消える。

        Caps Lock だけを書くタスクに縮めると、fn の 2 件が配列ごと上書きされる。
        """
        missing = [src for src in self.SRCS if src not in self.block]
        self.assertEqual(
            missing, [],
            "3 件そろっていない (配列ごと上書きするので、書き落とした分は消える)",
        )

    def test_the_write_is_not_unconditional(self):
        """毎回 changed を返すと、本当に変わったものが埋もれる (#174)。"""
        self.assertRegex(
            self.block, r"changed_when:.*stdout",
            "書き込みの有無を changed_when で見ていない",
        )

    @unittest.skipUnless(shutil.which("defaults"), "defaults が無い環境")
    def test_defaults_write_forms_behave_as_assumed(self):
        """前提にしている `defaults` の型の扱いそのもの。OS 側が変われば赤くなる。"""
        domain = "com.example.setup.ansible-tasks-test"
        self.addCleanup(
            subprocess.run, ["defaults", "delete", domain],
            capture_output=True,
        )

        def written(value):
            subprocess.run(
                ["defaults", "write", domain, "k", value],
                check=True, capture_output=True, env=clean_env(),
            )
            out = subprocess.run(
                ["defaults", "export", domain, "-"],
                check=True, capture_output=True, env=clean_env(),
            )
            return plistlib.loads(out.stdout)["k"][0]["n"]

        self.assertIsInstance(
            written("({n=1;})"), str,
            "旧形式が数字を整数で書くようになった (タスクを見直せる)",
        )
        self.assertIsInstance(
            written("<array><dict><key>n</key><integer>1</integer></dict></array>"), int,
            "XML plist で渡しても整数にならない (書き込みの形を変える必要がある)",
        )


class TaskTagNamingTest(unittest.TestCase):
    """CLAUDE.md の「tag 名はファイル名と同じ」を守る。

    ずれていると `--tags <ファイル名>` が 0 タスクで黙って成功する。
    """

    def test_every_task_file_is_imported_with_a_matching_tag(self):
        playbook = (TASKS.parent / "playbook_sillicon_mac.yml").read_text()
        imports = re.findall(
            r"tags:\s*(\S+)\s*\n\s*ansible\.builtin\.import_tasks:\s*tasks/(\S+)\.yml",
            playbook,
        )
        self.assertTrue(imports, "playbook から import を読み取れなかった")
        # 正規表現に合った import だけを見ていると、tags と import の順を入れ替えただけの
        # ファイルが検査から黙って外れる。tasks/ に在るものと突き合わせる (#218)
        self.assertEqual(
            sorted(name for _, name in imports),
            sorted(path.stem for path in TASKS.glob("*.yml")),
            "tasks/*.yml と、playbook から読み取れた import が一致しない "
            "(import されていないか、この検査が読めない書き方になっている)",
        )
        mismatched = [(tag, name) for tag, name in imports if tag != name]
        self.assertEqual(
            mismatched, [],
            "tag 名とファイル名がずれている (--tags <ファイル名> が 0 タスクで成功する)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
