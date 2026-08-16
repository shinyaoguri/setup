#!/usr/bin/env python3
"""bin/secret-read のテスト。

本物の op を呼ぶと 1Password の承認プロンプトが出てしまい、本物の security を呼ぶと
実際の Keychain を汚す。どちらも PATH の先頭に偽物を置いて差し替える (provisioning
preflight のテストと同じ型)。検証したいのは「どの参照をキャッシュしてよいと判断し、
いつ op を呼ばずに済ませるか」であって、op や security の挙動ではない。

このスクリプトの肝は **op を呼ばずに値が返せること** (= 1Password がロックされていても
無人セッションが止まらない) と、**許可リストに無い参照を Keychain に残さないこと**
(= 等級の線引き) の 2 点なので、そこを厚く見る。

    python3 claude/tests/secret_read_test.py
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent.parent / "bin" / "secret-read"

GYAZO_REF = "op://Automation/Gyazo API/credential"
SSH_REF = "op://Private/SSH Key/private key"

# 偽 security。実際の Keychain の代わりにディレクトリへ書く。service 名はスラッシュを
# 含む (op:// の参照そのもの) のでハッシュにしてファイル名にする。
FAKE_SECURITY = r"""#!/usr/bin/env bash
set -u
cmd="$1"; shift
svc=""; want_w=0
while [ $# -gt 0 ]; do
  case "$1" in
    -s) svc="$2"; shift 2 ;;
    -a) shift 2 ;;
    -l|-j) shift 2 ;;
    -w) want_w=1; shift ;;
    -U) shift ;;
    *) shift ;;
  esac
done
key="$FAKE_KEYCHAIN/$(printf '%s' "$svc" | shasum | cut -d' ' -f1)"
case "$cmd" in
  add-generic-password)
    read -r first || exit 1
    read -r second || exit 1
    [ "$first" = "$second" ] || exit 1
    printf '%s' "$first" > "$key"
    ;;
  find-generic-password)
    [ -f "$key" ] || exit 44
    [ "$want_w" -eq 1 ] && cat "$key"
    ;;
  delete-generic-password)
    [ -f "$key" ] || exit 44
    rm -f "$key"
    ;;
  *) exit 2 ;;
esac
exit 0
"""

# 偽 op。呼ばれた回数を数えられるようログを残す。
FAKE_OP = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_OP_LOG"
if [ -n "${FAKE_OP_FAIL:-}" ]; then
  echo "[ERROR] could not read secret: item not found" >&2
  exit 1
fi
[ "${1:-}" = "read" ] || exit 2
cat "$FAKE_OP_VALUE_FILE"
"""


class SecretReadTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.keychain = self.root / "keychain"
        self.keychain.mkdir()
        self.op_log = self.root / "op.log"
        self.op_log.write_text("")
        self.value_file = self.root / "value"
        self.set_op_value("gyazo-token-abc")

        self.allowlist = self.root / "secret-cache-allowlist"
        self.allowlist.write_text(f"# コメント行\n\n{GYAZO_REF}\n")

        self.install(self.bin / "security", FAKE_SECURITY)
        self.install(self.bin / "op", FAKE_OP)

    def install(self, path, body):
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def set_op_value(self, value):
        self.value_file.write_text(value + "\n")

    def run_script(self, *args, with_op=True, op_fails=False):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        env["SECRET_CACHE_ALLOWLIST"] = str(self.allowlist)
        env["FAKE_KEYCHAIN"] = str(self.keychain)
        env["FAKE_OP_LOG"] = str(self.op_log)
        env["FAKE_OP_VALUE_FILE"] = str(self.value_file)
        if op_fails:
            env["FAKE_OP_FAIL"] = "1"
        if not with_op:
            # op を PATH から外す = 1Password が使えない状況の再現
            (self.bin / "op").unlink()
        return subprocess.run(
            [str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
        )

    def op_calls(self):
        return [line for line in self.op_log.read_text().splitlines() if line]

    def cached_entries(self):
        return list(self.keychain.iterdir())

    # --- キャッシュしてよい参照 ---

    def test_初回は_op_を呼んで値を返しキャッシュする(self):
        result = self.run_script(GYAZO_REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")
        self.assertEqual(len(self.op_calls()), 1)
        self.assertEqual(len(self.cached_entries()), 1)

    def test_2回目は_op_を呼ばずにキャッシュから返す(self):
        self.run_script(GYAZO_REF)
        # 1Password 側の値を変えても、キャッシュを見ている限り影響を受けないはず
        self.set_op_value("changed-in-1password")
        result = self.run_script(GYAZO_REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")
        self.assertEqual(len(self.op_calls()), 1, "2 回目に op を呼んでいる")

    def test_op_が無くてもキャッシュがあれば読める(self):
        """本命の性質。1Password がロック / 不在でも無人セッションが止まらないこと。"""
        self.run_script(GYAZO_REF)
        result = self.run_script(GYAZO_REF, with_op=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")

    def test_op_もキャッシュも無ければ理由を言って失敗する(self):
        result = self.run_script(GYAZO_REF, with_op=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("op", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_改行や記号を含む値も壊さず往復する(self):
        secret = "line1\nline2 with spaces\tand=symbols/+"
        self.set_op_value(secret)
        first = self.run_script(GYAZO_REF)
        second = self.run_script(GYAZO_REF, with_op=False)
        self.assertEqual(first.stdout, secret + "\n")
        self.assertEqual(second.stdout, secret + "\n", "キャッシュ往復で値が変わった")

    # --- キャッシュしてはいけない参照 ---

    def test_許可リストに無い参照は_Keychain_に残さない(self):
        result = self.run_script(SSH_REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")  # 偽 op は同じ値を返す
        self.assertEqual(self.cached_entries(), [], "等級の違う秘密がキャッシュされた")

    def test_許可リストに無い参照は毎回_op_を呼ぶ(self):
        self.run_script(SSH_REF)
        self.run_script(SSH_REF)
        self.assertEqual(len(self.op_calls()), 2)

    def test_コメント行や空行は許可リストとして数えない(self):
        self.allowlist.write_text(f"#{GYAZO_REF}\n\n   \n")
        self.run_script(GYAZO_REF)
        self.assertEqual(self.cached_entries(), [], "コメント行を許可として読んでいる")

    def test_許可リストの前後の空白は無視する(self):
        self.allowlist.write_text(f"  {GYAZO_REF}  \n")
        self.run_script(GYAZO_REF)
        self.assertEqual(len(self.cached_entries()), 1)

    # --- 入れ替えと後始末 ---

    def test_refresh_は_op_から取り直して上書きする(self):
        self.run_script(GYAZO_REF)
        self.set_op_value("rotated-token")
        refreshed = self.run_script("--refresh", GYAZO_REF)
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertEqual(refreshed.stdout, "rotated-token\n")
        # 以後はキャッシュも新しい値になっている
        self.assertEqual(self.run_script(GYAZO_REF, with_op=False).stdout, "rotated-token\n")

    def test_forget_でキャッシュが消える(self):
        self.run_script(GYAZO_REF)
        result = self.run_script("--forget", GYAZO_REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cached_entries(), [])

    def test_forget_はキャッシュが無くても失敗しない(self):
        result = self.run_script("--forget", GYAZO_REF)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refresh_はキャッシュが無い状態からでも通る(self):
        result = self.run_script("--refresh", GYAZO_REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")
        self.assertEqual(len(self.cached_entries()), 1)

    # --- 誤用と失敗 ---

    def test_op_が失敗したら値を出さず非ゼロで終わる(self):
        result = self.run_script(GYAZO_REF, op_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.cached_entries(), [], "失敗した値をキャッシュしている")

    def test_op_参照でない引数は_op_を呼ぶ前に弾く(self):
        result = self.run_script("/etc/passwd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("op://", result.stderr)
        self.assertEqual(self.op_calls(), [], "検証前に op を呼んでいる")

    def test_引数なしは使い方を出して非ゼロ(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret-read", result.stderr)

    def test_知らないオプションは弾く(self):
        result = self.run_script("--nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.op_calls(), [])

    def test_参照を2つ渡したら弾く(self):
        result = self.run_script(GYAZO_REF, SSH_REF)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.op_calls(), [])

    # --- 状態の確認 ---

    def test_check_は値を出さない(self):
        self.run_script(GYAZO_REF)
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(GYAZO_REF, result.stdout)
        self.assertIn("キャッシュ済み", result.stdout)
        self.assertNotIn("gyazo-token-abc", result.stdout, "--check が値を漏らしている")

    def test_check_は未キャッシュを見分ける(self):
        result = self.run_script("--check")
        self.assertIn("未キャッシュ", result.stdout)

    def test_check_は許可リストの不正な行を指摘する(self):
        self.allowlist.write_text("not-a-reference\n")
        result = self.run_script("--check")
        self.assertIn("不正", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
