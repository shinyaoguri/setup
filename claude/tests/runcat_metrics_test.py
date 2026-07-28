#!/usr/bin/env python3
"""claude/runcat-metrics.py のテスト。

スクリプトは stdin を読んで走り切る作りなので、実際の契約
(stdin の JSON → 2 枚のカード JSON + stdout) をサブプロセス経由で検証する。

    python3 claude/tests/runcat_metrics_test.py
"""

import json
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from typing import NamedTuple

SCRIPT = Path(__file__).resolve().parent.parent / "runcat-metrics.py"


class Cards(NamedTuple):
    """1 回の実行で書き出される 2 枚のカードと stdout。"""

    limits: dict    # レート制限のカード (5h / 7d / 7d Fable)
    sessions: dict  # 動いているセッションのカード
    stdout: str


# 既定ではここを向けて使用量エンドポイントを叩かせない。実際の API を叩くと
# テストが外部とネットワークに依存し、ユーザーのレート制限も消費してしまう
NO_USAGE_URL = "file:///nonexistent/runcat-usage-stub.json"


class ScriptTestCase(unittest.TestCase):
    def run_script(self, stdin_text, workdir=None, usage_url=NO_USAGE_URL):
        """stdin を流してスクリプトを実行し、2 枚のカードと stdout を返す。

        workdir を渡すと出力先を共有できる (レート制限の控えやセッション状態を
        跨ぐ実行の検証用)。usage_url に file:// のスタブを渡すと、使用量
        エンドポイントから取れたときの経路を検証できる。
        """
        if workdir is not None:
            return self.run_script_in(stdin_text, Path(workdir), usage_url)
        with tempfile.TemporaryDirectory() as tmpdir:
            return self.run_script_in(stdin_text, Path(tmpdir), usage_url)

    def run_script_in(self, stdin_text, workdir, usage_url=NO_USAGE_URL):
        limits_out = workdir / "usage.json"
        sessions_out = workdir / "sessions.json"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=stdin_text,
            capture_output=True,
            text=True,
            env={
                "RUNCAT_OUT_FILE": str(limits_out),
                "RUNCAT_SESSIONS_OUT_FILE": str(sessions_out),
                "PATH": "/usr/bin:/bin",
                "HOME": str(workdir),
                "RUNCAT_USAGE_URL": usage_url,
            },
            check=True,
        )
        return Cards(
            json.loads(limits_out.read_text(encoding="utf-8")),
            json.loads(sessions_out.read_text(encoding="utf-8")),
            proc.stdout.strip(),
        )

    def rows(self, card):
        return {m["title"]: m for m in card["metrics"]}

    def titles(self, card):
        return [m["title"] for m in card["metrics"]]

    def only_session(self, cards):
        """セッションが 1 つである前提で、その行を返す。"""
        metrics = cards.sessions["metrics"]
        self.assertEqual(len(metrics), 1, f"セッション行が 1 つではない: {metrics}")
        return metrics[0]

    def run_hook(self, entries, event="Stop", workdir=None):
        """transcript JSONL を作り、hook 入力を流して 2 枚のカードを返す。"""
        if workdir is None:
            with tempfile.TemporaryDirectory() as tmpdir:
                return self.run_hook(entries, event, Path(tmpdir))
        workdir = Path(workdir)
        transcript = workdir / "transcript.jsonl"
        transcript.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8"
        )
        cards = self.run_script(json.dumps({
            "hook_event_name": event,
            "session_id": "abc123",
            "transcript_path": str(transcript),
            "cwd": "/Users/so/.setup",
        }), workdir=workdir)
        # hook の stdout は会話へ混ざり得るので何も出さない
        self.assertEqual(cards.stdout, "")
        return cards

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


