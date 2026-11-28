#!/usr/bin/env python3
"""bin/cosense (helpfeel/cosense-cli へ 1Password の PAT を持たせるラッパー) のテスト。

本物の cosense と secret-read の代わりに偽物を置く。見たいのは次の 4 点:

  - 役割 cosense-pat の値を COSENSE_PAT として本物へ渡すこと
  - 呼び出し側が COSENSE_PAT を決めていれば (空でも) 触らないこと
  - 1Password から取れなくても止めず、settings.json に任せて本物を起動すること
  - PATH の上で自分自身 (symlink 越しを含む) を本物と取り違えて呼び直さないこと

    python3 claude/tests/cosense_wrapper_test.py
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from hookenv import clean_env

WRAPPER = Path(__file__).resolve().parent.parent.parent / "bin" / "cosense"

# 偽 secret-read。呼ばれた引数を控え、FAKE_SECRET_FAIL なら 1Password に届かない状態を再現する
FAKE_SECRET_READ = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_SECRET_FAIL:-}" ]; then
  echo "secret-read: 1Password にタグ secret-read/cosense-pat の付いた項目が無い" >&2
  exit 1
fi
printf 'pat-from-1password\n'
"""

# 偽の本物。受け取った COSENSE_PAT (未設定なら <unset>) と引数を 1 行ずつ出す
FAKE_REAL = r"""#!/usr/bin/env bash
printf 'PAT=%s\n' "${COSENSE_PAT-<unset>}"
for arg in "$@"; do printf 'ARG=%s\n' "$arg"; done
exit "${FAKE_REAL_EXIT:-0}"
"""


class CosenseWrapperTest(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

        # ラッパーは自分の隣の secret-read を呼ぶ。本物と同じ配置を作る
        self.setup_bin = self.root / "setup" / "bin"
        self.setup_bin.mkdir(parents=True)
        shutil.copy(WRAPPER, self.setup_bin / "cosense")
        self.install(self.setup_bin / "secret-read", FAKE_SECRET_READ)

        # fnm が入れる node の bin に相当する場所
        self.node_bin = self.root / "node" / "bin"
        self.node_bin.mkdir(parents=True)
        self.install(self.node_bin / "cosense", FAKE_REAL)

        self.log = self.root / "secret-read.log"
        self.log.write_text("")

    def install(self, path, body):
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_wrapper(self, *args, path=None, env_extra=None, drop_pat=True):
        env = clean_env()
        if drop_pat:
            env.pop("COSENSE_PAT", None)
        env["PATH"] = path or f"{self.setup_bin}:{self.node_bin}:/usr/bin:/bin"
        env["FAKE_LOG"] = str(self.log)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [str(self.setup_bin / "cosense"), *args],
            env=env, capture_output=True, text=True, timeout=20,
        )

    def secret_read_calls(self):
        return [line for line in self.log.read_text().splitlines() if line]

    def test_1Password_の_PAT_を渡して本物を起動する(self):
        result = self.run_wrapper("whoami", "https://scrapbox.io")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["PAT=pat-from-1password", "ARG=whoami", "ARG=https://scrapbox.io"],
        )
        self.assertEqual(self.secret_read_calls(), ["cosense-pat"])

    def test_引数を空白ごと渡し終了コードも返す(self):
        result = self.run_wrapper("readPage", "a b/c", env_extra={"FAKE_REAL_EXIT": "3"})
        self.assertEqual(result.returncode, 3)
        self.assertIn("ARG=a b/c", result.stdout.splitlines())

    def test_COSENSE_PAT_が決まっていれば触らない(self):
        result = self.run_wrapper("whoami", env_extra={"COSENSE_PAT": "given"})
        self.assertIn("PAT=given", result.stdout.splitlines())
        self.assertEqual(self.secret_read_calls(), [], "決まっているのに 1Password を読みに行った")

    def test_COSENSE_PAT_を空にすると差し込みを止められる(self):
        """1Password に Cosense の項目を持たない人向けの止め方。"""
        result = self.run_wrapper("whoami", env_extra={"COSENSE_PAT": ""})
        self.assertIn("PAT=", result.stdout.splitlines())
        self.assertEqual(self.secret_read_calls(), [])

    def test_1Password_から取れなくても_settings_json_に任せて起動する(self):
        result = self.run_wrapper("whoami", env_extra={"FAKE_SECRET_FAIL": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PAT=<unset>", result.stdout.splitlines(), "空の PAT を渡している")
        self.assertIn("settings.json", result.stderr)
        self.assertIn("secret-read/cosense-pat", result.stderr, "secret-read の理由を握り潰している")

    def test_symlink_越しの自分を本物と取り違えない(self):
        """~/.local/bin などから張った symlink が PATH の前にあっても、呼び直しの輪にならない。"""
        other = self.root / "local" / "bin"
        other.mkdir(parents=True)
        (other / "cosense").symlink_to(self.setup_bin / "cosense")
        result = self.run_wrapper(
            "whoami", path=f"{other}:{self.setup_bin}:{self.node_bin}:/usr/bin:/bin",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PAT=pat-from-1password", result.stdout.splitlines())
        self.assertEqual(len(self.secret_read_calls()), 1, "自分を呼び直している")

    def test_本物が無ければ入れ方を言って失敗する(self):
        result = self.run_wrapper("whoami", path=f"{self.setup_bin}:/usr/bin:/bin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("@helpfeel/cosense-cli", result.stderr)
        self.assertEqual(self.secret_read_calls(), [], "起動できないのに 1Password を読みに行った")


if __name__ == "__main__":
    unittest.main(verbosity=2)
