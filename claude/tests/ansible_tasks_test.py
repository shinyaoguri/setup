#!/usr/bin/env python3
"""tasks/*.yml のうち、判定を間違えると黙って何もしなくなるものを固定する。

ansible のタスクは「実行されなかった」と「実行して変更が無かった」が結果として
同じ緑に見える。when の条件が永久に偽になっていても気付けないので、条件の根拠に
している外部コマンドの挙動ごと検証する (issue #172)。

    python3 claude/tests/ansible_tasks_test.py
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

TASKS = Path(__file__).resolve().parent.parent.parent / "tasks"

ABSENT_PKG = "com.example.definitely.not.installed"


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
        self.assertIn("killall", self.body, "Dock を入れ直していない")
        self.assertIn(
            "when: dock_autohide.changed", self.body,
            "Dock の再起動が条件付きになっていない",
        )

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


class FirstRunWithoutKeyTest(unittest.TestCase):
    """新しいマシンの初回実行が、Secretive の鍵が無いことで止まらない (issue #196)。

    Secretive は同じ実行の中で入ったばかりで、鍵を作るのは GUI 操作 — つまり初回は
    **必ず**鍵が無い。tasks/git.yml がそこで fail していたため、playbook は順序で後ろに
    ある fonts / terminal / zshrc / claude / fnm を一切適用せず、bootstrap も set -e で
    Step 7 と最後の案内へ届かなかった。「鍵がまだ無い」は途中経過であって失敗ではない。

    when の条件を文字列で見ても、実際に止まらないことは分からない。HOME を一時
    ディレクトリへ向けて tasks/git.yml を本当に流す (git config --global も鍵の探索も
    HOME の下で完結する)。ansible が無い環境では skip — CI で流す件は issue #220。
    """

    PLAYBOOK = TASKS.parent / "playbook_sillicon_mac.yml"
    FAKE_KEY = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= test@secretive"

    def setUp(self):
        if shutil.which("ansible-playbook") is None:
            self.skipTest("ansible が無い環境")
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.home = Path(self.workdir.name)

    def other_tags(self):
        tags = re.findall(r"^\s*tags:\s*(\S+)", self.PLAYBOOK.read_text(), re.M)
        self.assertIn("git", tags)
        return [t for t in tags if t != "git"]

    def run_git_tasks(self, explicit):
        """explicit=True は `--tags git` (人が名指しで流した)。False は全体実行の再現で、
        git 以外を --skip-tags で外す (重い brew / cask を流さないため)。どちらの形でも
        走るのは tasks/git.yml だけだが、ansible_run_tags は前者が ['git']、後者が ['all']。
        """
        selection = ["--tags", "git"] if explicit else ["--skip-tags", ",".join(self.other_tags())]
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(self.PLAYBOOK), *selection],
            capture_output=True, text=True, timeout=300, cwd=self.home,
            env=clean_env(HOME=str(self.home), XDG_CONFIG_HOME=None, ANSIBLE_NOCOLOR="1"),
        )

    def git_config(self, key):
        result = subprocess.run(
            ["git", "config", "--file", str(self.home / ".gitconfig"), "--get", key],
            capture_output=True, text=True, env=clean_env(HOME=str(self.home)),
        )
        return result.stdout.strip()

    def place_key(self):
        keys = self.home / "Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data/PublicKeys"
        keys.mkdir(parents=True)
        (keys / "test.pub").write_text(self.FAKE_KEY + "\n")

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
        self.assertEqual(signing_key.read_text().strip(), self.FAKE_KEY)
        self.assertIn(self.FAKE_KEY, (self.home / ".config/git/allowed_signers").read_text())


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
        mismatched = [(tag, name) for tag, name in imports if tag != name]
        self.assertEqual(
            mismatched, [],
            "tag 名とファイル名がずれている (--tags <ファイル名> が 0 タスクで成功する)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
