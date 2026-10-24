#!/usr/bin/env python3
"""bin/git-wt-clean のテスト (issue #319)。

Stop hook が全リポジトリの全ターンで流すので、**消しても失うものが無い worktree だけを
消す**ことを固定する。消す側 (clean・参照済み・無活動) と残す側 (変更あり・追跡外あり・
locked・未参照のコミット・最近の活動・呼び出し元・メイン) を 1 件ずつ並べる。

    python3 claude/tests/git_wt_clean_test.py
"""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from hookenv import clean_env

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "bin" / "git-wt-clean"
OLD = time.time() - 30 * 86400


class GitWtCleanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n"
            "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
        )
        self.env = clean_env(
            GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
            GIT_CEILING_DIRECTORIES=str(self.root),
        )
        self.main = self.root / "main"
        self.git("init", "-q", str(self.main), cwd=self.root)
        (self.main / ".gitignore").write_text("build/\n")
        (self.main / "a.txt").write_text("a\n")
        self.git("add", ".")
        self.git("commit", "-qm", "init")

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.main, env=self.env, check=True,
            capture_output=True, text=True,
        ).stdout

    def add(self, name, *extra, old=True):
        path = self.root / name
        self.git("worktree", "add", "-q", *extra, str(path))
        if old:
            self.age(path)
        return path

    def age(self, path):
        admin = Path(self.git("rev-parse", "--absolute-git-dir", cwd=path).strip())
        for f in ("HEAD", "index", "logs/HEAD"):
            if (admin / f).exists():
                os.utime(admin / f, (OLD, OLD))

    def run_script(self, *args, cwd=None):
        return subprocess.run(
            [str(SCRIPT), *args], cwd=cwd or self.main, env=self.env,
            capture_output=True, text=True, timeout=60,
        )

    def listed(self, cwd=None):
        result = self.run_script("--list", cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {Path(p).resolve() for p in result.stdout.splitlines()}

    # --- 消す側 -------------------------------------------------------------

    def test_lists_old_clean_branch_worktree(self):
        path = self.add("branch", "-b", "feat/x")
        self.assertEqual(self.listed(), {path})

    def test_lists_old_clean_detached_worktree_on_referenced_commit(self):
        path = self.add("detached", "--detach")
        self.assertEqual(self.listed(), {path})

    def test_ignored_files_do_not_keep_a_worktree(self):
        """ビルド生成物は再生成できる。容量の大半はここなので、残す理由にしない。"""
        path = self.add("built", "--detach")
        (path / "build").mkdir()
        (path / "build" / "out.bin").write_text("x")
        self.age(path)
        self.assertEqual(self.listed(), {path})

    def test_listing_does_not_reset_the_idle_clock(self):
        """hook は毎ターン流す。判定の status が index を書き直すと、無活動の時計が
        毎回巻き戻って永遠に消えない。"""
        path = self.add("detached", "--detach")
        (path / "a.txt").touch()  # stat 情報をずらし、status に index を書き直す理由を作る
        self.age(path)
        self.listed()
        self.assertEqual(self.listed(), {path})

    def test_clean_removes_and_keeps_branch(self):
        path = self.add("branch", "-b", "feat/x")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertIn("feat/x", self.git("branch", "--list", "feat/x"))

    def test_clean_prunes_registrations_whose_directory_is_gone(self):
        path = self.add("gone", "--detach")
        subprocess.run(["rm", "-rf", str(path)], check=True)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertNotIn(str(path), self.git("worktree", "list"))

    # --- 残す側 -------------------------------------------------------------

    def test_keeps_tracked_changes(self):
        path = self.add("dirty", "--detach")
        (path / "a.txt").write_text("changed\n")
        self.age(path)
        self.assertEqual(self.listed(), set())

    def test_keeps_untracked_files(self):
        path = self.add("untracked", "--detach")
        (path / "new.txt").write_text("new\n")
        self.age(path)
        self.assertEqual(self.listed(), set())

    def test_keeps_locked(self):
        self.add("locked", "--detach", "--lock")
        self.assertEqual(self.listed(), set())

    def test_keeps_detached_commit_that_no_ref_contains(self):
        path = self.add("orphan", "--detach")
        (path / "a.txt").write_text("b\n")
        self.git("commit", "-qam", "only here", cwd=path)
        self.age(path)
        self.assertEqual(self.listed(), set())

    def test_keeps_recently_active(self):
        self.add("recent", "--detach", old=False)
        self.assertEqual(self.listed(), set())

    def test_days_threshold_is_configurable(self):
        path = self.add("recent", "--detach", old=False)
        result = self.run_script("--list", "--days", "0")
        self.assertEqual({Path(p).resolve() for p in result.stdout.splitlines()}, {path})

    def test_keeps_the_callers_worktree(self):
        path = self.add("caller", "--detach")
        (path / "sub").mkdir()
        self.assertEqual(self.listed(cwd=path / "sub"), set())

    def test_never_lists_main(self):
        self.assertEqual(self.listed(), set())

    def test_clean_with_nothing_to_remove_keeps_everything(self):
        path = self.add("dirty", "--detach")
        (path / "a.txt").write_text("changed\n")
        self.age(path)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue((path / "a.txt").exists())

    # --- 使い方 -------------------------------------------------------------

    def test_rejects_bad_days(self):
        self.assertEqual(self.run_script("--days", "x").returncode, 64)

    def test_rejects_unknown_argument(self):
        self.assertEqual(self.run_script("--force").returncode, 64)

    def test_refuses_outside_a_repository(self):
        outside = self.root / "plain"
        outside.mkdir()
        self.assertEqual(self.run_script("--list", cwd=outside).returncode, 64)


if __name__ == "__main__":
    unittest.main()
