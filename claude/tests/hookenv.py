"""テストが子プロセスへ渡す環境を組む。

`claude/` と `bin/` のスクリプトは無効化スイッチ (`CLAUDE_PLAN_RECORD=0` など) や
振る舞いを変える変数を環境から読む。テストがそれを呼び出し元のセッションから継承すると、
**フックを黙らせているリポジトリから流したときだけ結果が変わる**。

issue #138 では mokume を主として開いたセッション (`.claude/settings.json` が
`CLAUDE_PLAN_RECORD=0` を立てている) から流した `plan_record_test` が 26 件落ちた。
スクリプトは壊れておらず、テストが素の `os.environ` を渡していただけだった。落ちる向きは
まだ気付けるが、**ガードを黙らせる変数を継承したまま「素通しした」を確かめると、判定を
何も見ていない緑ができる** — こちらは気付けない。

落とす変数は**スクリプト自身から集める**。一覧を手で持つと、新しいガードを足した日に
更新を忘れる。集め方は「既定値つきで読んでいる大文字の変数」で、システム変数だけを
除外リストで外す。

    from hookenv import clean_env
    subprocess.run([...], env=clean_env(FAKE_GH_PR="42"))

この方針が守られているかは claude/tests/env_isolation_test.py が見る。
"""

import atexit
import os
import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 判定の対象にするスクリプトの置き場。bin/ の道具も同じ理由で環境を読む
SCRIPT_DIRS = (ROOT / "claude", ROOT / "bin")

# スクリプトが読んでいても落としてはいけない変数。落とすと python や git や zsh が
# そもそも動かない。**足すときは「これはこのリポジトリの機構のスイッチではない」と
# 言えることを確かめる** — ここへ逃がすほど検査は緩くなる
SYSTEM_VARIABLES = frozenset(
    {
        "HOME", "PATH", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM",
        "PWD", "OLDPWD", "EDITOR", "PAGER", "SSH_AUTH_SOCK", "GITHUB_TOKEN",
        "GH_TOKEN", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    }
)

# ${VAR:-既定値} / ${VAR:=既定値} と、python の os.environ.get("VAR") / os.environ["VAR"]
_SHELL_DEFAULT = re.compile(r"\$\{([A-Z][A-Z0-9_]*):[-=]")
_PY_ENVIRON = re.compile(r"environ(?:\.get)?[\(\[]\s*[\"']([A-Z][A-Z0-9_]*)[\"']")


def script_files():
    """環境変数を読みうるスクリプトを列挙する。"""
    for directory in SCRIPT_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix in ("", ".sh", ".py"):
                yield path


def raw_variables_read_by(path):
    """そのスクリプトが既定値つきで読んでいる変数 (除外前)。"""
    text = path.read_text(errors="replace")
    return set(_SHELL_DEFAULT.findall(text)) | set(_PY_ENVIRON.findall(text))


def variables_read_by(path):
    """そのスクリプトが既定値つきで読んでいる変数 (システム変数を除く)。"""
    return raw_variables_read_by(path) - SYSTEM_VARIABLES


@lru_cache(maxsize=1)
def switch_variables():
    """このリポジトリのスクリプトが読む変数すべて。"""
    found = set()
    for script in script_files():
        found |= variables_read_by(script)
    return frozenset(found)


@lru_cache(maxsize=1)
def empty_zdotdir():
    """空のディレクトリ。ZDOTDIR をここへ向けると、子の zsh は利用者の rc ファイルを読まない。"""
    path = tempfile.mkdtemp(prefix="claude-tests-zdotdir-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def clean_env(**overrides):
    """呼び出し元のスイッチを落とした環境に、テストが明示した値を載せて返す。

    値に None を渡した変数は環境から落とす (未設定の再現)。

    **環境変数を落としても、子の zsh が ~/.zshenv を読めば戻ってくる。** このリポジトリの
    zshenv は /opt/homebrew/bin と setup の bin/ を PATH の先頭へ足すので、偽コマンドを
    PATH の先頭へ置いたつもりでも、Homebrew に同名があれば本物が先に引かれる (偽 brew の
    つもりで本物の `brew install` が走った。issue #238)。ZDOTDIR を空のディレクトリへ
    向けて読ませない。`-f` と違って環境変数なので、テスト対象のスクリプトが起動する
    孫の zsh にも届く。rc ファイルそのものを試すテストは ZDOTDIR を明示して上書きする。
    """
    env = {k: v for k, v in os.environ.items() if k not in switch_variables()}
    env["ZDOTDIR"] = empty_zdotdir()
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env
