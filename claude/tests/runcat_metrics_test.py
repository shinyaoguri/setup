#!/usr/bin/env python3
"""claude/runcat-metrics.py のテスト。

スクリプトは stdin を読んで走り切る作りなので、実際の契約
(stdin の JSON → 出力ファイルの JSON + stdout) をサブプロセス経由で検証する。

    python3 claude/tests/runcat_metrics_test.py
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "runcat-metrics.py"


class ScriptTestCase(unittest.TestCase):
    def run_script(self, stdin_text, workdir=None):
        """stdin を流してスクリプトを実行し、(スナップショット, stdout) を返す。

        workdir を渡すと出力先を共有できる (レート制限の控えを跨ぐ実行の検証用)。
        """
        if workdir is not None:
            return self.run_script_in(stdin_text, Path(workdir))
        with tempfile.TemporaryDirectory() as tmpdir:
            return self.run_script_in(stdin_text, Path(tmpdir))

    def run_script_in(self, stdin_text, workdir):
        out = workdir / "usage.json"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=stdin_text,
            capture_output=True,
            text=True,
            env={"RUNCAT_OUT_FILE": str(out), "PATH": "/usr/bin:/bin", "HOME": str(workdir)},
            check=True,
        )
        return json.loads(out.read_text(encoding="utf-8")), proc.stdout.strip()

    def rows(self, snapshot):
        return {m["title"]: m for m in snapshot["metrics"]}

    def run_hook(self, entries, event="Stop", workdir=None):
        """transcript JSONL を作り、hook 入力を流してスナップショットを返す。"""
        if workdir is None:
            with tempfile.TemporaryDirectory() as tmpdir:
                return self.run_hook(entries, event, Path(tmpdir))
        workdir = Path(workdir)
        transcript = workdir / "transcript.jsonl"
        transcript.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8"
        )
        snapshot, stdout = self.run_script(json.dumps({
            "hook_event_name": event,
            "session_id": "abc123",
            "transcript_path": str(transcript),
            "cwd": "/Users/so/.setup",
        }), workdir=workdir)
        # hook の stdout は会話へ混ざり得るので何も出さない
        self.assertEqual(stdout, "")
        return snapshot

    def assistant_entry(self, **overrides):
        entry = {
            "type": "assistant",
            "timestamp": "2026-07-23T09:19:24.246Z",
            "cwd": "/Users/so/.setup",
            "gitBranch": "feat/runcat-metrics-hook",
            "effort": "high",
            "message": {"model": "claude-opus-4-8", "usage": {
                "input_tokens": 2, "cache_creation_input_tokens": 1970,
                "cache_read_input_tokens": 138413, "output_tokens": 2163,
            }},
        }
        entry.update(overrides)
        return entry


class StatusLineModeTest(ScriptTestCase):
    """ターミナルの statusLine から呼ばれる経路。"""

    def test_full_payload_renders_every_row(self):
        now = int(time.time())
        payload = {
            "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
            "session_name": "runcat-metrics",
            "workspace": {
                "project_dir": "/Users/so/.setup",
                "git_worktree": "feature-xyz",
                "repo": {"host": "github.com", "owner": "shinyaoguri", "name": "setup"},
            },
            "cost": {
                "total_cost_usd": 12.3456,
                "total_duration_ms": 4_500_000,
                "total_api_duration_ms": 138_000,
                "total_lines_added": 156,
                "total_lines_removed": 23,
            },
            "context_window": {
                "total_input_tokens": 61_500,
                "total_output_tokens": 1_200,
                "context_window_size": 200_000,
                "used_percentage": 31.35,
            },
            "fast_mode": True,
            "effort": {"level": "xhigh"},
            "thinking": {"enabled": True},
            "rate_limits": {
                # 端数 30 秒を足して、テスト実行中の経過で分が繰り下がらないようにする
                "five_hour": {"used_percentage": 23.5, "resets_at": now + 9_660 + 30},
                "seven_day": {"used_percentage": 41.2, "resets_at": now + 273_600 + 30},
            },
            "agent": {"name": "security-reviewer"},
            "pr": {"number": 1234, "review_state": "pending"},
        }
        snapshot, stdout = self.run_script(json.dumps(payload))
        rows = self.rows(snapshot)

        self.assertEqual(stdout, "Opus 4.8")
        self.assertEqual(snapshot["title"], "Claude Code")
        # メニューバーへ出すのは週間制限の使用率 (整数へ丸める)
        self.assertEqual(snapshot["metricsBarValue"], "41%")
        self.assertEqual(rows["Model"]["formattedValue"], "Opus 4.8 · xhigh · think · fast")
        self.assertEqual(rows["Context"]["formattedValue"], "31.35% · 62.7k/200k")
        self.assertAlmostEqual(rows["Context"]["normalizedValue"], 0.3135)
        self.assertEqual(rows["5h"]["formattedValue"], "23.5% · 2h41m left")
        self.assertEqual(rows["7d"]["formattedValue"], "41.2% · 3d4h left")
        self.assertEqual(rows["Cost"]["formattedValue"], "$12.35")
        self.assertEqual(rows["Elapsed"]["formattedValue"], "1h15m · API 2m")
        self.assertEqual(rows["Edits"]["formattedValue"], "+156 / -23")
        self.assertEqual(rows["Project"]["formattedValue"], "setup · feature-xyz")
        self.assertEqual(rows["Session"]["formattedValue"], "runcat-metrics")
        self.assertEqual(rows["Agent"]["formattedValue"], "security-reviewer")
        self.assertEqual(rows["PR"]["formattedValue"], "#1234 · pending")
        # 進捗バーは割合を持つ行にだけ付く
        self.assertNotIn("normalizedValue", rows["Model"])
        self.assertNotIn("normalizedValue", rows["Cost"])

    def test_empty_payload_falls_back_to_model_row_only(self):
        snapshot, stdout = self.run_script("{}")
        self.assertEqual(stdout, "Claude Code")
        self.assertEqual([m["title"] for m in snapshot["metrics"]], ["Model"])
        self.assertNotIn("metricsBarValue", snapshot)

    def test_malformed_stdin_does_not_crash(self):
        for stdin_text in ("", "not json at all", "[1,2,3]", "null"):
            with self.subTest(stdin=stdin_text):
                snapshot, stdout = self.run_script(stdin_text)
                self.assertEqual(stdout, "Claude Code")
                self.assertEqual(self.rows(snapshot)["Model"]["formattedValue"], "Claude Code")

    def test_wrong_types_are_ignored_instead_of_rendered(self):
        payload = {
            "model": "Opus 4.8",  # dict でなく文字列
            "context_window": {"used_percentage": None, "total_input_tokens": "x"},
            "cost": {"total_cost_usd": True},  # bool は数値扱いしない
            "rate_limits": None,
            "effort": {"level": None},
            "fast_mode": "yes",  # true でなければマーカーを出さない
        }
        snapshot, _ = self.run_script(json.dumps(payload))
        self.assertEqual([m["title"] for m in snapshot["metrics"]], ["Model"])
        self.assertEqual(self.rows(snapshot)["Model"]["formattedValue"], "Claude Code")

    def test_expired_reset_and_over_limit_percentage(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 150, "resets_at": 1}}}
        snapshot, _ = self.run_script(json.dumps(payload))
        row = self.rows(snapshot)["5h"]
        self.assertEqual(row["formattedValue"], "150%")  # 過去のリセット時刻は添えない
        self.assertEqual(row["normalizedValue"], 1.0)  # バーは [0,1] にクランプ
        # 週間制限が無ければメニューバーの値も出さない (Context では代用しない)
        self.assertNotIn("metricsBarValue", snapshot)

    def test_million_token_context_window(self):
        payload = {"context_window": {
            "used_percentage": 8.4, "total_input_tokens": 83_000,
            "total_output_tokens": 1_500, "context_window_size": 1_000_000,
        }}
        snapshot, _ = self.run_script(json.dumps(payload))
        self.assertEqual(self.rows(snapshot)["Context"]["formattedValue"], "8.4% · 84.5k/1M")

    def test_project_falls_back_to_directory_name(self):
        payload = {"workspace": {"project_dir": "/Users/so/foo/"}, "worktree": {"name": "my-feature"}}
        snapshot, _ = self.run_script(json.dumps(payload))
        self.assertEqual(self.rows(snapshot)["Project"]["formattedValue"], "foo · my-feature")

    def test_zero_values_are_rendered_not_dropped(self):
        payload = {"cost": {"total_cost_usd": 0, "total_lines_added": 0, "total_lines_removed": 0},
                   "context_window": {"used_percentage": 0}}
        snapshot, _ = self.run_script(json.dumps(payload))
        rows = self.rows(snapshot)
        self.assertEqual(rows["Cost"]["formattedValue"], "$0.00")
        self.assertEqual(rows["Edits"]["formattedValue"], "+0 / -0")
        self.assertEqual(rows["Context"]["formattedValue"], "0%")

    def test_snapshot_is_valid_custom_metrics_schema(self):
        snapshot, _ = self.run_script('{"model": {"display_name": "Opus 4.8"}}')
        self.assertIsInstance(snapshot["title"], str)
        self.assertIsInstance(snapshot["symbol"], str)
        self.assertIsInstance(snapshot["metrics"], list)
        # lastUpdatedDate は RunCat が要求する ISO 8601 (UTC) 形式
        self.assertRegex(snapshot["lastUpdatedDate"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        for metric in snapshot["metrics"]:
            self.assertIsInstance(metric["title"], str)
            self.assertIsInstance(metric["formattedValue"], str)
            if "normalizedValue" in metric:
                self.assertGreaterEqual(metric["normalizedValue"], 0.0)
                self.assertLessEqual(metric["normalizedValue"], 1.0)

    def test_output_file_is_replaced_atomically(self):
        """一時ファイルを残さず os.replace で置き換える (RunCat が半端な内容を読まないため)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "usage.json"
            out.write_text("stale", encoding="utf-8")
            subprocess.run(
                [sys.executable, str(SCRIPT)],
                input='{"model": {"display_name": "Opus 4.8"}}',
                capture_output=True, text=True, check=True,
                env={"RUNCAT_OUT_FILE": str(out), "PATH": "/usr/bin:/bin", "HOME": tmpdir},
            )
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["title"], "Claude Code")
            self.assertEqual([p.name for p in Path(tmpdir).iterdir()], ["usage.json"])


