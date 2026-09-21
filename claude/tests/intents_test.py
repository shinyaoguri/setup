#!/usr/bin/env python3
"""claude/intents.json (意図の台帳) のテスト。

台帳は「Claude まわりで何をしたいか」の正本で、手段 (本体機能・第三者・自作) は
その下に交換可能なものとしてぶら下がる。本体が更新されたとき、自作の手段を
「まだ要るか」と問い直すための索引である (shinyaoguri/setup#232)。

索引は実体からずれた瞬間に嘘になるので、ここで両方向を縛る:

  実体 → 台帳   配布するファイル・settings.json のフックとキー・有効なプラグインは、
                必ずどれかの意図の手段として載っている (意図を言えない手段を置かない)
  台帳 → 実体   手段として書いたパス・設定キー・CLAUDE.md の文言・検証手段が実在する
  依存の被覆    settings.json に現れるフックのイベント名とツール名が depends_on に在る
                (本体の changelog と突き合わせる語彙に穴を開けない)

検査はどれも「問題の一覧を返す純関数」にしてある。実データで空になることに加えて、
壊した台帳を渡して**確かに拾う**ことも下で見る — 対象を壊しても緑のままになる
構造テストを、このリポジトリは既に何度か踏んでいる (#218)。

日付や経過日数には触れない。見直しの間隔を見るのは週次の検知の仕事で、ここに
時刻依存の assert を置くと、何も変えていない日に CI が赤くなる。

    python3 claude/tests/intents_test.py
"""

import copy
import json
import re
import unittest
from pathlib import Path

from ansible_tasks_test import without_comments

REPO = Path(__file__).resolve().parent.parent.parent
INTENTS = REPO / "claude" / "intents.json"
SETTINGS = REPO / "claude" / "settings.json"
CLAUDE_TASKS = REPO / "tasks" / "claude.yml"

# 台帳が肥大したら索引として読めなくなる。足す前に、既存の意図へ束ねられないかを考える合図
MAX_INTENTS = 40
MAX_WHY_CHARS = 200

TOP_KEYS = {"version", "reviewed_against", "intents"}
REQUIRED_KEYS = {"id", "intent", "why", "rationale", "means", "depends_on", "verify"}
OPTIONAL_KEYS = {"sunset", "gap"}

# native = 本体の設定 / self = このリポジトリの自作 / doc = CLAUDE.md の文言 /
# plugin-self = 自作プラグイン / plugin-third = 第三者のプラグイン / external = playbook など本体の外
KINDS = {"native", "self", "doc", "plugin-self", "plugin-third", "external"}
# 本体が同じことをするようになれば消える側。いつ消すかを言えないものは置かない
KINDS_NEEDING_SUNSET = {"self", "plugin-self", "doc"}

SURFACES = {
    "hook-event", "hook-input", "hook-output", "tool-name", "settings-key", "cli",
    "env-var", "plugin-spec", "skill-spec", "statusline-input", "file-format",
    "internal-api", "desktop-app", "harness-behavior", "model-behavior",
}
STABILITIES = {"documented", "undocumented"}

# env-doctor の節番号 (claude-plugins の rs-doctor-env.sh)。節が増えたらここも動かす
ENV_DOCTOR_SECTIONS = range(1, 11)

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SETTINGS_PREFIX = "settings:"
DISTRIBUTED_PREFIX = "~/.claude/"


def load(path):
    return json.loads(path.read_text())


def config_files(tasks_text):
    """tasks/claude.yml の claude_config_files に並ぶファイル名。

    コメントを落とした本文を渡すこと — 冒頭コメントが配布物の名前を大量に引用している。
    """
    names, inside = [], False
    for line in tasks_text.split("\n"):
        if line.strip() == "claude_config_files:":
            inside = True
            continue
        if inside:
            item = re.match(r"^\s+- (\S+)$", line)
            if not item:
                break
            names.append(item.group(1))
    return names


