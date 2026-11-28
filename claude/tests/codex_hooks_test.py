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
import time
import unittest
from pathlib import Path

from ansible_tasks_test import without_comments
from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent
HOOKS = REPO / "codex" / "hooks.json"
SETTINGS = REPO / "claude" / "settings.json"
TASKS = REPO / "tasks" / "codex.yml"
# このリポジトリが配る git のサブコマンド (bin/git-wt-clean など) の置き場
REPO_BIN = REPO / "bin"


def hook_path(fake_bin):
    """フックを流すときの PATH。

    偽の `git-<name>` を置いたディレクトリを先頭に、**本物の `bin/` も続けて置く**。
    本物が PATH に居る状況 (開発機) と居ない状況 (CI の runner) で判定が変わらない
    ようにするため — 居ない側しか試していなかったせいで issue #332 は CI で緑のまま
    開発機でだけ赤かった。git / nohup / sh を引くために /usr/bin と /bin も足す。
    """
    return ":".join([str(fake_bin), str(REPO_BIN), "/usr/bin", "/bin"])


def commands(hooks, event):
    return [
        hook["command"]
        for group in hooks.get("hooks", {}).get(event, [])
        for hook in group.get("hooks", [])
    ]


def gone_clean(hooks):
    return [c for c in commands(hooks, "Stop") if "git gone-clean" in c]


def wt_clean(hooks):
    return [c for c in commands(hooks, "Stop") if "git wt-clean" in c]


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

    def test_stop_runs_wt_clean_once_like_claude(self):
        """worktree の掃除 (issue #319) も Claude と同じコマンドで 1 回だけ。"""
        self.assertEqual(len(wt_clean(self.hooks)), 1)
        self.assertEqual(wt_clean(self.hooks), wt_clean(json.loads(SETTINGS.read_text())))

    def test_only_hooks(self):
        """Codex 固有の設定は config.toml の持ち物。ここへ混ぜない。"""
        self.assertEqual(list(self.hooks), ["hooks"])


class HookRunner:
    """フックのコマンドを実際に流し、`git <subcommand>` が呼ばれたかを見る。

    掃除そのものの中身は tasks/git.yml と bin/ の持ち物なので、ここでは差し替えた
    サブコマンドで「cwd のリポジトリで呼ばれたか」だけを見る。

    **差し替えは PATH 上の外部コマンド `git-<name>` で行う。** git は設定の alias より
    PATH の `git-<name>` を優先するので、alias を仕込む形はこのリポジトリが配る
    bin/git-wt-clean が PATH に居る開発機で本物に負け、マーカーが作られないまま
    黙って赤くなる (issue #332)。本番と同じ解決順を通るこの形なら、後から
    bin/git-gone-clean が増えても判定は変わらない。
    """

    subcommand = None  # サブクラスが名乗る (gone-clean / wt-clean)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.marker = self.root / "called"

    def run_hook(self, cwd, body):
        """`git <subcommand>` が body を実行するようにして、フックを 1 回流す。"""
        script = self.fake_bin / f"git-{self.subcommand}"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        # 利用者の alias / 設定が混ざらないように空の global 設定を向ける
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text("")
        return subprocess.run(
            ["/bin/sh", "-c", self.command], cwd=cwd, capture_output=True, text=True, timeout=30,
            env=clean_env(PATH=hook_path(self.fake_bin), GIT_CONFIG_GLOBAL=str(gitconfig),
                          GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=str(self.root)),
        )

    def make_repo(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        return repo


class PathSetupTest(unittest.TestCase):
    def test_the_real_bin_is_on_the_hook_path(self):
        """本物の `git-<name>` が PATH に居る状況を、CI の runner でも作る (issue #332)。"""
        self.assertIn(str(REPO_BIN), hook_path("/fake").split(":"))

    def test_the_repository_ships_a_git_subcommand(self):
        """上の状況が「本物が実在する」前提の上に立っていることを固定する。"""
        self.assertTrue(
            sorted(REPO_BIN.glob("git-*")), "bin/ に git-* が無く、PATH への差し込みが空振りする"
        )


class HookCommandTest(HookRunner, unittest.TestCase):
    subcommand = "gone-clean"

    def setUp(self):
        super().setUp()
        self.command = gone_clean(json.loads(HOOKS.read_text()))[0]

    def test_calls_gone_clean_inside_a_repository(self):
        result = self.run_hook(self.make_repo(), f"touch {self.marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists(), "リポジトリの中で gone-clean が呼ばれていない")

    def test_does_nothing_outside_a_repository(self):
        outside = self.root / "plain"
        outside.mkdir()
        result = self.run_hook(outside, f"touch {self.marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists(), "git の外で gone-clean を呼んだ")

    def test_failure_does_not_block_the_turn(self):
        result = self.run_hook(self.make_repo(), "echo boom >&2; exit 3")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, "", "失敗の出力がターンへ漏れている")


class WtCleanHookTest(HookRunner, unittest.TestCase):
    """worktree の削除は巨大になりうるので背景へ逃がす。hook 自体はすぐ返り、
    git の外でも失敗してもターンを止めない。"""

    subcommand = "wt-clean"

    def setUp(self):
        super().setUp()
        self.command = wt_clean(json.loads(HOOKS.read_text()))[0]

    def wait_for_marker(self):
        for _ in range(50):
            if self.marker.exists():
                return True
            time.sleep(0.1)
        return False

    def test_calls_wt_clean_in_the_background(self):
        started = time.monotonic()
        result = self.run_hook(self.make_repo(), f"sleep 2; touch {self.marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 1.5, "削除を待ってからターンへ戻っている")
        self.assertTrue(self.wait_for_marker(), "リポジトリの中で wt-clean が呼ばれていない")

    def test_does_nothing_outside_a_repository(self):
        outside = self.root / "plain"
        outside.mkdir()
        result = self.run_hook(outside, f"touch {self.marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(0.5)
        self.assertFalse(self.marker.exists(), "git の外で wt-clean を呼んだ")

    def test_failure_does_not_block_the_turn(self):
        result = self.run_hook(self.make_repo(), "echo boom >&2; exit 3")
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