class TwoCardsTest(ScriptTestCase):
    """性質の違う 2 つを 1 枚に詰めず、カードを分けて書き出す。"""

    def test_limits_and_sessions_go_to_separate_cards(self):
        now = int(time.time())
        payload = {
            "session_id": "s1",
            "model": {"display_name": "Opus 5"},
            "workspace": {"repo": {"name": "setup"}},
            "context_window": {"used_percentage": 21},
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": now + 9_660 + 30},
                "seven_day": {"used_percentage": 41.2, "resets_at": now + 273_600 + 30},
            },
        }
        cards = self.run_script(json.dumps(payload))

        # レート制限のカードにセッションは混ざらない
        self.assertEqual(self.titles(cards.limits), ["5h", "7d"])
        # セッションのカードにレート制限は混ざらない
        self.assertEqual(self.titles(cards.sessions), ["setup"])

    def test_each_card_has_its_own_title_and_symbol(self):
        """RunCat の設定でどちらのソースか見分けられるようにする。"""
        cards = self.run_script('{"session_id": "s1", "model": {"display_name": "Opus 5"}}')
        self.assertEqual(cards.limits["title"], "Claude Code")
        self.assertEqual(cards.sessions["title"], "Claude Sessions")
        self.assertNotEqual(cards.limits["symbol"], cards.sessions["symbol"])

    def test_both_cards_are_valid_custom_metrics_schema(self):
        cards = self.run_script('{"model": {"display_name": "Opus 4.8"}}')
        for card in (cards.limits, cards.sessions):
            self.assertIsInstance(card["title"], str)
            self.assertIsInstance(card["symbol"], str)
            self.assertIsInstance(card["metrics"], list)
            # lastUpdatedDate は RunCat が要求する ISO 8601 (UTC) 形式
            self.assertRegex(card["lastUpdatedDate"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            for metric in card["metrics"]:
                self.assertIsInstance(metric["title"], str)
                self.assertIsInstance(metric["formattedValue"], str)
                if "normalizedValue" in metric:
                    self.assertGreaterEqual(metric["normalizedValue"], 0.0)
                    self.assertLessEqual(metric["normalizedValue"], 1.0)

    def test_session_card_bar_shows_the_most_used_context(self):
        """メニューバーからは、圧縮が近いセッションがあるか分かるようにする。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for session_id, project, pct in (("s1", "setup", 21), ("s2", "divive", 68)):
                self.run_script(json.dumps({
                    "session_id": session_id,
                    "model": {"display_name": "Opus 5"},
                    "workspace": {"repo": {"name": project}},
                    "context_window": {"used_percentage": pct},
                }), workdir=tmpdir)
            cards = self.run_script(json.dumps({
                "session_id": "s2",
                "model": {"display_name": "Opus 5"},
                "workspace": {"repo": {"name": "divive"}},
                "context_window": {"used_percentage": 68},
            }), workdir=tmpdir)
            self.assertEqual(cards.sessions["metricsBarValue"], "68%")

    def test_empty_limits_still_writes_both_cards(self):
        """レート制限が取れなくても、セッションのカードは書き出す。"""
        cards = self.run_script('{"session_id": "s1", "workspace": {"repo": {"name": "setup"}}}')
        self.assertEqual(cards.limits["metrics"], [])
        self.assertEqual(self.titles(cards.sessions), ["setup"])


class StatusLineModeTest(ScriptTestCase):
    """ターミナルの statusLine から呼ばれる経路。"""

    def test_full_payload_renders_the_session_row(self):
        now = int(time.time())
        payload = {
            "session_id": "s1",
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
        cards = self.run_script(json.dumps(payload))
        limits = self.rows(cards.limits)

        self.assertEqual(cards.stdout, "Opus 4.8")
        self.assertEqual(limits["5h"]["formattedValue"], "23.5% · 2h41m left")
        self.assertEqual(limits["7d"]["formattedValue"], "41.2% · 3d4h left")
        # メニューバーへ出すのは週間制限の使用率 (整数へ丸める)
        self.assertEqual(cards.limits["metricsBarValue"], "41%")

        session = self.only_session(cards)
        self.assertEqual(session["title"], "setup")
        # 使用率 · モデルと effort · 経過。think/fast と API 時間は一覧では落とす
        self.assertEqual(session["formattedValue"], "31.4% · Opus 4.8 · xhigh · 1h15m")
        self.assertAlmostEqual(session["normalizedValue"], 0.3135)

    def test_empty_payload_still_renders_one_session(self):
        cards = self.run_script("{}")
        self.assertEqual(cards.stdout, "Claude Code")
        session = self.only_session(cards)
        # プロジェクト名もセッション名も無いときのラベル
        self.assertEqual(session["title"], "session")
        self.assertEqual(session["formattedValue"], "Claude Code")
        self.assertNotIn("metricsBarValue", cards.limits)

    def test_malformed_stdin_does_not_crash(self):
        for stdin_text in ("", "not json at all", "[1,2,3]", "null"):
            with self.subTest(stdin=stdin_text):
                cards = self.run_script(stdin_text)
                self.assertEqual(cards.stdout, "Claude Code")
                self.assertEqual(self.only_session(cards)["formattedValue"], "Claude Code")

    def test_wrong_types_are_ignored_instead_of_rendered(self):
        payload = {
            "model": "Opus 4.8",  # dict でなく文字列
            "context_window": {"used_percentage": None, "total_input_tokens": "x"},
            "cost": {"total_cost_usd": True},  # bool は数値扱いしない
            "rate_limits": None,
            "effort": {"level": None},
            "fast_mode": "yes",  # true でなければマーカーを出さない
        }
        cards = self.run_script(json.dumps(payload))
        self.assertEqual(self.titles(cards.sessions), ["session"])
        self.assertEqual(self.only_session(cards)["formattedValue"], "Claude Code")

    def test_expired_reset_and_over_limit_percentage(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 150, "resets_at": 1}}}
        cards = self.run_script(json.dumps(payload))
        row = self.rows(cards.limits)["5h"]
        self.assertEqual(row["formattedValue"], "150%")  # 過去のリセット時刻は添えない
        self.assertEqual(row["normalizedValue"], 1.0)  # バーは [0,1] にクランプ
        # 週間制限が無ければメニューバーの値も出さない (Context では代用しない)
        self.assertNotIn("metricsBarValue", cards.limits)

    def test_million_token_context_window(self):
        payload = {"context_window": {
            "used_percentage": 8.4, "total_input_tokens": 83_000,
            "total_output_tokens": 1_500, "context_window_size": 1_000_000,
        }}
        cards = self.run_script(json.dumps(payload))
        self.assertEqual(self.only_session(cards)["formattedValue"], "8.4% · Claude Code")

    def test_project_falls_back_to_directory_name(self):
        payload = {"workspace": {"project_dir": "/Users/so/foo/"}, "worktree": {"name": "my-feature"}}
        cards = self.run_script(json.dumps(payload))
        # 同名プロジェクトが他に無いので worktree 名は添えない
        self.assertEqual(self.only_session(cards)["title"], "foo")

    def test_zero_values_are_rendered_not_dropped(self):
        payload = {"context_window": {"used_percentage": 0}}
        cards = self.run_script(json.dumps(payload))
        session = self.only_session(cards)
        self.assertEqual(session["formattedValue"], "0% · Claude Code")
        self.assertEqual(session["normalizedValue"], 0.0)

    def test_output_files_are_replaced_atomically(self):
        """一時ファイルを残さず os.replace で置き換える (RunCat が半端な内容を読まないため)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            limits_out = Path(tmpdir) / "usage.json"
            sessions_out = Path(tmpdir) / "sessions.json"
            limits_out.write_text("stale", encoding="utf-8")
            subprocess.run(
                [sys.executable, str(SCRIPT)],
                input='{"model": {"display_name": "Opus 4.8"}}',
                capture_output=True, text=True, check=True,
                env={
                    "RUNCAT_OUT_FILE": str(limits_out),
                    "RUNCAT_SESSIONS_OUT_FILE": str(sessions_out),
                    "PATH": "/usr/bin:/bin", "HOME": tmpdir,
                    "RUNCAT_USAGE_URL": NO_USAGE_URL,
                },
            )
            self.assertEqual(
                json.loads(limits_out.read_text(encoding="utf-8"))["title"], "Claude Code")
            # 2 枚のカードとセッション状態だけが残る (書きかけの一時ファイルを残さない)
            self.assertEqual(
                sorted(p.name for p in Path(tmpdir).iterdir()),
                ["runcat-sessions", "sessions.json", "usage.json"])
            self.assertEqual(
                [p.suffix for p in (Path(tmpdir) / "runcat-sessions").iterdir()], [".json"])


class HookModeTest(ScriptTestCase):
    """hook から呼ばれる経路 (デスクトップアプリではこちらだけが動く)。"""

    def test_transcript_renders_the_session_row(self):
        cards = self.run_hook([
            {"type": "user", "timestamp": "2026-07-23T08:48:54.157Z", "cwd": "/Users/so/.setup"},
            {"type": "custom-title", "customTitle": "RunCat 連携"},
            {"type": "pr-link", "prNumber": 23, "prRepository": "shinyaoguri/setup"},
            self.assistant_entry(),
        ])
        session = self.only_session(cards)
        self.assertEqual(session["title"], "setup")
        # Opus は 1M 文脈。142,548 / 1M = 14.2548% → 小数第 1 位へ丸める
        self.assertEqual(session["formattedValue"], "14.3% · Opus 4.8 · high · 30m")
        # 使用量エンドポイントも控えも無いので、レート制限のカードは空になる
        self.assertEqual(cards.limits["metrics"], [])
        self.assertNotIn("metricsBarValue", cards.limits)

    def test_model_id_is_prettified(self):
        for model_id, expected in [
            ("claude-opus-4-8", "Opus 4.8"),
            ("claude-sonnet-5", "Sonnet 5"),
            ("claude-haiku-4-5-20251001", "Haiku 4.5"),  # 末尾の日付スナップショットは落とす
            ("claude-fable-5", "Fable 5"),
        ]:
            with self.subTest(model_id=model_id):
                cards = self.run_hook([self.assistant_entry(
                    message={"model": model_id, "usage": {}}, effort=None)])
                # エントリが 1 つだけなので経過は 0s。使用率は usage が空で出ない
                self.assertEqual(self.only_session(cards)["formattedValue"], f"{expected} · 0s")

    def test_worktree_cwd_falls_back_to_the_repository_name(self):
        """worktree の中では末尾が worktree 名になるので、元のリポジトリ名を出す。"""
        cards = self.run_hook([self.assistant_entry(
            cwd="/Users/so/Repos/myapp/.claude/worktrees/feat-something-250692",
            gitBranch="feat/something")])
        self.assertEqual(self.only_session(cards)["title"], "myapp")

    def test_sidechain_entries_are_ignored(self):
        """サブエージェント (isSidechain) の usage は本セッションの文脈量ではない。"""
        cards = self.run_hook([
            {"type": "user", "timestamp": "2026-07-23T08:48:54.157Z", "cwd": "/Users/so/.setup"},
            self.assistant_entry(),
            self.assistant_entry(isSidechain=True, message={
                "model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 5}}),
        ])
        self.assertEqual(
            self.only_session(cards)["formattedValue"], "14.3% · Opus 4.8 · high · 30m")

    def test_broken_transcript_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "transcript.jsonl"
            transcript.write_text(
                "not json\n\n" + json.dumps(self.assistant_entry()) + "\n{ truncated",
                encoding="utf-8",
            )
            cards = self.run_script(json.dumps({
                "hook_event_name": "Stop", "transcript_path": str(transcript)}))
            self.assertEqual(cards.stdout, "")
            self.assertIn("Opus 4.8", self.only_session(cards)["formattedValue"])

    def test_huge_transcript_reads_only_the_tail(self):
        """毎ツール呼び出しで走るため、巨大な transcript でも末尾しか読まない。"""
        # 末尾チャンクに載る行はセッション開始より後の時刻にして、
        # 先頭行を読まないと経過時間がずれるようにする
        filler = [{"type": "user", "timestamp": "2026-07-23T09:10:00.000Z", "text": "x" * 2000}
                  for _ in range(400)]  # 800KB 超 > TRANSCRIPT_TAIL_BYTES (256KB)
        start = {"type": "user", "timestamp": "2026-07-23T08:48:54.157Z", "cwd": "/Users/so/.setup"}
        cards = self.run_hook([start] + filler + [self.assistant_entry()])
        # 先頭行だけは別途読むので、セッション開始からの経過時間になる (末尾からなら 9m)
        value = self.only_session(cards)["formattedValue"]
        self.assertTrue(value.endswith("30m"), value)

    def test_missing_transcript_does_not_fail_the_hook(self):
        """hook を止めないため、transcript が読めなくても異常終了しない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            limits_out = Path(tmpdir) / "usage.json"
            sessions_out = Path(tmpdir) / "sessions.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=json.dumps({"hook_event_name": "Stop", "transcript_path": "/nonexistent.jsonl"}),
                capture_output=True, text=True,
                env={
                    "RUNCAT_OUT_FILE": str(limits_out),
                    "RUNCAT_SESSIONS_OUT_FILE": str(sessions_out),
                    "PATH": "/usr/bin:/bin", "HOME": tmpdir,
                    "RUNCAT_USAGE_URL": NO_USAGE_URL,
                },
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            # 既存のカードを壊さない (どちらも書かない)
            self.assertFalse(limits_out.exists())
            self.assertFalse(sessions_out.exists())


class RateLimitCacheTest(ScriptTestCase):
    """レート制限は hook 入力に無いため、statusLine で取れた値を控えて使い回す。"""

    CACHE_NAME = "runcat-rate-limits.json"

    def statusline_payload(self, five_hour=None, seven_day=None):
        limits = {}
        if five_hour is not None:
            limits["five_hour"] = five_hour
        if seven_day is not None:
            limits["seven_day"] = seven_day
        # run_hook と同じセッションとして扱わせる (statusLine と hook は同一セッションの
        # 別入口なので、session_id が揃っていないと 2 セッション分のカードになる)
        return json.dumps({
            "session_id": "abc123",
            "model": {"display_name": "Opus 4.8"},
            "rate_limits": limits,
        })

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

            cards = self.run_hook([self.assistant_entry()], workdir=tmpdir)
            limits = self.rows(cards.limits)
            # 控えは最後に取れた時点の値で実際はそれ以上なので、下限として ≥ を付ける
            self.assertEqual(limits["5h"]["formattedValue"], "≥23.5% · 2h41m left")
            self.assertEqual(limits["7d"]["formattedValue"], "≥41.2% · 3d4h left")
            self.assertEqual(cards.limits["metricsBarValue"], "≥41%")
            # 控えを使ってもセッションのカードは transcript 由来のまま
            self.assertIn("Opus 4.8", self.only_session(cards)["formattedValue"])

    def test_reset_windows_are_not_reused(self):
        """リセット済みのウィンドウの使用率はもう当てにならないので出さない。"""
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                five_hour={"used_percentage": 23.5, "resets_at": now - 60},
                seven_day={"used_percentage": 41.2, "resets_at": now + 273_600 + 30},
            ), workdir=tmpdir)
            limits = self.rows(self.run_hook([self.assistant_entry()], workdir=tmpdir).limits)
            self.assertNotIn("5h", limits)
            self.assertEqual(limits["7d"]["formattedValue"], "≥41.2% · 3d4h left")

    def test_windows_without_reset_time_are_not_cached(self):
        """リセット時刻が無いと古さを判定できないため控えない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                five_hour={"used_percentage": 23.5}), workdir=tmpdir)
            self.assertFalse((Path(tmpdir) / self.CACHE_NAME).exists())
            cards = self.run_hook([self.assistant_entry()], workdir=tmpdir)
            self.assertNotIn("5h", self.rows(cards.limits))

    def test_statusline_without_rate_limits_keeps_the_cache(self):
        """API キー利用などで制限が来ないターンがあっても、控えを消さない。"""
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload(
                seven_day={"used_percentage": 41.2, "resets_at": now + 273_600 + 30}), workdir=tmpdir)
            self.run_script('{"session_id": "abc123", "model": {"display_name": "Opus 4.8"}}',
                            workdir=tmpdir)
            self.assertEqual(self.cache(tmpdir)["seven_day"]["used_percentage"], 41.2)
            cards = self.run_hook([self.assistant_entry()], workdir=tmpdir)
            self.assertIn("7d", self.rows(cards.limits))

    def test_broken_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / self.CACHE_NAME).write_text("{ truncated", encoding="utf-8")
            cards = self.run_hook([self.assistant_entry()], workdir=tmpdir)
            self.assertIn("Opus 4.8", self.only_session(cards)["formattedValue"])
            self.assertNotIn("5h", self.rows(cards.limits))