def hook_entries(settings):
    """settings.json の hooks を (イベント名, matcher, command) の並びにする。"""
    return [
        (event, group.get("matcher"), hook.get("command", ""))
        for event, groups in settings.get("hooks", {}).items()
        for group in groups
        for hook in group.get("hooks", [])
    ]


def all_means(ledger):
    return [m for intent in ledger["intents"] for m in intent["means"]]


def resolve(settings, ref):
    """`settings:permissions.ask` を settings.json の値へ解決する。無ければ KeyError。"""
    node = settings
    for part in ref[len(SETTINGS_PREFIX):].split("."):
        node = node[part]
    return node


def plugin_name(ref):
    """`repo-standards@shinyaoguri#hooks/...` の `#` より前 (enabledPlugins のキー)。"""
    return ref.split("#", 1)[0]


def schema_problems(ledger):
    problems = []
    if set(ledger) != TOP_KEYS:
        problems.append(f"トップレベルのキーが {sorted(TOP_KEYS)} でない: {sorted(ledger)}")
        return problems

    reviewed = ledger["reviewed_against"]
    if not VERSION_RE.match(str(reviewed.get("claude_code", ""))):
        problems.append("reviewed_against.claude_code が x.y.z の形でない")
    if not DATE_RE.match(str(reviewed.get("date", ""))):
        problems.append("reviewed_against.date が YYYY-MM-DD の形でない")

    intents = ledger["intents"]
    if len(intents) > MAX_INTENTS:
        problems.append(f"意図が {len(intents)} 件ある (上限 {MAX_INTENTS})")

    seen = set()
    for intent in intents:
        name = intent.get("id", "<id なし>")
        keys = set(intent)
        if REQUIRED_KEYS - keys:
            problems.append(f"{name}: 必須キーが無い {sorted(REQUIRED_KEYS - keys)}")
            continue
        if keys - REQUIRED_KEYS - OPTIONAL_KEYS:
            problems.append(f"{name}: 未知のキー {sorted(keys - REQUIRED_KEYS - OPTIONAL_KEYS)}")
        if not ID_RE.match(name):
            problems.append(f"{name}: id が kebab-case でない")
        if name in seen:
            problems.append(f"{name}: id が重複している")
        seen.add(name)
        if len(intent["why"]) > MAX_WHY_CHARS:
            problems.append(f"{name}: why が {MAX_WHY_CHARS} 字を超える (理由の本文は rationale の先へ)")
        if not intent["rationale"]:
            problems.append(f"{name}: rationale が空 (理由の本文がどこに在るかを指す)")
        if not intent["verify"]:
            problems.append(f"{name}: verify が空 (確かめないなら none: 理由 と書く)")

        kinds = set()
        for means in intent["means"]:
            if set(means) - {"kind", "ref", "anchor"} or not {"kind", "ref"} <= set(means):
                problems.append(f"{name}: means のキーが kind / ref / anchor でない {sorted(means)}")
                continue
            if means["kind"] not in KINDS:
                problems.append(f"{name}: 未知の kind {means['kind']!r}")
            kinds.add(means["kind"])
        if kinds & KINDS_NEEDING_SUNSET and not intent.get("sunset"):
            problems.append(f"{name}: 自作の手段を持つのに sunset が無い (いつやめるかを言えない)")
        if not intent["means"] and intent.get("gap"):
            problems.append(f"{name}: 手段が無い (unmet) のに gap がある (gap は部分的に満たすときだけ)")

        for dep in intent["depends_on"]:
            if set(dep) != {"surface", "name", "stability"}:
                problems.append(f"{name}: depends_on のキーが surface / name / stability でない")
                continue
            if dep["surface"] not in SURFACES:
                problems.append(f"{name}: 未知の surface {dep['surface']!r}")
            if dep["stability"] not in STABILITIES:
                problems.append(f"{name}: 未知の stability {dep['stability']!r}")
    return problems


