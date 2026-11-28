#!/usr/bin/env python3
"""claude/secret-output-guard.py のテスト。

フックの契約 (stdin の JSON → deny の JSON、あるいは無出力) をサブプロセス経由で検証する。
コマンドは文字列として判定するだけで、実際には何も実行しない。

    python3 claude/tests/secret_output_guard_test.py
"""

import json
import subprocess
import unittest
from pathlib import Path

from hookenv import clean_env

SCRIPT = Path(__file__).resolve().parent.parent / "secret-output-guard.py"

# gyazo-capture スキルの呼び出し口 (shinyaoguri/claude-plugins)。これが通らないと道具が使えない
GYAZO_UPLOAD = (
    'curl -s -F "access_token=$(secret-read "${GYAZO_TOKEN_REF:-gyazo}")" '
    '-F "imagedata=@motion.webp" -F "title=動き" https://upload.gyazo.com/api/upload'
)


def run(command, **env):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    result = subprocess.run(
        ["python3", str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=clean_env(**env),
        check=True,
    )
    return result.stdout.strip()


class Guard(unittest.TestCase):
    def assertPass(self, command, **env):
        self.assertEqual(run(command, **env), "", command)

    def assertDeny(self, command, fragment=None):
        out = run(command)
        self.assertTrue(out, f"止まるべき: {command}")
        output = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        reason = output["permissionDecisionReason"]
        self.assertIn("setup#291", reason)
        if fragment:
            self.assertIn(fragment, reason)
        return reason


class IncidentTest(Guard):
    """setup#291 で値を出したコマンドそのもの。"""

    def test_capture_of_stderr_with_multios(self):
        reason = self.assertDeny(
            'T=$(~/.setup/bin/secret-read "$REF" 2>&1 >/dev/null | head -2); echo "stderr: $T"',
            "パイプや複文",
        )
        # 止める理由に、原因 (zsh の MULTIOS) と正しい形の両方が書いてある
        self.assertIn("MULTIOS", reason)
        self.assertIn("secret-read --check", reason)


class AllowedFormTest(Guard):
    def test_gyazo_upload_passes(self):
        self.assertPass(GYAZO_UPLOAD)

    def test_absolute_path_and_stderr_discarded(self):
        self.assertPass(
            'code=$(curl -s -o up.json -w "%{http_code}" '
            '-F "access_token=$(~/.setup/bin/secret-read "$REF" 2>/dev/null)" https://u); echo "$code"'
        )

    def test_env_prefix_to_any_command(self):
        self.assertPass('GH_TOKEN=$(secret-read "$REF") gh api user')
        self.assertPass('MOKUME_TOKEN=$(secret-read "$REF") mokume publish')
        self.assertPass('env TOKEN=$(secret-read "$REF") ./upload.sh')

    def test_consumer_inside_pipeline(self):
        self.assertPass('curl -s -H "Authorization: Bearer $(secret-read "$REF")" https://x | jq .name')

    def test_process_substitution_to_consumer(self):
        self.assertPass('curl -s -H @<(secret-read "$REF") https://x')

    def test_quiet_options(self):
        for option in ("--check", "--help", "--warm"):
            self.assertPass(f"secret-read {option}")
        self.assertPass('secret-read --forget "$REF"')

    def test_mentions_in_text_are_not_calls(self):
        self.assertPass("gh pr create --body 'secret-read \"$REF\" の値を出さない'")
        self.assertPass('gh issue comment 1 --body "secret-read を使う"')
        self.assertPass("cat <<'EOF'\n$(secret-read R)\n`secret-read R`\nEOF")
        self.assertPass("# secret-read R\nls")
        self.assertPass("grep -n secret-read bin/secret-read")

    def test_unrelated_commands(self):
        self.assertPass("git log --oneline | head")
        self.assertPass("npm run stop && open http://localhost:3000")
        self.assertPass('ls *(.) ; echo "$HOME/x" $(date)')

    def test_disabled_by_switch(self):
        self.assertPass('secret-read "$REF"', CLAUDE_SECRET_OUTPUT_GUARD="0")


class DeniedFormTest(Guard):
    def test_bare_call(self):
        self.assertDeny('secret-read "$REF"', "そのまま")
        self.assertDeny('~/.setup/bin/secret-read --refresh "$REF"', "そのまま")

    def test_piped_even_when_only_counting(self):
        # 数えるだけでも、書き方次第で値が複製される (MULTIOS)
        self.assertDeny('secret-read "$REF" | wc -c', "そのまま")
        self.assertDeny('secret-read "$REF" >/dev/null | head', "そのまま")

    def test_redirect_inside_substitution(self):
        self.assertDeny('curl -d "t=$(secret-read "$REF" 2>&1)" https://x', "2>/dev/null")

    def test_standalone_assignment(self):
        self.assertDeny('T=$(secret-read "$REF")', "代入")
        self.assertDeny('T="$(secret-read "$REF")"; curl -d "t=$T" https://x', "代入")

    def test_argument_of_non_consumer(self):
        self.assertDeny('echo "$(secret-read "$REF")"', "`echo`")
        self.assertDeny('ls "$(secret-read "$REF")"', "`ls`")
        self.assertDeny('printf "%s" "$(secret-read "$REF")" | pbcopy', "`printf`")

    def test_nested_under_printer(self):
        self.assertDeny('echo "$(printf %s "$(secret-read "$REF")")"', "`printf`")

    def test_env_prefix_to_printer(self):
        self.assertDeny('TOKEN=$(secret-read "$REF") printenv', "printenv")
        self.assertDeny('TOKEN=$(secret-read "$REF") env', "env")
        self.assertDeny('TOKEN=$(secret-read "$REF") bash -x ./upload.sh', "-x")

    def test_curl_verbose(self):
        self.assertDeny('curl -sv -H "Authorization: Bearer $(secret-read "$REF")" https://x', "curl -v")
        self.assertDeny('curl --trace-ascii - -d "t=$(secret-read "$REF")" https://x', "curl -v")

    def test_xtrace(self):
        self.assertDeny('set -x; curl -d "t=$(secret-read "$REF")" https://x', "xtrace")
        self.assertDeny('set -o xtrace\ncurl -d "t=$(secret-read "$REF")" https://x', "xtrace")

    def test_backticks(self):
        self.assertDeny('curl -d "t=`secret-read R`" https://x', "バッククォート")

    def test_unquoted_heredoc_body(self):
        self.assertDeny('gh pr comment 1 --body-file - <<EOF\ntoken: $(secret-read R)\nEOF', "ヒアドキュメント")

    def test_redirect_target_and_parameter(self):
        self.assertDeny('curl https://x > "$(secret-read R)"')
        self.assertDeny('curl -d "${X:-$(secret-read R)}" https://x')

    def test_code_in_string(self):
        self.assertDeny("sh -c 'secret-read \"$REF\" | head'", "文字列")
        self.assertDeny("eval 'secret-read \"$REF\"'", "文字列")

    def test_other_value_readers(self):
        self.assertDeny("op read op://Automation/x/credential", "op read")
        self.assertDeny("op item get x --reveal --fields password", "op item get")
        self.assertDeny("security find-generic-password -a u -s svc -w", "security")
        self.assertPass('curl -d "t=$(op read op://a/b/c)" https://x')
        self.assertPass("op item get x --fields username")
        self.assertPass("security find-generic-password -a u -s svc")

    def test_unparsable_command_with_call(self):
        self.assertDeny('curl -d "t=$(secret-read R" https://x', "区切れず")


if __name__ == "__main__":
    unittest.main()
