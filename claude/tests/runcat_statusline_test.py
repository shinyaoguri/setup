#!/usr/bin/env python3
"""claude/runcat-statusline.py のテスト。

スクリプトは import 時に stdin を読んで走り切る作りなので、実際の契約
(stdin の JSON → 出力ファイルの JSON + stdout) をサブプロセス経由で検証する。

    python3 claude/tests/runcat_statusline_test.py
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "runcat-statusline.py"


class RunCatStatusLineTestCase(unittest.TestCase):
    def run_script(self, stdin_text):
        """stdin を流してスクリプトを実行し、(スナップショット, stdout) を返す。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "usage.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=stdin_text,
                capture_output=True,
                text=True,
                env={"RUNCAT_OUT_FILE": str(out), "PATH": "/usr/bin:/bin", "HOME": tmpdir},
                check=True,
            )
            return json.loads(out.read_text(encoding="utf-8")), proc.stdout.strip()

    def rows(self, snapshot):
        return {m["title"]: m for m in snapshot["metrics"]}

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
        self.assertEqual(snapshot["metricsBarValue"], "31%")  # メニューバーは整数へ丸める
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

    def test_million_token_context_window(self):
        payload = {"context_window": {
            "used_percentage": 8.4, "total_input_tokens": 83_000,
            "total_output_tokens": 1_500, "context_window_size": 1_000_000,
        }}
        snapshot, _ = self.run_script(json.dumps(payload))
        self.assertEqual(self.rows(snapshot)["Context"]["formattedValue"], "8.4% · 84.5k/1M")
        self.assertEqual(snapshot["metricsBarValue"], "8%")

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
        self.assertEqual(snapshot["metricsBarValue"], "0%")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