def dangling_refs(ledger, settings, repo):
    """台帳 → 実体。手段・検証として書いたものが実在しないもの。"""
    problems = []
    enabled = settings.get("enabledPlugins", {})
    for intent in ledger["intents"]:
        name = intent["id"]
        for means in intent["means"]:
            kind, ref, anchor = means["kind"], means["ref"], means.get("anchor")
            if kind == "native":
                if not ref.startswith(SETTINGS_PREFIX):
                    problems.append(f"{name}: native の ref は {SETTINGS_PREFIX}<キーパス> で書く: {ref}")
                    continue
                try:
                    value = resolve(settings, ref)
                except (KeyError, TypeError):
                    problems.append(f"{name}: settings.json に {ref} が無い")
                    continue
                if anchor and anchor not in json.dumps(value, ensure_ascii=False):
                    problems.append(f"{name}: {ref} の中に {anchor!r} が無い")
            elif kind in ("self", "doc", "external"):
                path = repo / ref
                if not path.is_file():
                    problems.append(f"{name}: {ref} が実在しない")
                    continue
                if kind == "doc" and not anchor:
                    problems.append(f"{name}: doc には anchor (実在を確かめる文言) が要る: {ref}")
                if anchor:
                    text = without_comments(path) if kind == "external" else path.read_text()
                    if anchor not in text:
                        problems.append(f"{name}: {ref} に {anchor!r} が無い")
            elif kind in ("plugin-self", "plugin-third"):
                if not enabled.get(plugin_name(ref)):
                    problems.append(f"{name}: {plugin_name(ref)} が enabledPlugins で有効になっていない")

        for verify in intent["verify"]:
            if verify.startswith("none:"):
                if not verify[len("none:"):].strip():
                    problems.append(f"{name}: none: の後ろに理由が無い")
            elif verify.startswith("env-doctor:"):
                section = verify[len("env-doctor:"):]
                if not section.isdigit() or int(section) not in ENV_DOCTOR_SECTIONS:
                    problems.append(f"{name}: env-doctor の節番号が範囲外: {verify}")
            elif not (repo / verify).is_file():
                problems.append(f"{name}: verify のファイルが実在しない: {verify}")
    return problems


def orphan_means(ledger, settings, distributed):
    """実体 → 台帳。どの意図からも参照されていない手段。"""
    means = all_means(ledger)
    refs = {(m["kind"], m["ref"]) for m in means}
    native_refs = [m["ref"][len(SETTINGS_PREFIX):] for m in means if m["kind"] == "native"]
    problems = []

    for filename in distributed:
        if filename == "settings.json":
            covered = bool(native_refs)
        elif filename == "CLAUDE.md":
            covered = ("doc", "claude/CLAUDE.md") in refs
        else:
            covered = ("self", f"claude/{filename}") in refs
        if not covered:
            problems.append(f"配布物 claude/{filename} を手段に持つ意図が無い")

    for event, _matcher, command in hook_entries(settings):
        if command.startswith(DISTRIBUTED_PREFIX):
            script = "claude/" + command[len(DISTRIBUTED_PREFIX):].split()[0]
            covered = ("self", script) in refs
        else:
            # スクリプトを持たないインラインのフック。settings:hooks.<イベント> + anchor で指す
            covered = any(
                m["kind"] == "native" and m["ref"] == f"{SETTINGS_PREFIX}hooks.{event}"
                and m.get("anchor") and m["anchor"] in command
                for m in means
            )
        if not covered:
            problems.append(f"フック {event}: {command} を手段に持つ意図が無い")

    plugins = {plugin_name(m["ref"]) for m in means if m["kind"] in ("plugin-self", "plugin-third")}
    for plugin in settings.get("enabledPlugins", {}):
        if plugin not in plugins:
            problems.append(f"プラグイン {plugin} を手段に持つ意図が無い")

    for key in settings:
        if key == "hooks":
            continue  # フックは上で command 単位に見ている (キー単位では粗すぎる)
        if not any(ref == key or ref.startswith(key + ".") for ref in native_refs):
            problems.append(f"settings.json の {key} を手段に持つ意図が無い")
    return problems


