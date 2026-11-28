#!/usr/bin/env python3
"""リポジトリの .gitignore が、秘密の一次防御として効いていること。

グローバル CLAUDE.md は「秘密情報の置き場は gitignore 済みの .env などに限る」と定め、
claude/git-safety-guard.sh はこの gitignore が効いていることを**一次防御として前提に
している** (deny の理由も「.gitignore に追加する (漏れているから検査に引っかかって
いる)」から始まる)。

この守りがマシン側の ~/.config/git/ignore にしか無いと、**新しいマシンで再現しない**
(issue #178)。リポジトリの .gitignore で閉じていることを確かめる。

    python3 claude/tests/gitignore_test.py
"""

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def ignored(path, *, repo_only):
    """path が無視されるか。

    repo_only=True のときは**リポジトリの .gitignore だけ**で判定する
    (グローバルの除外設定を空にする)。マシン側に頼っていないことを見るため。
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": str(REPO)}
    args = ["git"]
    if repo_only:
        # グローバルと ~/.config の除外を無効化する。core.excludesFile を
        # 存在しないパスへ向ければ、残るのはリポジトリ内の .gitignore だけ
        args += ["-c", "core.excludesFile=/dev/null"]
    args += ["check-ignore", "-q", path]
    return subprocess.run(args, cwd=REPO, env=env).returncode == 0


class SecretsAreIgnoredTest(unittest.TestCase):
    def test_dotenv_is_ignored_by_the_repository_itself(self):
        """マシン側の設定に頼らず、リポジトリだけで .env が無視されること。"""
        self.assertTrue(
            ignored(".env", repo_only=True),
            ".env がリポジトリの .gitignore で無視されていない",
        )

    def test_dotenv_variants_are_ignored(self):
        for name in (".env.local", ".env.production", ".env.1password"):
            with self.subTest(name):
                self.assertTrue(ignored(name, repo_only=True))

    def test_private_key_files_are_ignored(self):
        """秘密鍵のファイルを置いても追跡されない (issue #226)。

        このリポジトリは GitHub App の秘密鍵 (PEM) を扱う運用をしている。値は 1Password に
        在ってファイルにはしない建前だが、取り出して確かめる場面でファイルができうる。
        """
        for name in ("mokume-agent.private-key.pem", "deploy.key", "tmp/cert.p12"):
            with self.subTest(name):
                self.assertTrue(
                    ignored(name, repo_only=True),
                    f"{name} がリポジトリの .gitignore で無視されていない",
                )

    def test_public_keys_are_not_ignored(self):
        """公開鍵まで無視しない (*.pub は配るものになりうる)。"""
        self.assertFalse(ignored("id_ecdsa.pub", repo_only=True))

    def test_the_template_stays_tracked(self):
        """.env.example は値でなく変数名を置く場所なので追跡する。"""
        self.assertFalse(
            ignored(".env.example", repo_only=True),
            ".env.example まで無視している (雛形が配れない)",
        )

    def test_machine_local_claude_settings_are_ignored(self):
        """settings.local.json はマシンごとに違う許可。コミットすると他マシンを汚す。"""
        self.assertTrue(
            ignored(".claude/settings.local.json", repo_only=True),
            "settings.local.json がリポジトリの .gitignore で無視されていない",
        )

    def test_no_secret_is_currently_tracked(self):
        """いま追跡されているファイルに秘密の置き場が混ざっていないこと。"""
        listing = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.split("\n")
        tracked = [
            f for f in listing
            if f == ".env" or f.startswith(".env.") and f != ".env.example"
            or f.endswith("settings.local.json")
        ]
        self.assertEqual(tracked, [], f"秘密の置き場が追跡されている: {tracked}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
