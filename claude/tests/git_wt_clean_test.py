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
# 畳まれた worktree の近道 (既定 1 日) には届き、通常の閾値 (既定 7 日) には届かない
TWO_DAYS = time.time() - 2 * 86400


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

    def age(self, path, when=OLD):
        admin = Path(self.git("rev-parse", "--absolute-git-dir", cwd=path).strip())
        for f in ("HEAD", "index", "logs/HEAD"):
            if (admin / f).exists():
                os.utime(admin / f, (when, when))

    def with_origin(self):
        """裸のリモートに main を置き、origin/HEAD を張る (既定ブランチの比較相手)。"""
        bare = self.root / "origin.git"
        self.git("init", "-q", "--bare", str(bare), cwd=self.root)
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", "-u", "origin", "main")
        self.git("remote", "set-head", "origin", "main")

    def branch_with_change(self, name, content):
        """main から切ったブランチに 1 コミット足した worktree を作る。"""
        path = self.add(name, "-b", name, old=False)
        (path / "a.txt").write_text(content)
        self.git("commit", "-qam", name, cwd=path)
        return path

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

    def test_lists_folded_worktree_after_one_day(self):
        """squash merge 済み (同じ変更が別のコミットで main に入った) なら 7 日待たない。"""
        self.with_origin()
        path = self.branch_with_change("feat-done", "done\n")
        (self.main / "a.txt").write_text("done\n")
        self.git("commit", "-qam", "squash of feat-done")
        self.git("push", "-q", "origin", "main")
        self.age(path, TWO_DAYS)
        self.assertEqual(self.listed(), {path})

    def test_lists_worktree_whose_upstream_is_gone_after_one_day(self):
        """merge でリモートのブランチが消えた (追跡先が gone) なら 7 日待たない。"""
        self.with_origin()
        path = self.branch_with_change("feat-gone", "gone\n")
        self.git("push", "-q", "-u", "origin", "feat-gone", cwd=path)
        self.git("push", "-q", "origin", "--delete", "feat-gone")
        self.git("fetch", "-q", "--prune", "origin")
        self.age(path, TWO_DAYS)
        self.assertEqual(self.listed(), {path})

    def test_merged_days_threshold_is_configurable(self):
        self.with_origin()
        path = self.add("same", "--detach", old=False)
        result = self.run_script("--list", "--merged-days", "0")
        self.assertEqual({Path(p).resolve() for p in result.stdout.splitlines()}, {path})

    # --- 残す側 -------------------------------------------------------------

    def test_keeps_unfolded_worktree_for_the_full_days(self):
        """main に入っていない仕事は、2 日では消さない (通常の 7 日を待つ)。"""
        self.with_origin()
        path = self.branch_with_change("feat-open", "open\n")
        self.age(path, TWO_DAYS)
        self.assertEqual(self.listed(), set())

    def test_keeps_folded_worktree_active_within_a_day(self):
        """畳まれていても、1 日以内に触ったものは使い途中かもしれないので残す。"""
        self.with_origin()
        self.add("fresh", "--detach", old=False)
        self.assertEqual(self.listed(), set())

    def test_app_worktree_does_not_take_the_folded_shortcut(self):
        """アプリが作った worktree はアプリが回収するので、近道に乗せず 7 日のまま。"""
        self.with_origin()
        path = self.add("app", "--detach")
        admin = Path(self.git("rev-parse", "--absolute-git-dir", cwd=path).strip())
        (admin / "claude-desktop-worktree").touch()
        self.age(path, TWO_DAYS)
        self.assertEqual(self.listed(), set())

    def test_without_a_remote_folded_shortcut_is_off(self):
        """比べる既定ブランチが無ければ、近道は使わない。"""
        path = self.add("noremote", "--detach")
        self.age(path, TWO_DAYS)
        self.assertEqual(self.listed(), set())

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

    def test_rejects_bad_merged_days(self):
        self.assertEqual(self.run_script("--merged-days", "-1").returncode, 64)

    def test_rejects_unknown_argument(self):
        self.assertEqual(self.run_script("--force").returncode, 64)

    def test_refuses_outside_a_repository(self):
        outside = self.root / "plain"
        outside.mkdir()
        self.assertEqual(self.run_script("--list", cwd=outside).returncode, 64)


if __name__ == "__main__":
    unittest.main()