class MultiSessionTest(ScriptTestCase):
    """セッションのカードは、同時に動いているものを畳んで並べる。"""

    SESSIONS_DIR = "runcat-sessions"

    def statusline_payload(self, session_id, project, used_percentage, duration_ms, branch=None,
                           model="Opus 5"):
        payload = {
            "session_id": session_id,
            "model": {"display_name": model},
            "workspace": {"repo": {"name": project}},
            "context_window": {"used_percentage": used_percentage},
            "cost": {"total_duration_ms": duration_ms},
        }
        if branch is not None:
            payload["workspace"]["git_worktree"] = branch
        return json.dumps(payload)

    def state_files(self, workdir):
        return sorted((Path(workdir) / self.SESSIONS_DIR).iterdir())

    def age_session(self, workdir, seconds):
        """状態ファイルの更新時刻を過去へずらす (TTL 判定は状態の中身で行う)。"""
        for path in self.state_files(workdir):
            state = json.loads(path.read_text(encoding="utf-8"))
            state["updated_at"] = state["updated_at"] - seconds
            path.write_text(json.dumps(state), encoding="utf-8")

    def test_every_live_session_gets_a_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload("s1", "setup", 21, 4_500_000), workdir=tmpdir)
            cards = self.run_script(
                self.statusline_payload("s2", "divive", 8, 180_000), workdir=tmpdir)
            rows = self.rows(cards.sessions)

            # セッションごとに 1 行へ畳む (使用率 · モデル · 経過)
            self.assertEqual(rows["setup"]["formattedValue"], "21% · Opus 5 · 1h15m")
            self.assertEqual(rows["divive"]["formattedValue"], "8% · Opus 5 · 3m")
            self.assertAlmostEqual(rows["setup"]["normalizedValue"], 0.21)

    def test_stale_sessions_are_dropped(self):
        """終了したセッションはカードに残さない (更新が途絶えたら畳む)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload("s1", "setup", 21, 4_500_000), workdir=tmpdir)
            self.age_session(tmpdir, 31 * 60)  # SESSION_TTL_SECONDS (30 分) 超え
            cards = self.run_script(
                self.statusline_payload("s2", "divive", 8, 180_000), workdir=tmpdir)
            self.assertEqual(self.titles(cards.sessions), ["divive"])

    def test_same_project_rows_are_told_apart_by_branch(self):
        """同じリポジトリの worktree を 2 つ開くとラベルが衝突するのでブランチを添える。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(
                self.statusline_payload("s1", "setup", 21, 60_000, branch="feat/a"), workdir=tmpdir)
            cards = self.run_script(
                self.statusline_payload("s2", "setup", 8, 60_000, branch="feat/b"), workdir=tmpdir)
            rows = self.rows(cards.sessions)
            self.assertIn("setup · feat/a", rows)
            self.assertIn("setup · feat/b", rows)

    def test_same_branch_rows_fall_back_to_the_session_name(self):
        """同じ worktree で 2 つ開くとブランチでも区別できないので、セッション名で分ける。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for session_id, title in (("s1", "レビューの続き"), ("s2", "テスト追加")):
                payload = json.loads(
                    self.statusline_payload(session_id, "setup", 21, 60_000, branch="main"))
                payload["session_name"] = title
                self.run_script(json.dumps(payload), workdir=tmpdir)
            cards = self.run_script(json.dumps(payload), workdir=tmpdir)
            self.assertEqual(sorted(self.titles(cards.sessions)), ["テスト追加", "レビューの続き"])

    def test_session_name_is_only_used_when_the_branch_collides(self):
        """ブランチで区別できるならセッション名は使わない (ラベルを短く保つ)。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            for session_id, branch in (("s1", "feat/a"), ("s2", "feat/b")):
                payload = json.loads(
                    self.statusline_payload(session_id, "setup", 21, 60_000, branch=branch))
                payload["session_name"] = "使わないはずの名前"
                self.run_script(json.dumps(payload), workdir=tmpdir)
            cards = self.run_script(json.dumps(payload), workdir=tmpdir)
            self.assertEqual(
                sorted(self.titles(cards.sessions)), ["setup · feat/a", "setup · feat/b"])

    def test_statusline_and_hook_share_one_row_per_session(self):
        """同じセッションの 2 つの入口が別行に割れない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload("abc123", "setup", 21, 60_000), workdir=tmpdir)
            self.run_hook([self.assistant_entry()], workdir=tmpdir)  # session_id は abc123
            self.assertEqual(len(self.state_files(tmpdir)), 1)

    def test_session_id_is_not_used_as_a_path(self):
        """session_id は外から来る値なので、そのままファイル名にしない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.run_script(self.statusline_payload("../../escape", "setup", 21, 60_000),
                            workdir=tmpdir)
            files = self.state_files(tmpdir)
            self.assertEqual([p.name for p in files], ["______escape.json"])


