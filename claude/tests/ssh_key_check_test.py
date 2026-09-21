#!/usr/bin/env python3
"""bin/ssh-key-check のテスト。

本物の ssh-add を呼ぶと Secretive の agent に署名させてしまい (壊れた鍵を再現するには
本物を壊すしかない)、本物の gh を呼ぶとネットワークとアカウントの状態に結果が左右される。
どちらも PATH の先頭に偽物を置いて差し替える (secret_read_test.py と同じ型)。

このスクリプトの肝は **4 つの完了条件のどれが欠けても名指しで言うこと** と、
**「壊れている」(ng) と「確かめられなかった」(判定不能) を混ぜないこと** の 2 点。
後者を混ぜると、gh のスコープ不足を「鍵が未登録」と読んで、打つべき手を取り違える。

    python3 claude/tests/ssh_key_check_test.py
"""

import os
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

SCRIPT = Path(__file__).resolve().parent.parent.parent / "bin" / "ssh-key-check"

KEY_TYPE = "ecdsa-sha2-nistp256"
KEY_BODY = "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBExampleKeyBodyForTests"
KEY_COMMENT = "test-key@secretive.test.local"
ECDSA_KEY = f"{KEY_TYPE} {KEY_BODY} {KEY_COMMENT}"
# GitHub は登録時のコメントを落として返すことがある。比較がそこで割れないこと
GITHUB_KEY = f"{KEY_TYPE} {KEY_BODY}"
OTHER_KEY = f"{KEY_TYPE} AAAASomebodyElsesKeyBody other@example.com"
MLDSA_KEY = f"ssh-mldsa65@openssh.com AAAAMLDSAKeyBody {KEY_COMMENT}"

# 偽 ssh-add。呼ばれた引数と、そのとき見えていた SSH_AUTH_SOCK をログに残す
# (**環境から socket を取っていないこと**の検証に使う)。
#
# FAKE_SSH_ADD_SIGN=hang は "Require Authentication" が付いたまま = agent が承認
# ダイアログを出したまま返らない状態の再現。**SIGALRM では死なない**作りにしてある:
# ここを素の sleep にすると `perl -e 'alarm N; exec'` だけの弱い実装が緑になってしまう
# (secret-read が実際に踏んだ穴。偽 op が本物より弱かった)。
FAKE_SSH_ADD = r"""#!/usr/bin/env bash
set -u
printf '%s SOCK=%s\n' "$*" "${SSH_AUTH_SOCK:-}" >> "$FAKE_SSH_ADD_LOG"
case "${1:-}" in
  -l)
    case "${FAKE_SSH_ADD_LIST:-ok}" in
      ok) echo "256 SHA256:AbC test-key (ECDSA)"; exit 0 ;;
      nokeys) echo "The agent has no identities."; exit 1 ;;
      *) echo "Error connecting to agent: No such file or directory" >&2; exit 2 ;;
    esac
    ;;
  -T)
    calls=$(grep -c '^-T ' "$FAKE_SSH_ADD_LOG")
    mode="${FAKE_SSH_ADD_SIGN:-ok}"
    case "$mode" in
      # 1 回目だけ遅い = agent の cold start。2 回目で判定される側
      first-slow:*) if [ "$calls" -le 1 ]; then mode="slow:${mode#first-slow:}"; else mode=ok; fi ;;
      # 2 回目だけ遅い = 温めても遅い (承認を求められた形)
      last-slow:*) if [ "$calls" -le 1 ]; then mode=ok; else mode="slow:${mode#last-slow:}"; fi ;;
    esac
    case "$mode" in
      ok) echo "Good signature"; exit 0 ;;
      slow:*) sleep "${mode#slow:}"; echo "Good signature"; exit 0 ;;
      hang) exec perl -e '$SIG{ALRM} = "IGNORE"; sleep 30' ;;
      *) echo "agent refused operation" >&2; exit 1 ;;
    esac
    ;;
esac
exit 2
"""

