"""Structural contract tests for the Contract Executor Skill.

These tests only verify SKILL.md structure/frontmatter and the
test-prompts.json executable manifest schema and coverage. They do NOT
execute agent behavior; the main session will run the behavior fixtures with
fresh weak executors separately. Asserts are section-based so forbidden and
blocked tokens must appear in their dedicated sections and must not leak
into the mechanical-allowed section (no false positives from negation or
free text).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1] / "skills" / "contract-executor" / "SKILL.md"
FIXTURE_FILE = Path(__file__).resolve().parents[1] / "skills" / "contract-executor" / "test-prompts.json"

STATE_MACHINE_PHASES = ("READ", "IMPLEMENT", "VERIFY", "REPORT")
REPORT_FIELDS = ("STATUS", "CHANGES", "VERIFIED", "JUDGMENT CALLS", "GAPS")
PACKET_FIELDS = (
    "RUN_ID",
    "TASK_ID",
    "DELIVERABLE",
    "OBJECTIVE",
    "WRITE_SET",
    "READ_ONLY",
    "INTERFACES",
    "CONSTRAINTS",
    "DEPENDENCIES",
    "INDEPENDENT_ACCEPTANCE",
    "VERIFICATION",
    "DECISION_BUDGET",
    "STOP_CONDITIONS",
    "RETURN_FORMAT",
)
ALLOWED_MECHANICAL_ACTIONS = (
    "按指定目标修改",
    "沿用局部现有代码风格",
    "整理 import",
    "实现给定测试所需最小逻辑",
    "运行指定命令",
    "修复明确的局部编译错误",
)
BLOCKED_TRIGGERS = (
    "新增依赖",
    "public API",
    "schema",
    "transaction",
    "concurrency",
    "error contract",
    "acceptance criteria",
    "write-set",
)
FORBIDDEN_BEHAVIORS = (
    "重新规划",
    "重新理解整个用户需求",
    "架构设计",
    "spawn agent",
    "问最终用户",
    "自己改变范围",
    "自己选择依赖",
    "自己改变接口",
    "自行扩大验证目标",
)
LIFECYCLE_ONLY_TOKENS = ("RESUME_SAME", "SPAWN_SUCCESSOR", "CLOSE_ALLOWED", "COMPLETE_ALLOWED", "run_ledger")

REQUIRED_FIXTURES = {
    "mechanical": "completed",
    "new_dependency": "blocked",
    "write_set_expansion": "blocked",
    "public_api_change": "blocked",
}
BLOCKED_FIXTURE_MARKERS = {
    "new_dependency": "新增依赖",
    "write_set_expansion": "write-set",
    "public_api_change": "public API",
}

BEHAVIOR_FIXTURES = (
    {
        "name": "mechanical",
        "blocked": False,
        "expect_in_allowed": ALLOWED_MECHANICAL_ACTIONS,
        "expect_in_blocked": (),
    },
    {
        "name": "new_dependency",
        "blocked": True,
        "expect_in_allowed": (),
        "expect_in_blocked": ("新增依赖",),
    },
    {
        "name": "write_set_extension",
        "blocked": True,
        "expect_in_allowed": (),
        "expect_in_blocked": ("write-set",),
    },
)


def parse_frontmatter(text: str) -> dict[str, str]:
    body = text[1:] if text.startswith("\ufeff") else text
    if not body.startswith("---"):
        return {}
    lines = body.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().strip("\"'")
            value = value.strip().strip("\"'")
            if key and key not in data:
                data[key] = value
    return data


def sections(text: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip()
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return {name: "\n".join(lines) for name, lines in result.items()}


class ContractExecutorStructuralContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")
        self.sections = sections(self.text)

    def test_frontmatter_name_and_description(self) -> None:
        meta = parse_frontmatter(self.text)
        self.assertEqual(meta.get("name"), "contract-executor")
        self.assertTrue(meta.get("description"))

    def test_state_machine_section_only_four_phases(self) -> None:
        machine = self.sections["状态机"]
        self.assertIn("READ → IMPLEMENT → VERIFY → REPORT", machine)
        self.assertNotIn("←", machine)
        for phase in STATE_MACHINE_PHASES:
            self.assertIn(phase, machine)
        for token in ("RESUME", "PARK", "KEEP", "WAIT", "CLOSE_ALLOWED"):
            self.assertNotIn(token, machine)

    def test_allowed_actions_in_dedicated_section(self) -> None:
        allowed = self.sections["允许的机械动作"]
        for token in ALLOWED_MECHANICAL_ACTIONS + ("MECHANICAL", "LOCAL_LOW_RISK"):
            self.assertIn(token, allowed)
        for token in FORBIDDEN_BEHAVIORS:
            self.assertNotIn(token, allowed)

    def test_blocked_triggers_in_dedicated_section(self) -> None:
        blocked = self.sections["BLOCKED 触发项"]
        for token in BLOCKED_TRIGGERS:
            self.assertIn(token, blocked)
        self.assertIn("GAPS", blocked)
        self.assertNotIn("DECISION_REQUIRED", self.text)
        self.assertNotIn("EVIDENCE", self.text)

    def test_report_section_requires_machine_json_with_five_fields(self) -> None:
        report = self.sections["REPORT"]
        self.assertIn("JSON", report)
        self.assertIn("exactly five keys", report)
        for field in REPORT_FIELDS:
            self.assertIn(field, report)

    def test_forbidden_behaviors_in_dedicated_section(self) -> None:
        forbidden = self.sections["禁止行为"]
        for token in FORBIDDEN_BEHAVIORS:
            self.assertIn(token, forbidden)

    def test_behavior_fixtures(self) -> None:
        allowed = self.sections["允许的机械动作"]
        blocked = self.sections["BLOCKED 触发项"]
        for fixture in BEHAVIOR_FIXTURES:
            for token in fixture["expect_in_allowed"]:
                self.assertIn(token, allowed)
            for token in fixture["expect_in_blocked"]:
                self.assertIn(token, blocked)
            if fixture["blocked"]:
                for token in fixture["expect_in_blocked"]:
                    self.assertNotIn(token, allowed)

    def test_is_coder_skill_not_orchestrator(self) -> None:
        self.assertIn("coding 子代理", self.text)
        self.assertTrue(re.search(r"不是\s*orchestrator|不编排", self.text))

    def test_coder_not_decision_owner(self) -> None:
        self.assertIn("coder 永远不能成为高影响 decision owner", self.text)

    def test_no_lifecycle_scope_creep(self) -> None:
        for token in LIFECYCLE_ONLY_TOKENS:
            self.assertNotIn(token, self.text)

    def test_fixture_manifest_schema(self) -> None:
        data = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data.get("schema_version"), 2)
        fixtures = data.get("fixtures")
        self.assertIsInstance(fixtures, list)
        self.assertTrue(fixtures)
        for fixture in fixtures:
            self.assertIsInstance(fixture, dict)
            self.assertIsInstance(fixture.get("id"), str)
            self.assertTrue(fixture["id"].strip())
            setup = fixture.get("setup_files")
            self.assertIsInstance(setup, dict)
            for path, content in setup.items():
                self.assertIsInstance(path, str)
                self.assertTrue(path.strip())
                self.assertIsInstance(content, str)
            packet = fixture.get("packet")
            self.assertIsInstance(packet, dict)
            for field in PACKET_FIELDS:
                self.assertIn(field, packet)
            self.assertIsInstance(fixture.get("agent_prompt"), str)
            self.assertTrue(fixture["agent_prompt"].strip())
            expected = fixture.get("expected")
            self.assertIsInstance(expected, dict)
            report = expected.get("report")
            self.assertIsInstance(report, dict)
            self.assertIn(report.get("STATUS"), ("completed", "blocked"))
            self.assertEqual(report.get("fields"), list(REPORT_FIELDS))
            changed = expected.get("changed_files")
            self.assertIsInstance(changed, list)
            self.assertTrue(all(isinstance(path, str) and path.strip() for path in changed))
            files = expected.get("files")
            self.assertIsInstance(files, dict)
            self.assertTrue(all(isinstance(path, str) and isinstance(content, str) for path, content in files.items()))
            verification = expected.get("verification")
            self.assertIsInstance(verification, dict)
            self.assertIn("command", verification)
            self.assertIn("exit_code", verification)
            self.assertIn("failure_count", verification)
            command = verification.get("command")
            self.assertTrue(
                command is None
                or (
                    isinstance(command, list)
                    and all(isinstance(arg, str) and arg for arg in command)
                )
            )
            self.assertIsInstance(expected.get("forbidden_changes"), list)
            self.assertIsInstance(expected.get("forbidden_actions"), list)
            self.assertIsInstance(expected.get("generated_allowlist"), list)
            self.assertTrue(
                all(isinstance(pattern, str) for pattern in expected["generated_allowlist"])
            )

    def test_fixture_coverage(self) -> None:
        data = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
        by_id = {fixture["id"]: fixture for fixture in data["fixtures"]}
        self.assertEqual(set(by_id), set(REQUIRED_FIXTURES))
        for fixture_id, status in REQUIRED_FIXTURES.items():
            self.assertEqual(by_id[fixture_id]["expected"]["report"]["STATUS"], status)

    def test_blocked_fixtures_expect_zero_workspace_changes(self) -> None:
        data = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
        by_id = {fixture["id"]: fixture for fixture in data["fixtures"]}
        for fixture_id, status in REQUIRED_FIXTURES.items():
            if status != "blocked":
                continue
            self.assertEqual(by_id[fixture_id]["expected"]["changed_files"], [])
            self.assertEqual(by_id[fixture_id]["expected"]["files"], {})
            self.assertIsNone(by_id[fixture_id]["expected"]["verification"]["command"])
            self.assertEqual(by_id[fixture_id]["expected"]["generated_allowlist"], [])

    def test_blocked_fixture_prompts_contain_trigger_markers(self) -> None:
        data = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
        by_id = {fixture["id"]: fixture for fixture in data["fixtures"]}
        for fixture_id, marker in BLOCKED_FIXTURE_MARKERS.items():
            self.assertIn(marker, by_id[fixture_id]["agent_prompt"])

    def test_mechanical_fixture_has_no_blocked_markers_and_expected_verification(self) -> None:
        data = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
        by_id = {fixture["id"]: fixture for fixture in data["fixtures"]}
        prompt = by_id["mechanical"]["agent_prompt"]
        for marker in BLOCKED_FIXTURE_MARKERS.values():
            self.assertNotIn(marker, prompt)
        self.assertEqual(by_id["mechanical"]["expected"]["changed_files"], ["src/app.py"])
        self.assertIsInstance(by_id["mechanical"]["expected"]["verification"]["command"], list)
        self.assertTrue(by_id["mechanical"]["expected"]["verification"]["command"])
        self.assertEqual(by_id["mechanical"]["expected"]["verification"]["exit_code"], 0)
        self.assertEqual(by_id["mechanical"]["expected"]["verification"]["failure_count"], 0)
        self.assertTrue(by_id["mechanical"]["expected"]["generated_allowlist"])


if __name__ == "__main__":
    unittest.main()