class DisplayWidthTest(ScriptTestCase):
    """カード幅は一番長い行に引きずられるため、ラベルと値を切り詰める。"""

    def width(self, text):
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)

    def test_long_labels_are_clipped(self):
        payload = {"workspace": {"repo": {"name": "global-claude-md-symlinks-250692"}}}
        cards = self.run_script(json.dumps(payload))
        # ラベルは 20 幅まで
        self.assertEqual(self.only_session(cards)["title"], "global-claude-md-sy…")
        self.assertLessEqual(self.width(self.only_session(cards)["title"]), 20)

    def test_long_values_are_clipped(self):
        payload = {
            "model": {"display_name": "Some Very Long Model Name Indeed"},
            "workspace": {"repo": {"name": "setup"}},
            "context_window": {"used_percentage": 21},
            "effort": {"level": "xhigh"},
        }
        cards = self.run_script(json.dumps(payload))
        value = self.only_session(cards)["formattedValue"]
        self.assertLessEqual(self.width(value), 32)
        self.assertTrue(value.endswith("…"), value)
        # 先頭の使用率は必ず読める
        self.assertTrue(value.startswith("21% · "), value)

    def test_full_width_labels_count_as_two(self):
        payload = {"session_name": "アドオンのプロダクション対応レビューと後片付け"}
        cards = self.run_script(json.dumps(payload))
        # プロジェクト名が無ければセッション名をラベルにする。全角は 2 幅 (20 幅 = 全角 10 文字)
        self.assertEqual(self.only_session(cards)["title"], "アドオンのプロダク…")
        self.assertLessEqual(self.width(self.only_session(cards)["title"]), 20)

    def test_short_values_are_left_alone(self):
        payload = {"workspace": {"repo": {"name": "setup"}}, "model": {"display_name": "Opus 5"}}
        cards = self.run_script(json.dumps(payload))
        self.assertEqual(self.only_session(cards)["title"], "setup")
        self.assertEqual(self.only_session(cards)["formattedValue"], "Opus 5")