class HookModeTest(ScriptTestCase):
    """hook から呼ばれる経路 (デスクトップアプリではこちらだけが動く)。"""

    def test_transcript_renders_available_rows(self):
        snapshot = self.run_hook([
            {"type": "user", "timestamp": "2026-07-23T08:48:54.157Z", "cwd": "/Users/so/.setup"},
            {"type": "custom-title", "customTitle": "RunCat 連携"},
            {"type": "pr-link", "prNumber": 23, "prRepository": "shinyaoguri/setup"},
            self.assistant_entry(),
        ])
        rows = self.rows(snapshot)
        self.assertEqual(rows["Model"]["formattedValue"], "Opus 4.8 · high")
        # 142,548 / 200,000 = 71.274% → 小数第 1 位へ丸める
        self.assertEqual(rows["Context"]["formattedValue"], "71.3% · 142.5k/200k")
        self.assertEqual(rows["Elapsed"]["formattedValue"], "30m")
        self.assertEqual(rows["Project"]["formattedValue"], "setup · feat/runcat-metrics-hook")
        self.assertEqual(rows["Session"]["formattedValue"], "RunCat 連携")
        self.assertEqual(rows["PR"]["formattedValue"], "#23")
        # hook 入力からは取れない行は出さない (5h / 7d は控えが無ければ同様)
        for absent in ("5h", "7d", "Cost", "Edits", "Agent"):
            self.assertNotIn(absent, rows)
        self.assertNotIn("metricsBarValue", snapshot)

    def test_model_id_is_prettified(self):
        for model_id, expected in [
            ("claude-opus-4-8", "Opus 4.8"),
            ("claude-sonnet-5", "Sonnet 5"),
            ("claude-haiku-4-5-20251001", "Haiku 4.5"),  # 末尾の日付スナップショットは落とす
            ("claude-fable-5", "Fable 5"),
        ]:
            with self.subTest(model_id=model_id):
                snapshot = self.run_hook([self.assistant_entry(
                    message={"model": model_id, "usage": {}}, effort=None)])
                self.assertEqual(self.rows(snapshot)["Model"]["formattedValue"], expected)

    def test_sidechain_entries_are_ignored(self):
        """サブエージェント (isSidechain) の usage は本セッションの文脈量ではない。"""
        snapshot = self.run_hook([
            self.assistant_entry(),
            self.assistant_entry(isSidechain=True, message={
                "model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 5}}),
        ])
        rows = self.rows(snapshot)
        self.assertEqual(rows["Model"]["formattedValue"], "Opus 4.8 · high")
        self.assertEqual(rows["Context"]["formattedValue"], "71.3% · 142.5k/200k")

    def test_broken_transcript_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            transcript.write_text(
                "not json\n\n" + json.dumps(self.assistant_entry()) + "\n{ truncated",
                encoding="utf-8",
            )
            snapshot, stdout = self.run_script(json.dumps({
                "hook_event_name": "Stop", "transcript_path": str(transcript)}))
            self.assertEqual(stdout, "")
            self.assertEqual(self.rows(snapshot)["Model"]["formattedValue"], "Opus 4.8 · high")

    def test_huge_transcript_reads_only_the_tail(self):
        """毎ツール呼び出しで走るため、巨大な transcript でも末尾しか読まない。"""
        # 末尾チャンクに載る行はセッション開始より後の時刻にして、
        # 先頭行を読まないと経過時間がずれるようにする
        filler = [{"type": "user", "timestamp": "2026-07-23T09:10:00.000Z", "text": "x" * 2000}
                  for _ in range(400)]  # 800KB 超 > TRANSCRIPT_TAIL_BYTES (256KB)
        start = {"type": "user", "timestamp": "2026-07-23T08:48:54.157Z", "cwd": "/Users/so/.setup"}
        snapshot = self.run_hook([start] + filler + [self.assistant_entry()])
        rows = self.rows(snapshot)
        self.assertEqual(rows["Model"]["formattedValue"], "Opus 4.8 · high")
        # 先頭行だけは別途読むので、セッション開始からの経過時間になる (末尾からなら 9m)
        self.assertEqual(rows["Elapsed"]["formattedValue"], "30m")

    def test_missing_transcript_does_not_fail_the_hook(self):
        """hook を止めないため、transcript が読めなくても異常終了しない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "usage.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=json.dumps({"hook_event_name": "Stop", "transcript_path": "/nonexistent.jsonl"}),
                capture_output=True, text=True,
                env={"RUNCAT_OUT_FILE": str(out), "PATH": "/usr/bin:/bin", "HOME": tmpdir},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertFalse(out.exists())  # 既存のカードを壊さない


class RateLimitCacheTest(ScriptTestCase):
    """レート制限は hook 入力に無いため、statusLine で取れた値を控えて使い回す。"""

    CACHE_NAME = "runcat-rate-limits.json"

    def statusline_payload(self, five_hour=None, seven_day=None):
        limits = {}
        if five_hour is not None:
            limits["five_hour"] = five_hour
        if seven_day is not None:
            limits["seven_day"] = seven_day
        return json.dumps({"model": {"display_name": "Opus 4.8"}, "rate_limits": limits})

    def cache(self, workdir):
        return json.loads((Path(workdir) / self.CACHE_NAME).read_text(encoding="utf-8"))

    def test_statusline_caches_rate_limits_for_the_hook(self):
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                five_hour={"used_percentage": 23.5, "resets_at": now + 9_660 + 30},
                seven_day={"used_percentage": 41.2, "resets_at": now + 273_600 + 30},
            ), workdir=tmpdir)
            self.assertEqual(self.cache(tmpdir)["five_hour"]["used_percentage"], 23.5)

            snapshot = self.run_hook([self.assistant_entry()], workdir=tmpdir)
            rows = self.rows(snapshot)
            # 控えは最後に取れた時点の値で実際はそれ以上なので、下限として ≥ を付ける
            self.assertEqual(rows["5h"]["formattedValue"], "≥23.5% · 2h41m left")
            self.assertEqual(rows["7d"]["formattedValue"], "≥41.2% · 3d4h left")
            self.assertEqual(snapshot["metricsBarValue"], "≥41%")
            # 控えを使っても他の行は transcript 由来のまま
            self.assertEqual(rows["Model"]["formattedValue"], "Opus 4.8 · high")

    def test_reset_windows_are_not_reused(self):
        """リセット済みのウィンドウの使用率はもう当てにならないので出さない。"""
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                five_hour={"used_percentage": 23.5, "resets_at": now - 60},
                seven_day={"used_percentage": 41.2, "resets_at": now + 273_600 + 30},
            ), workdir=tmpdir)
            rows = self.rows(self.run_hook([self.assistant_entry()], workdir=tmpdir))
            self.assertNotIn("5h", rows)
            self.assertEqual(rows["7d"]["formattedValue"], "≥41.2% · 3d4h left")

    def test_windows_without_reset_time_are_not_cached(self):
        """リセット時刻が無いと古さを判定できないため控えない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                five_hour={"used_percentage": 23.5}), workdir=tmpdir)
            self.assertFalse((Path(tmpdir) / self.CACHE_NAME).exists())
            self.assertNotIn("5h", self.rows(self.run_hook([self.assistant_entry()], workdir=tmpdir)))

    def test_statusline_without_rate_limits_keeps_the_cache(self):
        """API キー利用などで制限が来ないターンがあっても、控えを消さない。"""
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                seven_day={"used_percentage": 41.2, "resets_at": now + 273_600 + 30}), workdir=tmpdir)
            self.run_script('{"model": {"display_name": "Opus 4.8"}}', workdir=tmpdir)
            self.assertEqual(self.cache(tmpdir)["seven_day"]["used_percentage"], 41.2)
            self.assertIn("7d", self.rows(self.run_hook([self.assistant_entry()], workdir=tmpdir)))

    def test_broken_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / self.CACHE_NAME).write_text("{ truncated", encoding="utf-8")
            snapshot = self.run_hook([self.assistant_entry()], workdir=tmpdir)
            rows = self.rows(snapshot)
            self.assertEqual(rows["Model"]["formattedValue"], "Opus 4.8 · high")
            self.assertNotIn("5h", rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
