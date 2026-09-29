#!/usr/bin/env python3
"""codex/hooks.json と、それを ~/.codex へ配る tasks/codex.yml のテスト (issue #297)。

Codex のセッションでも、Claude と同じ Stop hook (`git gone-clean`) で役目を終えた
ローカルブランチを片付ける。フックは全リポジトリの全ターンで走るので、**git の外でも、
掃除が失敗してもターンを止めない**ことを固定する。中身は claude/settings.json の
Stop hook と同じコマンドに揃え、片方だけ直して食い違うのを落とす。

    python3 claude/tests/codex_hooks_test.py
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ansible_tasks_test import without_comments
from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent
HOOKS = REPO / "codex" / "hooks.json"
SETTINGS = REPO / "claude" / "settings.json"
TASKS = REPO / "tasks" / "codex.yml"


def commands(hooks, event):
    return [
        hook["command"]
        for group in hooks.get("hooks", {}).get(event, [])
        for hook in group.get("hooks", [])
    ]


def gone_clean(hooks):
    return [c for c in commands(hooks, "Stop") if "git gone-clean" in c]


class HooksFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hooks = json.loads(HOOKS.read_text())

    def test_stop_runs_gone_clean_once(self):
        self.assertEqual(len(gone_clean(self.hooks)), 1)

    def test_same_command_as_claude(self):
        """Claude 側だけ直して Codex 側が古いまま残るのを落とす。"""
        claude = gone_clean(json.loads(SETTINGS.read_text()))
        self.assertEqual(gone_clean(self.hooks), claude)

    def test_only_hooks(self):
        """Codex 固有の設定は config.toml の持ち物。ここへ混ぜない。"""
        self.assertEqual(list(self.hooks), ["hooks"])


class HookCommandTest(unittest.TestCase):
    """コマンドを実際に流す。gone-clean の中身は tasks/git.yml の持ち物なので、
    ここでは差し替えた alias で「cwd のリポジトリで呼ばれたか」だけを見る。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.command = gone_clean(json.loads(HOOKS.read_text()))[0]
        self.marker = self.root / "called"

    def run_hook(self, cwd, alias):
        gitconfig = self.root / "gitconfig"
        # 引用符で囲む。囲まないと `;` 以降が gitconfig のコメントとして落ちる
        gitconfig.write_text(f'[alias]\n\tgone-clean = "{alias}"\n')
        return subprocess.run(
            ["/bin/sh", "-c", self.command], cwd=cwd, capture_output=True, text=True, timeout=30,
            env=clean_env(GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
                          GIT_CEILING_DIRECTORIES=str(self.root)),
        )

    def make_repo(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        return repo

    def test_calls_gone_clean_inside_a_repository(self):
        result = self.run_hook(self.make_repo(), f"!touch {self.marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists(), "リポジトリの中で gone-clean が呼ばれていない")

    def test_does_nothing_outside_a_repository(self):
        outside = self.root / "plain"
        outside.mkdir()
        result = self.run_hook(outside, f"!touch {self.marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists(), "git の外で gone-clean を呼んだ")

    def test_failure_does_not_block_the_turn(self):
        result = self.run_hook(self.make_repo(), "!echo boom >&2; exit 3")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, "", "失敗の出力がターンへ漏れている")


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.body = without_comments(TASKS)

    def test_links_hooks_json(self):
        self.assertIn("{{ playbook_dir }}/codex/hooks.json", self.body)
        self.assertIn("/.codex/hooks.json", self.body)
        self.assertIn("state: link", self.body)

    def test_leaves_config_toml_to_the_app(self):
        """config.toml は Codex アプリが書き換える。リンクや差し込みで奪い合わない。"""
        self.assertNotIn("config.toml", self.body)

    def test_imported_by_the_playbook(self):
        playbook = (REPO / "playbook_sillicon_mac.yml").read_text()
        self.assertIn("tasks/codex.yml", playbook)


if __name__ == "__main__":
    unittest.main()