def uncovered_dependencies(ledger, settings):
    """settings.json に現れる本体の語 (イベント名・ツール名) で、depends_on に無いもの。"""
    declared = {
        (dep["surface"], dep["name"])
        for intent in ledger["intents"] for dep in intent["depends_on"]
    }
    problems = []
    for event, matcher, _command in hook_entries(settings):
        if ("hook-event", event) not in declared:
            problems.append(f"フックのイベント {event} が depends_on に無い")
        for tool in (matcher or "").split("|"):
            if tool and tool != "*" and ("tool-name", tool) not in declared:
                problems.append(f"matcher のツール名 {tool} が depends_on に無い")
    return sorted(set(problems))


class LedgerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = load(INTENTS)
        cls.settings = load(SETTINGS)
        cls.distributed = config_files(without_comments(CLAUDE_TASKS))


class RealLedgerTest(LedgerTestCase):
    """リポジトリに在る台帳そのものが、実体と食い違っていないこと。"""

    def test_schema(self):
        self.assertEqual(schema_problems(self.ledger), [])

    def test_every_ref_exists(self):
        self.assertEqual(dangling_refs(self.ledger, self.settings, REPO), [])

    def test_no_orphan_means(self):
        self.assertEqual(orphan_means(self.ledger, self.settings, self.distributed), [])

    def test_hook_vocabulary_is_declared(self):
        self.assertEqual(uncovered_dependencies(self.ledger, self.settings), [])

    def test_config_files_are_found(self):
        """抽出が空振りすると、配布物の被覆検査が何も見ずに緑になる。"""
        self.assertIn("settings.json", self.distributed)
        self.assertIn("git-safety-guard.sh", self.distributed)

    def test_hooks_are_found(self):
        self.assertTrue(any(c.startswith(DISTRIBUTED_PREFIX) for _, _, c in hook_entries(self.settings)))
        self.assertTrue(any(not c.startswith(DISTRIBUTED_PREFIX) for _, _, c in hook_entries(self.settings)))


