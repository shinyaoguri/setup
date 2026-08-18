#!/usr/bin/env python3
"""claude/automode-guard.py のテスト。

フックの契約 (stdin の JSON → ask の JSON、あるいは無出力) をサブプロセス経由で検証する。
判定は編集前後の `.autoMode` の差分だけで決まるので、tempfile に settings.json を置いて
実際に編集を適用させる。

Edit のケースはインデントに依存しないよう、値の中に置いたマーカー文字列を差し替える形で
書く (整形が変わってもテストが壊れない)。マーカーが `autoMode` の中にあるか外にあるかで
期待が入れ替わるのが、このフックの判定そのもの。

    python3 claude/tests/automode_guard_test.py
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "automode-guard.py"


def settings(auto_mode=True, **overrides):
    data = {
        "permissions": {
            "defaultMode": "auto",
            "ask": ["Bash(gh repo delete:*)"],
            "allow": ["Bash(git status:*)"],
        },
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": ["HOOK-MARKER"]}]},
    }
    if auto_mode:
        data["autoMode"] = {
            "allow": ["$defaults", "ALLOW-MARKER"],
            "environment": ["$defaults", "ENV-MARKER"],
            "soft_deny": ["$defaults", "SOFT-MARKER"],
            "hard_deny": ["$defaults", "HARD-MARKER"],
        }
    data.update(overrides)
    return data


def dumped(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)
        self.path = self.root / "settings.json"
        self.path.write_text(dumped(settings()))

    # --- 実行 -------------------------------------------------------------

    def run_hook(self, tool, **tool_input):
        return subprocess.run(
            [str(SCRIPT)],
            input=json.dumps(
                {"tool_name": tool, "cwd": str(self.root), "tool_input": tool_input}
            ),
            capture_output=True,
            text=True,
            cwd=str(self.root),
            timeout=30,
        )

    def edit(self, old, new, path=None, **extra):
        return self.run_hook(
            "Edit",
            file_path=str(path or self.path),
            old_string=old,
            new_string=new,
            **extra,
        )

    def write(self, data, path=None):
        content = data if isinstance(data, str) else dumped(data)
        return self.run_hook("Write", file_path=str(path or self.path), content=content)

    def assert_allowed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "素通しのはずが出力があった")

    def assert_asked(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        decision = payload["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "ask", result.stdout)
        return payload["hookSpecificOutput"]["permissionDecisionReason"]

    # --- autoMode に差分が出る編集は ask ----------------------------------

    def test_entry_added_to_allow(self):
        reason = self.assert_asked(
            self.edit('"ALLOW-MARKER"', '"ALLOW-MARKER",\n"NEW ENTRY"')
        )
        self.assertIn("allow", reason)
        self.assertIn("2 件", reason)
        self.assertIn("3 件", reason)

    def test_entry_removed_from_hard_deny(self):
        data = settings()
        data["autoMode"]["hard_deny"] = ["$defaults"]
        reason = self.assert_asked(self.write(data))
        self.assertIn("hard_deny", reason)

    def test_defaults_dropped(self):
        data = settings()
        data["autoMode"]["soft_deny"] = ["SOFT-MARKER"]
        reason = self.assert_asked(self.write(data))
        self.assertIn("$defaults なし", reason)

    def test_entry_reworded_in_place(self):
        """件数も $defaults も変わらない文言の書き換えも拾う (弱化は機械に読めない)。"""
        reason = self.assert_asked(self.edit('"HARD-MARKER"', '"HARD-MARKER (softened)"'))
        self.assertIn("hard_deny", reason)

    def test_tightening_also_asks(self):
        """締める変更も同じく通る。緩めたか締めたかは判定しない。"""
        data = settings()
        data["autoMode"]["soft_deny"].append("EXTRA GUARD")
        self.assert_asked(self.write(data))

    def test_environment_entry_changed(self):
        self.assert_asked(self.edit('"ENV-MARKER"', '"ENV-MARKER plus a trusted host"'))

    def test_section_added(self):
        data = settings()
        del data["autoMode"]["allow"]
        self.path.write_text(dumped(data))
        reason = self.assert_asked(self.write(settings()))
        self.assertIn("無し", reason)

    def test_automode_introduced_by_write(self):
        """autoMode を持たなかったファイルに生やす編集も拾う。"""
        self.path.write_text(dumped(settings(auto_mode=False)))
        self.assert_asked(self.write(settings()))

    def test_new_file_created_with_automode(self):
        fresh = self.root / "settings.local.json"
        self.assert_asked(self.write(settings(), path=fresh))

    def test_multiedit_touching_automode(self):
        result = self.run_hook(
            "MultiEdit",
            file_path=str(self.path),
            edits=[
                {"old_string": '"HOOK-MARKER"', "new_string": '"HOOK-CHANGED"'},
                {"old_string": '"SOFT-MARKER"', "new_string": '"SOFT-CHANGED"'},
            ],
        )
        self.assert_asked(result)

    def test_replace_all_reaching_automode(self):
        """同じ語がファイル内の複数箇所にあり、replace_all で autoMode まで届く場合。"""
        data = settings()
        data["permissions"]["allow"].append("SHARED-MARKER")
        data["autoMode"]["allow"].append("SHARED-MARKER")
        self.path.write_text(dumped(data))
        self.assert_asked(
            self.edit('"SHARED-MARKER"', '"SHARED-CHANGED"', replace_all=True)
        )

    def test_edit_breaking_the_json(self):
        reason = self.assert_asked(self.edit('"autoMode": {', '"autoMode": ['))
        self.assertIn("JSON として読めなくなります", reason)

    # --- autoMode に差分が出ない編集は素通し -------------------------------

    def test_hooks_only_change(self):
        self.assert_allowed(self.edit('"HOOK-MARKER"', '"HOOK-CHANGED"'))

    def test_permissions_only_change(self):
        data = settings()
        data["permissions"]["allow"].append("Bash(gh pr checks:*)")
        self.assert_allowed(self.write(data))

    def test_reformatting_without_semantic_change(self):
        """整形だけの書き換え (インデントが変わっても値が同じ) は呼ばない。"""
        self.assert_allowed(
            self.write(json.dumps(settings(), indent=4, ensure_ascii=False))
        )

    def test_replace_all_not_reaching_automode(self):
        data = settings()
        data["permissions"]["allow"].append("SHARED-MARKER")
        self.path.write_text(dumped(data))
        self.assert_allowed(
            self.edit('"SHARED-MARKER"', '"SHARED-CHANGED"', replace_all=True)
        )

    def test_file_without_automode(self):
        plain = self.root / "package.json"
        plain.write_text(dumped({"name": "x", "autoModeish": ["$defaults"]}))
        self.assert_allowed(
            self.edit('"x"', '"y"', path=plain)
        )

    def test_non_json_file(self):
        source = self.root / "app.swift"
        source.write_text('let autoMode = ["$defaults"]\n')
        self.assert_allowed(self.edit("$defaults", "$nothing", path=source))

    def test_edit_that_does_not_match(self):
        """当たらない置換は Claude Code 側がエラーにする。判定を捏造しない。"""
        self.assert_allowed(self.edit('"NOT PRESENT"', '"anything"'))

    def test_notebook_edit_is_ignored(self):
        self.assert_allowed(
            self.run_hook("NotebookEdit", notebook_path=str(self.path), new_source="x")
        )

    def test_bash_is_ignored(self):
        self.assert_allowed(
            self.run_hook("Bash", command="jq '.autoMode.allow += [\"x\"]' settings.json")
        )

    def test_malformed_payload_is_ignored(self):
        result = subprocess.run(
            [str(SCRIPT)], input="not json", capture_output=True, text=True, timeout=30
        )
        self.assert_allowed(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
