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

import base64
import os
import stat
import subprocess
import tempfile
import time
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
#
# FAKE_OP_HANG は「1Password がロックされていて、承認ダイアログを出したまま返らない」の
# 再現。本物に寄せるための条件が 3 つある:
#
#   1. SIGALRM では死なない — op は Go 製で、alarm(2) 頼みのタイムアウトは効かない。
#      ここを素の `sleep` にすると本物では通らない実装が緑になってしまう (実際になった)
#   2. SIGTERM / SIGKILL では死ぬ — 諦める側はここまでやる必要がある
#   3. 子プロセスを持たない — 親だけ殺しても子が stdout を掴んだままだとコマンド置換が
#      返らない。exec で置き換えて、殺す相手を 1 つに保つ
FAKE_OP = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_OP_LOG"
if [ -n "${FAKE_OP_HANG:-}" ]; then
  exec perl -e '$SIG{ALRM} = "IGNORE"; sleep shift' "$FAKE_OP_HANG"
fi
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

    def run_script(
        self,
        *args,
        with_op=True,
        op_fails=False,
        op_hangs=None,
        ttl=None,
        refresh_timeout=None,
    ):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        env["SECRET_CACHE_ALLOWLIST"] = str(self.allowlist)
        env["FAKE_KEYCHAIN"] = str(self.keychain)
        env["FAKE_OP_LOG"] = str(self.op_log)
        env["FAKE_OP_VALUE_FILE"] = str(self.value_file)
        if op_fails:
            env["FAKE_OP_FAIL"] = "1"
        if op_hangs is not None:
            env["FAKE_OP_HANG"] = str(op_hangs)
        if ttl is not None:
            env["SECRET_CACHE_TTL"] = str(ttl)
        if refresh_timeout is not None:
            env["SECRET_CACHE_REFRESH_TIMEOUT"] = str(refresh_timeout)
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

    # --- キャッシュの中身を直接いじるためのヘルパー ---
    #
    # 寿命の検証で実時間を待つとテストが遅く不安定になるので、Keychain 側の最終取得時刻を
    # 過去へずらして「古くなった状態」を作る。偽 security と同じ規則でファイル名を決める。

    def keychain_path(self, ref):
        digest = subprocess.run(
            ["shasum"], input=ref, capture_output=True, text=True
        ).stdout.split()[0]
        return self.keychain / digest

    def age_cache(self, ref, seconds):
        """キャッシュの最終取得時刻を seconds 秒だけ過去へずらす。"""
        path = self.keychain_path(ref)
        epoch, _, encoded = path.read_text().partition(":")
        path.write_text(f"{int(epoch) - seconds}:{encoded}")

    def write_legacy_cache(self, ref, value):
        """時刻を持たない旧形式 (base64 のみ) のキャッシュを置く。"""
        encoded = base64.b64encode(value.encode()).decode()
        self.keychain_path(ref).write_text(encoded)

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

    # --- キャッシュの寿命 ---
    #
    # 正本は 1Password 側なので、古くなったキャッシュは黙って正本を裏切る。ローテートに
    # 自動で追いつくこと、そのために可用性 (ロック中でも読める) を犠牲にしないことの 2 つを
    # 対で見る。

    def test_寿命を過ぎたら_op_から取り直して新しい値を返す(self):
        self.run_script(GYAZO_REF, ttl=100)
        self.set_op_value("rotated-token")
        self.age_cache(GYAZO_REF, 200)

        result = self.run_script(GYAZO_REF, ttl=100)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "rotated-token\n")

    def test_取り直したら次の呼び出しでは_op_を呼ばない(self):
        """取り直しで時刻が進むこと。進まなければ毎回 op を叩きに行ってしまう。"""
        self.run_script(GYAZO_REF, ttl=100)
        self.age_cache(GYAZO_REF, 200)
        self.run_script(GYAZO_REF, ttl=100)
        calls_after_refresh = len(self.op_calls())

        self.run_script(GYAZO_REF, ttl=100)
        self.assertEqual(len(self.op_calls()), calls_after_refresh)

    def test_取り直しに失敗しても古い値で動き続ける(self):
        """本命の性質。1Password が閉じていても呼び出し側は止まらない。"""
        self.run_script(GYAZO_REF, ttl=100)
        self.age_cache(GYAZO_REF, 200)

        result = self.run_script(GYAZO_REF, ttl=100, op_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")

    def test_取り直しに失敗した直後は_op_を呼び直さない(self):
        """再試行の抑制。これが無いとロック中は呼び出しのたびにタイムアウトを待つ。"""
        self.run_script(GYAZO_REF, ttl=100)
        self.age_cache(GYAZO_REF, 200)
        self.run_script(GYAZO_REF, ttl=100, op_fails=True)
        calls_after_failure = len(self.op_calls())

        self.run_script(GYAZO_REF, ttl=100, op_fails=True)
        self.assertEqual(len(self.op_calls()), calls_after_failure)

    def test_取り直しに失敗しても値は壊さない(self):
        """時刻だけ進めるつもりで値まで飛ばしていないこと。"""
        self.run_script(GYAZO_REF, ttl=100)
        self.age_cache(GYAZO_REF, 200)
        self.run_script(GYAZO_REF, ttl=100, op_fails=True)

        result = self.run_script(GYAZO_REF, ttl=100, with_op=False)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")

    def test_op_が返ってこなくても待たされずキャッシュを返す(self):
        """ロック中の op read は承認待ちで返らない。待たないことがこの設計の核心。

        値だけを見ても通ってしまう (待った末に諦めても同じ値が出る) ので、経過時間まで
        見る。閾値は op を 30 秒黙らせた設定と明確に切り分けられるところに置く。
        """
        self.run_script(GYAZO_REF, ttl=100)
        self.age_cache(GYAZO_REF, 200)

        started = time.monotonic()
        result = self.run_script(GYAZO_REF, ttl=100, refresh_timeout=1, op_hangs=30)
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")
        self.assertLess(elapsed, 15, "タイムアウトが効かず op の応答を待っている")

    def test_TTL_0_なら取り直さない(self):
        self.run_script(GYAZO_REF, ttl=0)
        self.set_op_value("rotated-token")
        self.age_cache(GYAZO_REF, 10**6)
        calls_before = len(self.op_calls())

        result = self.run_script(GYAZO_REF, ttl=0)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")
        self.assertEqual(len(self.op_calls()), calls_before)

    def test_寿命内なら_op_を呼ばない(self):
        self.run_script(GYAZO_REF, ttl=100)
        self.age_cache(GYAZO_REF, 50)
        calls_before = len(self.op_calls())

        self.run_script(GYAZO_REF, ttl=100)
        self.assertEqual(len(self.op_calls()), calls_before)

    def test_時刻を持たない古いキャッシュは取り直して移行する(self):
        """既にキャッシュ済みの項目を作り直させないための後方互換。"""
        self.write_legacy_cache(GYAZO_REF, "legacy-token")
        self.set_op_value("rotated-token")

        result = self.run_script(GYAZO_REF, ttl=100)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "rotated-token\n")
        self.assertIn(":", self.keychain_path(GYAZO_REF).read_text(), "新形式へ移っていない")

    def test_時刻を持たない古いキャッシュも_op_が無ければそのまま読める(self):
        self.write_legacy_cache(GYAZO_REF, "legacy-token")
        result = self.run_script(GYAZO_REF, ttl=100, with_op=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "legacy-token\n")

    def test_許可リストに無い参照は寿命の影響を受けない(self):
        """キャッシュしない参照は毎回 op を読む。TTL が何であれ変わらない。"""
        self.run_script(SSH_REF, ttl=10**6)
        self.run_script(SSH_REF, ttl=10**6)
        self.assertEqual(len(self.op_calls()), 2)
        self.assertEqual(self.cached_entries(), [])

    def test_TTL_が壊れていても既定で動き続ける(self):
        result = self.run_script(GYAZO_REF, ttl="いつまでも")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gyazo-token-abc\n")
        self.assertIn("SECRET_CACHE_TTL", result.stderr)

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

    def test_check_は鮮度を出すが値は出さない(self):
        self.run_script(GYAZO_REF)
        self.age_cache(GYAZO_REF, 7200)
        result = self.run_script("--check")
        self.assertIn("2 時間前", result.stdout)
        self.assertNotIn("gyazo-token-abc", result.stdout, "--check が値を漏らしている")

    def test_check_は時刻を持たない古いキャッシュを見分ける(self):
        self.write_legacy_cache(GYAZO_REF, "legacy-token")
        result = self.run_script("--check")
        self.assertIn("取得時刻が不明", result.stdout)
        self.assertNotIn("legacy-token", result.stdout, "--check が値を漏らしている")

    def test_check_は許可リストの不正な行を指摘する(self):
        self.allowlist.write_text("not-a-reference\n")
        result = self.run_script("--check")
        self.assertIn("不正", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