class DetectionTest(LedgerTestCase):
    """壊した台帳・壊した実体を渡して、検査が確かに拾うこと。"""

    def setUp(self):
        self.broken = copy.deepcopy(self.ledger)
        self.world = copy.deepcopy(self.settings)

    def intent(self, intent_id):
        return next(i for i in self.broken["intents"] if i["id"] == intent_id)

    def assertReported(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), f"{fragment!r} が報告されていない: {problems}")

    # --- 実体 → 台帳 ---

    def test_new_hook_script_without_intent(self):
        self.world["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": "~/.claude/new-guard.sh"})
        self.assertReported(orphan_means(self.ledger, self.world, self.distributed), "new-guard.sh")

    def test_new_inline_hook_without_intent(self):
        self.world["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "say done"})
        self.assertReported(orphan_means(self.ledger, self.world, self.distributed), "say done")

    def test_new_distributed_file_without_intent(self):
        self.assertReported(orphan_means(self.ledger, self.settings, self.distributed + ["new-tool.py"]), "new-tool.py")

    def test_new_plugin_without_intent(self):
        self.world["enabledPlugins"]["shiny@somewhere"] = True
        self.assertReported(orphan_means(self.ledger, self.world, self.distributed), "shiny@somewhere")

    def test_new_settings_key_without_intent(self):
        self.world["outputStyle"] = "terse"
        self.assertReported(orphan_means(self.ledger, self.world, self.distributed), "outputStyle")

    def test_removed_means_orphans_the_script(self):
        self.intent("signal-ownership")["means"] = []
        self.assertReported(orphan_means(self.broken, self.settings, self.distributed), "signal-guard.py")

    # --- 台帳 → 実体 ---

    def test_missing_self_file(self):
        self.intent("signal-ownership")["means"][0]["ref"] = "claude/gone.py"
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "claude/gone.py")

    def test_anchor_no_longer_in_claude_md(self):
        doc = next(m for m in self.intent("autonomy-boundary")["means"] if m["kind"] == "doc")
        doc["anchor"] = "この文言は CLAUDE.md に存在しない"
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "存在しない")

    def test_doc_without_anchor(self):
        doc = next(m for m in self.intent("autonomy-boundary")["means"] if m["kind"] == "doc")
        del doc["anchor"]
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "anchor")

    def test_external_anchor_ignores_comments(self):
        """コメントにしか無い語を anchor にしても、実在とは認めない。"""
        external = next(m for m in self.intent("config-distribution")["means"] if m["kind"] == "external")
        external["anchor"] = "スキルはこのリポでは配らない"
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "配らない")

    def test_unresolvable_settings_key(self):
        self.intent("frictionless-reads")["means"][0]["ref"] = "settings:permissions.nope"
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "permissions.nope")

    def test_disabled_plugin(self):
        self.world["enabledPlugins"]["eli5@claude-community"] = False
        self.assertReported(dangling_refs(self.ledger, self.world, REPO), "eli5@claude-community")

    def test_missing_verify_file(self):
        self.intent("signal-ownership")["verify"] = ["claude/tests/gone_test.py"]
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "gone_test.py")

    def test_verify_none_needs_a_reason(self):
        self.intent("third-party-tools")["verify"] = ["none:"]
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "理由")

    def test_env_doctor_section_out_of_range(self):
        self.intent("skill-supply")["verify"] = ["env-doctor:99"]
        self.assertReported(dangling_refs(self.broken, self.settings, REPO), "env-doctor:99")

    # --- 依存の被覆 ---

    def test_new_hook_event(self):
        self.world["hooks"]["SubagentStop"] = [{"hooks": [{"type": "command", "command": "~/.claude/plan-record.sh guard"}]}]
        self.assertReported(uncovered_dependencies(self.ledger, self.world), "SubagentStop")

    def test_new_matcher_tool(self):
        self.world["hooks"]["PreToolUse"][0]["matcher"] = "Bash|WebFetch"
        self.assertReported(uncovered_dependencies(self.ledger, self.world), "WebFetch")

    # --- スキーマ ---

    def test_self_means_needs_sunset(self):
        del self.intent("signal-ownership")["sunset"]
        self.assertReported(schema_problems(self.broken), "sunset")

    def test_status_is_derived_not_written(self):
        self.intent("signal-ownership")["status"] = "met"
        self.assertReported(schema_problems(self.broken), "未知のキー")

    def test_unknown_surface(self):
        self.intent("signal-ownership")["depends_on"][0]["surface"] = "vibes"
        self.assertReported(schema_problems(self.broken), "vibes")

    def test_unknown_kind(self):
        self.intent("signal-ownership")["means"][0]["kind"] = "magic"
        self.assertReported(schema_problems(self.broken), "magic")

    def test_duplicate_id(self):
        self.broken["intents"].append(copy.deepcopy(self.broken["intents"][0]))
        self.assertReported(schema_problems(self.broken), "重複")

    def test_long_why(self):
        self.intent("signal-ownership")["why"] = "あ" * (MAX_WHY_CHARS + 1)
        self.assertReported(schema_problems(self.broken), "why")

    def test_too_many_intents(self):
        extra = copy.deepcopy(self.broken["intents"][0])
        for n in range(MAX_INTENTS):
            self.broken["intents"].append({**extra, "id": f"filler-{n}"})
        self.assertReported(schema_problems(self.broken), "上限")

    def test_gap_on_unmet_intent(self):
        self.intent("wish-desktop-fresh-environment")["gap"] = "一部だけ"
        self.assertReported(schema_problems(self.broken), "unmet")

    def test_bad_reviewed_version(self):
        self.broken["reviewed_against"]["claude_code"] = "latest"
        self.assertReported(schema_problems(self.broken), "x.y.z")


if __name__ == "__main__":
    unittest.main()
