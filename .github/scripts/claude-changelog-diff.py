#!/usr/bin/env python3
"""Claude Code の CHANGELOG のうち、まだ見直していない版を意図の台帳と突き合わせる。

claude/intents.json の reviewed_against より新しい版の節を抜き、台帳の depends_on に
書いた語へ当たった行を先頭に集めたレポート (Markdown) を作る。判定はしない —
非推奨化の告知に決まった形式が無く、文字列だけでは「自作をやめられるか」は決まらない。
ここは**読む材料を絞る**ところまでで、判定は claude-upstream-review スキルの仕事
(shinyaoguri/setup#232)。

当たりは 2 段に分ける。実際の changelog で測ると、`PreToolUse` は 40 版で 3 行しか
当たらないのに `Bash` は 55 行当たる — ツール名は一般語で、ほとんどがツール自体の
修正であってフックの契約とは関係が無い:

  強   フックのイベント・入出力、設定キー、プラグインやスキルの仕様など、本体の仕様語
  弱   ツール名と harness の振る舞い (WEAK_SURFACES)。並べるが、起票の引き金にしない

起票が要るのは、新しい版が在り、かつ「強い当たりが在る」か「最後の見直しから
max-age-days を超えた」とき。差が在るだけで毎回知らせると、確認が反射になる (setup#148)。

    claude-changelog-diff.py --changelog CHANGELOG.md --intents claude/intents.json \\
        --today 2026-09-21 --report report.md

標準出力には要約の JSON を 1 行出す (workflow とスキルが読む)。
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

CHANGELOG_URL = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
HEADING_RE = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\s*$")
# 告知の形式は決まっていないので、ここに当たらない非推奨化も在る。見落としを減らす補助
NOTICE_RE = re.compile(r"deprecat|remov|renam|no longer|breaking", re.IGNORECASE)
WEAK_SURFACES = {"tool-name", "harness-behavior", "model-behavior", "desktop-app"}
# GitHub の Issue 本文の上限は 65,536 字。要約と当たりの節を置いた残りを全抜粋に回す
DEFAULT_MAX_CHARS = 60000
DEFAULT_MAX_AGE_DAYS = 30


def parse_version(text):
    return tuple(int(part) for part in text.split("."))


def sections(changelog):
    """CHANGELOG を [(版のタプル, 版の文字列, [箇条書きの行])] にする。並びは元のまま。"""
    result, current = [], None
    for line in changelog.split("\n"):
        heading = HEADING_RE.match(line)
        if heading:
            version = tuple(int(g) for g in heading.groups())
            current = (version, ".".join(heading.groups()), [])
            result.append(current)
        elif current is not None and line.startswith("- "):
            current[2].append(line)
    return result


def newer_than(parsed, reviewed):
    """reviewed より新しい版だけ。reviewed そのものは含めない (見直し済みなので)。"""
    return [section for section in parsed if section[0] > reviewed]


def vocabulary(ledger):
    """台帳の depends_on から、changelog と照合できる語を {語: (強いか, [意図の id])} で返す。

    非 ASCII の name は日本語で書いた振る舞いの説明で、英語の changelog には現れない。
    同じ語が強弱両方の surface に在れば強い側に倒す。
    """
    words = {}
    for intent in ledger["intents"]:
        for dep in intent["depends_on"]:
            name = dep["name"]
            if not name.isascii():
                continue
            strong = dep["surface"] not in WEAK_SURFACES
            known_strong, ids = words.get(name, (False, []))
            if intent["id"] not in ids:
                ids = ids + [intent["id"]]
            words[name] = (known_strong or strong, ids)
    return words


def word_pattern(word):
    """語の前後が識別子の続きでないこと。`Edit` が `Edited` や `MultiEdit` に当たらない。"""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])")


def find_hits(newer, words):
    """[(版, 行, [当たった語], [意図の id], 強いか)] を changelog の並びで返す。"""
    patterns = [(word, word_pattern(word), strong, ids) for word, (strong, ids) in words.items()]
    hits = []
    for _version, label, lines in newer:
        for line in lines:
            matched = [(word, strong, ids) for word, pattern, strong, ids in patterns if pattern.search(line)]
            if not matched:
                continue
            intent_ids = sorted({i for _w, _s, ids in matched for i in ids})
            hits.append((label, line, [w for w, _s, _i in matched], intent_ids, any(s for _w, s, _i in matched)))
    return hits


def days_between(earlier, later):
    return (datetime.date.fromisoformat(later) - datetime.date.fromisoformat(earlier)).days


def summarize(changelog, ledger, today, max_age_days=DEFAULT_MAX_AGE_DAYS):
    reviewed = ledger["reviewed_against"]
    parsed = sections(changelog)
    newer = newer_than(parsed, parse_version(reviewed["claude_code"]))
    hits = find_hits(newer, vocabulary(ledger))
    strong = [h for h in hits if h[4]]
    age = days_between(reviewed["date"], today)
    return {
        "reviewed": reviewed["claude_code"],
        "reviewed_date": reviewed["date"],
        "latest": max(parsed)[1] if parsed else None,
        "versions": len(newer),
        "strong_hits": len(strong),
        "weak_hits": len(hits) - len(strong),
        "age_days": age,
        # 見出しが 1 つも読めないのは、取得の失敗か見出し形式の変更。黙って「差なし」にしない
        "parse_failed": not parsed,
        "issue_needed": not parsed or bool(newer and (strong or age > max_age_days)),
    }, newer, hits


def hit_lines(hits):
    return [
        f"- `{label}` {line[2:]}\n  - 語: {', '.join(f'`{w}`' for w in words)} / 意図: {', '.join(f'`{i}`' for i in intent_ids)}"
        for label, line, words, intent_ids, _strong in hits
    ]


def render(summary, newer, hits, max_chars=DEFAULT_MAX_CHARS):
    if summary["parse_failed"]:
        return (
            "# Claude Code の CHANGELOG を読めなかった\n\n"
            "`## x.y.z` の形の見出しが 1 つも見つからない。取得に失敗したか、見出しの形式が変わった "
            f"(台帳の `upstream-tracking` が依存している)。\n\n{CHANGELOG_URL}\n"
        )

    strong = [h for h in hits if h[4]]
    weak = [h for h in hits if not h[4]]
    hit_text = {line for _label, line, _w, _i, _s in hits}
    notices = [
        f"- `{label}` {line[2:]}"
        for _version, label, lines in newer for line in lines
        if NOTICE_RE.search(line) and line not in hit_text
    ]

    head = [
        f"# Claude Code {summary['reviewed']} → {summary['latest']} ({summary['versions']} 版)",
        "",
        f"台帳 (`claude/intents.json`) の見直し済みは **{summary['reviewed']}** ({summary['reviewed_date']}・{summary['age_days']} 日前)。"
        "判定は `claude-upstream-review` スキルで行い、終えたら `reviewed_against` を進める。",
        "",
        "このレポートは読む材料を絞っただけで、判定ではない。当たりが無い行にも、自作の手段を"
        "置き換えられる新機能は載りうる (語彙に無いものは当たらない)。",
        "",
        f"## 本体の仕様語に当たった行 ({len(strong)})",
        "",
        *(hit_lines(strong) or ["(なし)"]),
        "",
        f"## ツール名・一般語に当たった行 ({len(weak)})",
        "",
        "ツール自体の修正が大半で、フックの契約に関わるものは少ない。",
        "",
        *(hit_lines(weak) or ["(なし)"]),
        "",
        f"## 非推奨・削除・改名を示す語を含む、上に無い行 ({len(notices)})",
        "",
        *(notices or ["(なし)"]),
        "",
        "## 全抜粋",
        "",
    ]
    body = []
    for _version, label, lines in newer:
        body += [f"### {label}", "", *lines, ""]

    head_text, body_text = "\n".join(head), "\n".join(body)
    room = max_chars - len(head_text)
    if len(body_text) > room:
        notice = f"\n\n(長いのでここで切った。続きは {CHANGELOG_URL})\n"
        body_text = body_text[: max(room - len(notice), 0)].rsplit("\n", 1)[0] + notice
    return head_text + body_text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--changelog", required=True, type=Path)
    parser.add_argument("--intents", required=True, type=Path)
    parser.add_argument("--today", default=datetime.date.today().isoformat())
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--report", type=Path, help="レポートの書き出し先 (省略時は書かない)")
    args = parser.parse_args(argv)

    ledger = json.loads(args.intents.read_text())
    summary, newer, hits = summarize(args.changelog.read_text(), ledger, args.today, args.max_age_days)
    if args.report:
        args.report.write_text(render(summary, newer, hits, args.max_chars))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
