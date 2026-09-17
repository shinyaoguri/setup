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
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent.parent / "tasks"

ABSENT_PKG = "com.example.definitely.not.installed"


class RosettaDetectionTest(unittest.TestCase):
    """Rosetta の存在チェック。誤ると新しいマシンに Rosetta が入らない。"""

    def setUp(self):
        raw = (TASKS / "rosetta.yml").read_text()
        # コメントは「なぜその形にしたか」を書くのに悪い形を引用するので、検査から外す
        self.body = "\n".join(
            line for line in raw.split("\n") if not line.lstrip().startswith("#")
        )

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
        raw = (TASKS / "fonts.yml").read_text()
        self.body = "\n".join(
            line for line in raw.split("\n") if not line.lstrip().startswith("#")
        )

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
        body = (TASKS / "ssh.yml").read_text()
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
            body = path.read_text()
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
        raw = (TASKS / "macos.yml").read_text()
        self.body = raw
        # コメントは「なぜその形にしたか」を書くのに機構の名前を引用するので、
        # タスク単位で見るときは外す (コメントが前のタスクの塊に混ざる)
        self.tasks_only = "\n".join(
            line for line in raw.split("\n") if not line.lstrip().startswith("#")
        )

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