# 偽 gh。スクリプトは `gh api --paginate <endpoint> --jq '.[].key'` の形でしか呼ばないので、
# jq を通した後の「鍵の行」を返す。スコープ不足 (404) は本物の文面に寄せる —
# **これを「未登録」と取り違えないこと**が検査したいことの半分なので、印が要る。
FAKE_GH = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
[ "${1:-}" = "api" ] || exit 2
endpoint=""
for arg in "$@"; do
  case "$arg" in /user/*) endpoint="$arg" ;; esac
done
case "$endpoint" in
  /user/keys) mode="${FAKE_GH_AUTH:-ok}"; file="${FAKE_GH_AUTH_FILE:-}" ;;
  /user/ssh_signing_keys) mode="${FAKE_GH_SIGNING:-ok}"; file="${FAKE_GH_SIGNING_FILE:-}" ;;
  *) exit 2 ;;
esac
case "$mode" in
  ok) [ -z "$file" ] || cat "$file"; exit 0 ;;
  scope)
    echo 'gh: Not Found (HTTP 404)' >&2
    echo 'gh: This API operation needs the "admin:ssh_signing_key" scope. To request it, run:  gh auth refresh -h github.com -s admin:ssh_signing_key' >&2
    exit 1
    ;;
  hang) exec perl -e '$SIG{ALRM} = "IGNORE"; sleep 30' ;;
  *) echo "gh: could not connect" >&2; exit 1 ;;
esac
"""


def bind_agent_socket(data_dir):
    """SecretAgent が常駐している状態 (= socket が在ること) を作り、socket を返す。

    検査器は繋ぐ前に `[ -S ]` を見るので、実際に bind した socket が要る (普通の
    ファイルでは「常駐していない」側に倒れる)。

    bind は絶対パスでは通らない — AF_UNIX のパス長は 104 文字で、一時ディレクトリと
    Secretive のコンテナのパスだけで超える。短い相対パスで bind するために cwd を移す。
    """
    here = os.getcwd()
    try:
        os.chdir(data_dir)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind("socket.ssh")
        return sock
    finally:
        os.chdir(here)


def install_fake(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class SshKeyCheckTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

        self.home = self.root / "home"
        self.data = self.home / "Library/Containers/com.maxgoedjen.Secretive.SecretAgent/Data"
        self.public_keys = self.data / "PublicKeys"
        self.public_keys.mkdir(parents=True)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ssh_add_log = self.root / "ssh-add.log"
        self.ssh_add_log.write_text("")
        self.gh_log = self.root / "gh.log"
        self.gh_log.write_text("")

        self.install(self.bin / "ssh-add", FAKE_SSH_ADD)
        self.install(self.bin / "gh", FAKE_GH)

        self.place_key(ECDSA_KEY)
        self.start_agent()
        self.set_github_keys(auth=[GITHUB_KEY], signing=[GITHUB_KEY])

    # --- 状況を組み立てるヘルパー -------------------------------------------
    def install(self, path, body):
        install_fake(path, body)

    def place_key(self, *keys):
        for existing in self.public_keys.glob("*.pub"):
            existing.unlink()
        for index, key in enumerate(keys):
            (self.public_keys / f"{index:032x}.pub").write_text(key + "\n")

    def start_agent(self):
        self.addCleanup(bind_agent_socket(self.data).close)

    def set_github_keys(self, auth=None, signing=None):
        self.auth_keys = self.root / "github-auth-keys"
        self.signing_keys = self.root / "github-signing-keys"
        self.auth_keys.write_text("".join(f"{k}\n" for k in (auth or [])))
        self.signing_keys.write_text("".join(f"{k}\n" for k in (signing or [])))

    def run_script(self, *args, sign="ok", agent="ok", gh_auth="ok", gh_signing="ok",
                   limit=3, fast=500, gh_limit=1, with_gh=True):
        env = clean_env(HOME=str(self.home))
        env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        env["FAKE_SSH_ADD_LOG"] = str(self.ssh_add_log)
        env["FAKE_SSH_ADD_SIGN"] = sign
        env["FAKE_SSH_ADD_LIST"] = agent
        env["FAKE_GH_LOG"] = str(self.gh_log)
        env["FAKE_GH_AUTH"] = gh_auth
        env["FAKE_GH_SIGNING"] = gh_signing
        env["FAKE_GH_AUTH_FILE"] = str(self.auth_keys)
        env["FAKE_GH_SIGNING_FILE"] = str(self.signing_keys)
        env["SSH_KEY_CHECK_LIMIT"] = str(limit)
        env["SSH_KEY_CHECK_FAST"] = str(fast)
        env["SSH_KEY_CHECK_GH_LIMIT"] = str(gh_limit)
        # 環境の SSH_AUTH_SOCK を見ていないことを見るための囮。これを見ていると
        # 別の agent を検査して緑になる (SSH 越しのセッションで実際に起きる形)
        env["SSH_AUTH_SOCK"] = str(self.root / "decoy.sock")
        if not with_gh:
            (self.bin / "gh").unlink()
        return subprocess.run(
            [str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            preexec_fn=os.setsid,
        )

    def sign_probes(self):
        return [ln for ln in self.ssh_add_log.read_text().splitlines() if ln.startswith("-T ")]

    def gh_calls(self):
        return [ln for ln in self.gh_log.read_text().splitlines() if ln]

    def assertReports(self, result, label, state):
        """検査 1 行が期待どおりの状態で出ていること。"""
        for line in result.stdout.splitlines():
            if line.strip().startswith(label + " —"):
                self.assertIn(state, line, f"{label} の状態が違う: {line}")
                return
        self.fail(f"{label} の行が出ていない:\n{result.stdout}")


class LocalChecksTest(SshKeyCheckTestCase):
    """このマシンの中だけで分かること (鍵の選択・agent・鍵タイプ・承認要求)。"""

    def test_全部揃っていれば緑で終わる(self):
        result = self.run_script("--local")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("問題なし", result.stdout)

    def test_鍵が無ければ_ng(self):
        self.place_key()
        result = self.run_script("--local")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "公開鍵", ".pub が無い")

    def test_鍵が_2_本あれば_ng(self):
        """どれで署名するか決まらない。以前の `head -1` はここで任意の 1 本を拾っていた。"""
        self.place_key(ECDSA_KEY, OTHER_KEY)
        result = self.run_script("--local")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "公開鍵", "2 本ある")

    def test_鍵が決まらなければ他の検査は判定不能(self):
        """見る鍵が決まらないのに「鍵タイプが違う」とは言えない。"""
        self.place_key(ECDSA_KEY, OTHER_KEY)
        result = self.run_script("--local")
        self.assertReports(result, "鍵タイプ", "判定不能")
        self.assertReports(result, "承認なしで署名できる", "判定不能")

    def test_agent_が応答しなければ_ng(self):
        result = self.run_script("--local", agent="noagent")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "SecretAgent", "応答しない")

    def test_agent_が鍵を持っていなければ_ng(self):
        result = self.run_script("--local", agent="nokeys")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "SecretAgent", "鍵を 1 本も持っていない")

    def test_socket_が無ければ_ng(self):
        (self.data / "socket.ssh").unlink()
        result = self.run_script("--local")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "SecretAgent", "socket が無い")

    def test_agent_が応答しないときは署名を試さない(self):
        """居ない agent に上限いっぱい待つのは無駄で、時間切れを承認要求と誤読できる。"""
        result = self.run_script("--local", agent="noagent")
        self.assertReports(result, "承認なしで署名できる", "判定不能")
        self.assertEqual(self.sign_probes(), [], "agent が居ないのに署名を試した")

    def test_鍵タイプが_ML_DSA_なら_ng_で名指しする(self):
        """`.pub` は普通にできるので、ここを見ないと署名設定まで入って実行時に落ちる。"""
        self.place_key(MLDSA_KEY)
        result = self.run_script("--local")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "鍵タイプ", "ssh-mldsa65@openssh.com")
        self.assertIn("ecdsa-sha2-nistp256", result.stdout, "作り直す先を言っていない")

    def test_承認を求められると_ng(self):
        """Require Authentication が付いたまま = 署名が返ってこない。"""
        result = self.run_script("--local", sign="hang", limit=1)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "承認なしで署名できる", "返らなかった")
        self.assertIn("Require Authentication", result.stdout)

    def test_時間切れなら_2_回目を試さない(self):
        """温め直しても承認は要る。待ち時間を倍にする意味がない。"""
        self.run_script("--local", sign="hang", limit=1)
        self.assertEqual(len(self.sign_probes()), 1, "時間切れの後も probe を打った")

    def test_署名できても遅ければ_ng(self):
        """人が反射で Touch ID を押した形。通ったこと自体を ok にしない。"""
        result = self.run_script("--local", sign="last-slow:1", limit=3, fast=500)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "承認なしで署名できる", "署名できたが")

    def test_1_回目だけ遅いのは_ok(self):
        """Secure Enclave の cold start を承認要求と取り違えない (判定は 2 回目)。"""
        result = self.run_script("--local", sign="first-slow:1", limit=3, fast=500)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertReports(result, "承認なしで署名できる", "承認を求められずに")

    def test_署名が拒否されたら_ng_ではなく判定不能(self):
        """agent が 1 で断るのは承認要求とは別の壊れ方。打つ手が違う。"""
        result = self.run_script("--local", sign="refuse")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertReports(result, "承認なしで署名できる", "判定不能")

    def test_承認の上限は外から緩められない(self):
        """ここを外から大きくできると、付いたままでも ok になる = 検査を消すスイッチ。"""
        result = self.run_script("--local", sign="last-slow:3", limit=5, fast=99999)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "承認なしで署名できる", "署名できたが")

    def test_agent_は環境ではなく_Secretive_の_socket_を見る(self):
        """SSH 越しのセッションでは環境が転送された agent を指す。信じると別物を検査する。"""
        self.run_script("--local")
        socks = {ln.split("SOCK=", 1)[1] for ln in self.ssh_add_log.read_text().splitlines()}
        self.assertEqual(socks, {str(self.data / "socket.ssh")})

    def test_local_は_gh_を呼ばない(self):
        self.run_script("--local")
        self.assertEqual(self.gh_calls(), [], "--local がネットワークを使った")


class GithubChecksTest(SshKeyCheckTestCase):
    """GitHub 側の登録。**gh の失敗を「未登録」と取り違えないこと**が肝。"""

    def test_両方登録されていれば緑(self):
        result = self.run_script("--github")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_authentication_key_が無ければ_ng(self):
        self.set_github_keys(auth=[OTHER_KEY], signing=[GITHUB_KEY])
        result = self.run_script("--github")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "GitHub authentication key", "一覧に無い")

    def test_signing_key_が無ければ_ng(self):
        self.set_github_keys(auth=[GITHUB_KEY], signing=[])
        result = self.run_script("--github")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertReports(result, "GitHub signing key", "一覧に無い")

    def test_比較はコメントを無視する(self):
        """GitHub 側はコメントを落として返す。ここで割れると常に「未登録」になる。"""
        self.set_github_keys(auth=[GITHUB_KEY], signing=[GITHUB_KEY])
        result = self.run_script("--github")
        self.assertReports(result, "GitHub authentication key", "登録済み")

    def test_スコープ不足は_ng_ではなく判定不能(self):
        """404 を「未登録」と読むと、`gh auth refresh` と言うべき場面で登録し直させる。"""
        result = self.run_script("--github", gh_signing="scope")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertReports(result, "GitHub signing key", "スコープが足りない")
        self.assertIn("gh auth refresh -h github.com -s admin:ssh_signing_key", result.stdout)

    def test_gh_が無ければ判定不能(self):
        result = self.run_script("--github", with_gh=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertReports(result, "GitHub authentication key", "判定不能")

    def test_ネットワークが遅くても止まらない(self):
        """captive portal の下で playbook を止めない。判定不能に倒れて案内だけ出す。"""
        result = self.run_script("--github", gh_auth="hang", gh_signing="fail")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertReports(result, "GitHub authentication key", "判定不能")

    def test_github_は署名を試さない(self):
        self.run_script("--github")
        self.assertEqual(self.sign_probes(), [], "--github が agent に署名させた")


class PrintKeyTest(SshKeyCheckTestCase):
    """tasks/git.yml が使う鍵を返す口。検査器と ansible が同じ鍵を見るための 1 箇所。"""

    def test_鍵が_1_本なら公開鍵の行を返す(self):
        result = self.run_script("--print-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), ECDSA_KEY)

    def test_鍵が決まらなければ何も返さない(self):
        self.place_key(ECDSA_KEY, OTHER_KEY)
        result = self.run_script("--print-key")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_鍵が無ければ何も返さない(self):
        self.place_key()
        result = self.run_script("--print-key")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_値以外を出さない(self):
        """ansible がそのまま `~/.ssh/git_signing_key.pub` へ書くので、診断行が混ざると壊れる。"""
        result = self.run_script("--print-key")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)


class ExitCodeTest(SshKeyCheckTestCase):
    """終了コードは 3 値 + 使い方の誤り。呼び出し側がこれで倒し方を決める。"""

    def test_ng_と判定不能が混ざれば_1(self):
        """壊れているものが 1 つでもあるなら、確かめられなかったものより重い。"""
        self.place_key(MLDSA_KEY)
        result = self.run_script(gh_signing="scope")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_判定不能だけなら_2(self):
        result = self.run_script(gh_signing="scope")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_使い方の誤りは_1_でも_2_でもない(self):
        """打ち間違いが「鍵が壊れている」と読まれると、署名設定が黙って飛ぶ。"""
        result = self.run_script("--local", "extra")
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        result = self.run_script("--no-such-option")
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)

    def test_help_は_0(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--print-key", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