class UsageApiTest(ScriptTestCase):
    """Claude Code の /usage と同じ使用量エンドポイントから制限を取る経路。

    statusLine が渡す rate_limits には five_hour / seven_day しか無く、
    モデル別の週間制限はここからしか取れない。
    """

    # ユーザーの実レスポンスから、行の材料になる部分だけを写したもの
    RESPONSE = {
        "five_hour": {"utilization": 0.0, "resets_at": "2026-07-28T09:50:00.712514+00:00"},
        "seven_day": {"utilization": 18.0, "resets_at": "2026-08-02T00:59:59.712531+00:00"},
        "seven_day_opus": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": 0,
             "resets_at": "2026-07-28T09:50:00.712514+00:00", "scope": None, "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": 18,
             "resets_at": "2026-08-02T00:59:59.712531+00:00", "scope": None, "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 10,
             "resets_at": "2026-08-02T00:59:59.712746+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
             "is_active": False},
        ],
    }

    def stub(self, workdir, payload):
        """使用量エンドポイントの代わりに読ませる file:// スタブを置く。"""
        path = Path(workdir) / "usage-response.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path.as_uri()

    def payload(self, session_id="s1", project="setup"):
        return json.dumps({
            "session_id": session_id,
            "model": {"display_name": "Opus 5"},
            "workspace": {"repo": {"name": project}},
            "context_window": {"used_percentage": 21},
        })

    def test_all_three_limits_are_rendered(self):
        """5 時間 / 週間 (全モデル) / 週間 (モデル別) の 3 本が出る。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            url = self.stub(tmpdir, self.RESPONSE)
            cards = self.run_script(self.payload(), workdir=tmpdir, usage_url=url)
            rows = self.rows(cards.limits)

            self.assertEqual(self.titles(cards.limits), ["5h", "7d", "7d Fable"])
            self.assertTrue(rows["5h"]["formattedValue"].startswith("0% · "),
                            rows["5h"]["formattedValue"])
            self.assertTrue(rows["7d"]["formattedValue"].startswith("18% · "),
                            rows["7d"]["formattedValue"])
            self.assertTrue(rows["7d Fable"]["formattedValue"].startswith("10% · "),
                            rows["7d Fable"]["formattedValue"])
            # 控えから読んだ値ではないので ≥ は付けない
            self.assertNotIn("≥", rows["7d"]["formattedValue"])
            # メニューバーは週間 (全モデル)
            self.assertEqual(cards.limits["metricsBarValue"], "18%")

    def test_scoped_label_comes_from_the_model_name(self):
        """モデル別の枠が増えても scope から拾える。"""
        response = dict(self.RESPONSE, limits=[
            {"kind": "weekly_scoped", "percent": 42,
             "resets_at": "2026-08-02T00:59:59+00:00",
             "scope": {"model": {"display_name": "Opus"}}},
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            url = self.stub(tmpdir, response)
            cards = self.run_script(self.payload(), workdir=tmpdir, usage_url=url)
            self.assertIn("7d Opus", self.rows(cards.limits))

    def test_unknown_kinds_are_skipped(self):
        """知らない種別や percent の無い行は捨てる (増えても壊れない)。"""
        response = dict(self.RESPONSE, limits=[
            {"kind": "weekly_all", "percent": 18, "resets_at": "2026-08-02T00:59:59+00:00"},
            {"kind": "brand_new_kind", "percent": 5},
            {"kind": "session"},  # percent が無い
            {"kind": "weekly_scoped", "percent": 7, "scope": {"model": {}}},  # 名前が無い
            "not a dict",
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            url = self.stub(tmpdir, response)
            cards = self.run_script(self.payload(), workdir=tmpdir, usage_url=url)
            self.assertEqual(self.titles(cards.limits), ["7d"])
            self.assertEqual(self.titles(cards.sessions), ["setup"])

    def test_response_is_cached_between_runs(self):
        """hook は毎ツール呼び出しで走るので、短い間隔では叩き直さない。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            url = self.stub(tmpdir, self.RESPONSE)
            self.run_script(self.payload(), workdir=tmpdir, usage_url=url)

            # 応答の中身を差し替えても、控えが新しいうちは読み直さない
            self.stub(tmpdir, {"limits": [
                {"kind": "weekly_all", "percent": 99,
                 "resets_at": "2026-08-02T00:59:59+00:00"}]})
            cards = self.run_script(self.payload(), workdir=tmpdir, usage_url=url)
            rows = self.rows(cards.limits)
            self.assertTrue(rows["7d"]["formattedValue"].startswith("18% · "),
                            rows["7d"]["formattedValue"])
            self.assertIn("7d Fable", rows)

    def test_stale_cache_is_marked_when_refresh_fails(self):
        """控えが古いまま再取得に失敗したら、下限として ≥ を付ける。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            url = self.stub(tmpdir, self.RESPONSE)
            self.run_script(self.payload(), workdir=tmpdir, usage_url=url)

            cache = Path(tmpdir) / "runcat-usage-limits.json"
            cached = json.loads(cache.read_text(encoding="utf-8"))
            cached["fetched_at"] -= 3600  # USAGE_MAX_AGE_SECONDS を大きく超えさせる
            cache.write_text(json.dumps(cached), encoding="utf-8")
            (Path(tmpdir) / "usage-response.json").unlink()  # 再取得は失敗する

            cards = self.run_script(self.payload(), workdir=tmpdir, usage_url=url)
            value = self.rows(cards.limits)["7d"]["formattedValue"]
            self.assertTrue(value.startswith("≥18%"), value)

    def test_usage_api_wins_over_the_statusline_values(self):
        """両方あるときは、モデル別まで取れる使用量エンドポイントを使う。"""
        now = int(time.time())
        payload = json.loads(self.payload())
        payload["rate_limits"] = {
            "five_hour": {"used_percentage": 99, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 88, "resets_at": now + 3600},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            url = self.stub(tmpdir, self.RESPONSE)
            cards = self.run_script(json.dumps(payload), workdir=tmpdir, usage_url=url)
            rows = self.rows(cards.limits)
            self.assertTrue(rows["7d"]["formattedValue"].startswith("18% · "),
                            rows["7d"]["formattedValue"])
            self.assertIn("7d Fable", rows)

    def test_falls_back_to_statusline_when_the_endpoint_is_unreachable(self):
        """エンドポイントが壊れても行が消えるだけで、他の値は出し続ける。"""
        now = int(time.time())
        payload = json.loads(self.payload())
        payload["rate_limits"] = {
            "seven_day": {"used_percentage": 41.2, "resets_at": now + 273_600 + 30},
        }
        cards = self.run_script(json.dumps(payload))  # 既定の届かない URL
        rows = self.rows(cards.limits)
        self.assertEqual(rows["7d"]["formattedValue"], "41.2% · 3d4h left")
        self.assertNotIn("7d Fable", rows)
        self.assertEqual(self.titles(cards.sessions), ["setup"])

    def test_broken_response_does_not_break_the_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "usage-response.json"
            path.write_text("{ truncated", encoding="utf-8")
            cards = self.run_script(
                self.payload(), workdir=tmpdir, usage_url=path.as_uri())
            self.assertEqual(cards.limits["metrics"], [])
            self.assertEqual(self.titles(cards.sessions), ["setup"])


class ContextWindowTest(ScriptTestCase):
    """hook 入力には文脈の上限が無いので、モデル名から引く。"""

    # 経過時間を既存テストと揃えるための開始エントリ (これが無いと 0s になる)
    START = {"type": "user", "timestamp": "2026-07-23T08:48:54.157Z", "cwd": "/Users/so/.setup"}

    def usage_entry(self, model, cache_read):
        return self.assistant_entry(message={"model": model, "usage": {
            "input_tokens": 2, "cache_creation_input_tokens": 1_970,
            "cache_read_input_tokens": cache_read, "output_tokens": 2_163,
        }})

    def test_claude_5_models_get_the_1m_window(self):
        """Opus / Sonnet / Fable 5 はいずれも 1M が既定 (ベータヘッダ不要)。"""
        for model, name in (("claude-opus-5", "Opus 5"), ("claude-sonnet-5", "Sonnet 5"),
                            ("claude-fable-5", "Fable 5")):
            with self.subTest(model=model):
                cards = self.run_hook([self.START, self.usage_entry(model, 411_465)])
                session = self.only_session(cards)
                # 415,600 / 1M = 41.56% → 小数第 1 位へ丸める
                self.assertEqual(session["formattedValue"], f"41.6% · {name} · high · 30m")
                self.assertAlmostEqual(session["normalizedValue"], 0.416)

    def test_haiku_gets_the_200k_window(self):
        """現行モデルで 200k なのは Haiku 4.5 だけ。"""
        cards = self.run_hook([self.START, self.usage_entry("claude-haiku-4-5-20251001", 138_413)])
        self.assertEqual(
            self.only_session(cards)["formattedValue"], "71.3% · Haiku 4.5 · high · 30m")

    def test_usage_above_the_known_window_does_not_exceed_100_percent(self):
        """表に無い上限のモデルでも 100% 超えの表示にはしない。"""
        cards = self.run_hook([self.START, self.usage_entry("claude-haiku-4-5", 411_465)])
        session = self.only_session(cards)
        self.assertTrue(session["formattedValue"].startswith("100% · "), session["formattedValue"])
        self.assertEqual(session["normalizedValue"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
