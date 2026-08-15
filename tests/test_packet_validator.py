"""Tests for the Task Packet Validator CLI (plan sections 36-39, 116)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
VALIDATOR = SCRIPTS / "validate_task_packet.py"


def _write_json(data: object) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-packet-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return Path(name)


def base_packet(**overrides: object) -> dict:
    packet: dict = {
        "RUN_ID": "R-1",
        "TASK_ID": "T1",
        "DELIVERABLE": "implement endpoint",
        "OBJECTIVE": "observable acceptance result",
        "WRITE_SET": ["src/a.py"],
        "READ_ONLY": ["src/b.py"],
        "INTERFACES": "fixed method signatures",
        "CONSTRAINTS": "mechanical only",
        "DEPENDENCIES": [],
        "INDEPENDENT_ACCEPTANCE": "tests pass",
        "VERIFICATION": ["python -m unittest"],
        "DECISION_BUDGET": "MECHANICAL",
        "STOP_CONDITIONS": ["block on public API change"],
        "RETURN_FORMAT": "STATUS: completed|blocked",
    }
    packet.update(overrides)
    return packet


def base_decision(decision_id: str, **overrides: object) -> dict:
    decision: dict = {
        "id": decision_id,
        "domain": "transaction-boundary",
        "owner": "main",
        "status": "resolved",
        "value": "preserve existing boundary",
        "evidence": [],
        "affects": ["T1"],
    }
    decision.update(overrides)
    return decision


def run_validator(
    packet: dict,
    decisions: object | None = None,
    tasks: list[dict] | None = None,
    change_facts: dict | None = None,
) -> tuple[int, dict | None, dict | None]:
    paths: list[Path] = []
    packet_path = _write_json(packet)
    paths.append(packet_path)
    args = [sys.executable, str(VALIDATOR), "--packet", str(packet_path)]
    if decisions is not None:
        decision_path = _write_json(decisions)
        paths.append(decision_path)
        args += ["--decisions", str(decision_path)]
    if tasks is not None:
        task_path = _write_json(tasks)
        paths.append(task_path)
        args += ["--tasks", str(task_path)]
    if change_facts is not None:
        facts_path = _write_json(change_facts)
        paths.append(facts_path)
        args += ["--change-facts", str(facts_path)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    err = None
    if proc.stderr.strip():
        try:
            err = json.loads(proc.stderr)
        except json.JSONDecodeError:
            err = {"error": {"code": "unparseable", "message": proc.stderr.strip()}}
    return proc.returncode, out, err


def evidence_kinds(out: dict) -> list[tuple[str, str]]:
    return [(item["kind"], item.get("field", "")) for item in out.get("evidence", [])]


class PacketValidatorCliTest(unittest.TestCase):
    def test_valid_packet(self) -> None:
        code, out, err = run_validator(base_packet())
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")
        self.assertEqual(out["decision_budget"], "MECHANICAL")
        self.assertEqual(out["decision_refs"], [])

    def test_red_claim_with_missing_implementation_is_blocked(self) -> None:
        packet = base_packet(VERIFICATION=["RED：运行测试，确认因实现缺失而失败"])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["status"], "BLOCKED")
        evidence = next(item for item in out["evidence"] if item["kind"] == "invalid_red_evidence")
        self.assertEqual(evidence["field"], "VERIFICATION")
        self.assertIn("RED", evidence["problem"])

    def test_red_claim_with_implementation_synonyms_is_blocked(self) -> None:
        invalid_reds = (
            "RED: tests_run > 0; target behavior assertion failed because of missing implementation",
            "RED: tests_run > 0; target behavior assertion failed because it is not implemented",
            "RED: tests_run > 0; target behavior assertion failed because it is unimplemented",
            "RED: tests_run > 0; target behavior fails until implemented",
            "RED：tests_run > 0；目标行为断言失败，因为未实现",
            "RED：tests_run > 0；目标行为断言失败，因为尚未实现",
            "RED：tests_run > 0；目标行为断言失败，因为缺少实现",
        )
        for verification in invalid_reds:
            with self.subTest(verification=verification):
                code, out, err = run_validator(base_packet(VERIFICATION=[verification]))
                self.assertEqual(code, 3, err)
                assert out is not None
                self.assertIn(("invalid_red_evidence", "VERIFICATION"), evidence_kinds(out))

    def test_red_compiler_message_reference_is_valid(self) -> None:
        verification = (
            'RED: tests_run > 0; target behavior assertion failed; '
            'assert compiler message contains "cannot find symbol"'
        )
        code, out, err = run_validator(base_packet(VERIFICATION=[verification]))
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_red_negative_implementation_check_is_valid(self) -> None:
        verification = (
            "RED: tests_run > 0; target behavior assertion failed; "
            "verify implementation is not missing"
        )
        code, out, err = run_validator(base_packet(VERIFICATION=[verification]))
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_red_claim_with_compile_failure_is_blocked(self) -> None:
        invalid_reds = (
            "RED: production class OrderService is missing",
            "RED: 生产类 OrderService 不存在",
            "RED: missing symbol OrderService",
            "RED: cannot find symbol OrderService",
            "RED: mvn testCompile failed",
            "RED: compilation failed before tests ran",
        )
        for verification in invalid_reds:
            with self.subTest(verification=verification):
                code, out, err = run_validator(base_packet(VERIFICATION=[verification]))
                self.assertEqual(code, 3, err)
                assert out is not None
                self.assertIn(("invalid_red_evidence", "VERIFICATION"), evidence_kinds(out))

    def test_new_production_type_red_requires_behavior_failure_evidence(self) -> None:
        invalid_reds = (
            "RED：新增生产类型；tests_run > 0；目标行为断言失败",
            "RED：新增生产类型已建立最小可编译签名壳；tests_run == 0；目标行为断言失败",
            "RED：新增生产类型已建立最小可编译签名壳；tests_run > 0；测试失败",
        )
        for verification in invalid_reds:
            with self.subTest(verification=verification):
                code, out, err = run_validator(base_packet(VERIFICATION=[verification]))
                self.assertEqual(code, 3, err)
                assert out is not None
                self.assertIn(("invalid_red_evidence", "VERIFICATION"), evidence_kinds(out))

    def test_new_production_type_red_with_compilable_shell_is_valid(self) -> None:
        verification = "RED：新增生产类型已建立最小可编译签名壳；tests_run > 0；目标行为断言失败"
        code, out, err = run_validator(base_packet(VERIFICATION=[verification]))
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_non_red_compile_verification_remains_valid(self) -> None:
        packet = base_packet(
            VERIFICATION=[
                "mvn testCompile",
                "诊断历史输出 cannot find symbol",
                "确认生产类 OrderService 是否不存在",
            ]
        )
        code, out, err = run_validator(packet)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_missing_decision_budget_defaults_mechanical(self) -> None:
        packet = base_packet()
        del packet["DECISION_BUDGET"]
        code, out, err = run_validator(packet)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")
        self.assertEqual(out["decision_budget"], "MECHANICAL")

    def test_missing_write_set_blocked(self) -> None:
        packet = base_packet()
        del packet["WRITE_SET"]
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["status"], "BLOCKED")
        self.assertIn(("missing_field", "WRITE_SET"), evidence_kinds(out))

    def test_missing_acceptance_blocked(self) -> None:
        packet = base_packet()
        del packet["INDEPENDENT_ACCEPTANCE"]
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["status"], "BLOCKED")
        self.assertIn(("missing_field", "INDEPENDENT_ACCEPTANCE"), evidence_kinds(out))

    def test_missing_verification_blocked(self) -> None:
        packet = base_packet()
        del packet["VERIFICATION"]
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("missing_field", "VERIFICATION"), evidence_kinds(out))

    def test_missing_stop_condition_blocked(self) -> None:
        packet = base_packet()
        del packet["STOP_CONDITIONS"]
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("missing_field", "STOP_CONDITIONS"), evidence_kinds(out))

    def test_invalid_decision_budget_blocked(self) -> None:
        packet = base_packet(DECISION_BUDGET="FREE_FORM")
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_decision_budget", ""), evidence_kinds(out))

    def test_self_dependency_blocked(self) -> None:
        packet = base_packet(DEPENDENCIES=["T1"])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_dependency", "T1"), evidence_kinds(out))

    def test_non_list_dependency_blocked(self) -> None:
        packet = base_packet(DEPENDENCIES="T2")
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_field", "DEPENDENCIES"), evidence_kinds(out))

    def test_unknown_task_dependency_blocked(self) -> None:
        packet = base_packet(DEPENDENCIES=["T9"])
        tasks = [
            {
                "TASK_ID": "T1",
                "DELIVERABLE": "d",
                "WHY_ONE_TASK": "w",
                "INDEPENDENT_ACCEPTANCE": "a",
                "WRITE_SET": [],
                "READ_ONLY": [],
                "PREDECESSORS": [],
                "SUCCESSORS": [],
                "VERIFICATION_UNIT": "v",
                "PARALLELISM": "SERIAL",
            }
        ]
        code, out, err = run_validator(packet, tasks=tasks)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("unknown_task_dependency", "T9"), evidence_kinds(out))

    def test_unknown_decision_reference_blocked(self) -> None:
        packet = base_packet(DECISION_REFS=["D-999"])
        decisions = [base_decision("D-001")]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["status"], "BLOCKED")
        self.assertEqual(out["decision_required"], ["D-999"])
        self.assertIn(("unknown_decision_reference", ""), evidence_kinds(out))

    def test_unresolved_decision_reference_blocked(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", status="open")]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["decision_required"], ["D-001"])
        self.assertIn(("unresolved_decision_reference", ""), evidence_kinds(out))

    def test_coder_owned_decision_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", owner="coder")]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_valid_decision_reference(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = {"D-001": base_decision("D-001")}
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")
        self.assertEqual(out["decision_refs"], ["D-001"])

    def test_decision_missing_schema_field_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decision = base_decision("D-001")
        del decision["owner"]
        code, _, err = run_validator(packet, decisions=[decision])
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_decision_duplicate_id_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001"), base_decision("D-001")]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_decision_key_id_mismatch_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = {"D-001": base_decision("D-002")}
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_decision_affects_mismatch_blocked(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", affects=["T2"])]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["status"], "BLOCKED")
        self.assertEqual(out["decision_required"], ["D-001"])
        self.assertIn(("decision_affects_mismatch", ""), evidence_kinds(out))

    def test_empty_write_set_blocked(self) -> None:
        packet = base_packet(WRITE_SET=[])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_field", "WRITE_SET"), evidence_kinds(out))

    def test_whitespace_write_set_entry_blocked(self) -> None:
        packet = base_packet(WRITE_SET=["   "])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_path", "WRITE_SET"), evidence_kinds(out))

    def test_empty_verification_blocked(self) -> None:
        packet = base_packet(VERIFICATION=[])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_field", "VERIFICATION"), evidence_kinds(out))

    def test_whitespace_verification_entry_blocked(self) -> None:
        packet = base_packet(VERIFICATION=["  "])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_field", "VERIFICATION"), evidence_kinds(out))

    def test_duplicate_normalized_write_set_blocked(self) -> None:
        packet = base_packet(WRITE_SET=[r"src\a.py", "SRC/A.PY"])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("duplicate_path", "WRITE_SET"), evidence_kinds(out))

    def test_write_read_conflict_blocked(self) -> None:
        packet = base_packet(WRITE_SET=["src/a.py"], READ_ONLY=["SRC/A.PY"])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("write_read_conflict", ""), evidence_kinds(out))

    def test_empty_read_only_allowed(self) -> None:
        packet = base_packet(READ_ONLY=[])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_decision_conflict_blocked(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [
            base_decision(
                "D-001",
                fact_constraints=[{"path": "tests_changed", "allowed_values": [False]}],
            )
        ]
        facts = {"tests_changed": True}
        code, out, err = run_validator(packet, decisions=decisions, change_facts=facts)
        self.assertEqual(code, 3, err)
        assert out is not None
        conflict = next(item for item in out["evidence"] if item["kind"] == "DECISION_CONFLICT")
        self.assertEqual(conflict["decision_id"], "D-001")
        self.assertEqual(conflict["path"], "tests_changed")
        self.assertEqual(conflict["expected"], [False])
        self.assertEqual(conflict["actual"], True)
        self.assertEqual(out["decision_required"], ["D-001"])

    def test_decision_constraint_matches_valid(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [
            base_decision(
                "D-001",
                fact_constraints=[{"path": "tests_changed", "allowed_values": [False]}],
            )
        ]
        facts = {"tests_changed": False}
        code, out, err = run_validator(packet, decisions=decisions, change_facts=facts)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_missing_change_facts_fail_closed(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [
            base_decision(
                "D-001",
                fact_constraints=[{"path": "tests_changed", "allowed_values": [False]}],
            )
        ]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("missing_change_facts", ""), evidence_kinds(out))

    def test_malformed_fact_constraints_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", fact_constraints=[{"path": "tests_changed"}])]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_reserved_device_name_write_set_blocked(self) -> None:
        packet = base_packet(WRITE_SET=["src/CON.txt"])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_path", "WRITE_SET"), evidence_kinds(out))

    def test_decision_conflict_when_path_missing_even_null_allowed(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [
            base_decision(
                "D-001",
                fact_constraints=[{"path": "missing.path", "allowed_values": [None]}],
            )
        ]
        facts = {"tests_changed": False}
        code, out, err = run_validator(packet, decisions=decisions, change_facts=facts)
        self.assertEqual(code, 3, err)
        assert out is not None
        conflict = next(item for item in out["evidence"] if item["kind"] == "DECISION_CONFLICT")
        self.assertEqual(conflict["path"], "missing.path")
        self.assertEqual(conflict["expected"], [None])
        self.assertIsNone(conflict["actual"])
        self.assertEqual(out["decision_required"], ["D-001"])

    def test_decision_constraint_null_actual_matches(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [
            base_decision(
                "D-001",
                fact_constraints=[{"path": "maybe", "allowed_values": [None]}],
            )
        ]
        facts = {"maybe": None}
        code, out, err = run_validator(packet, decisions=decisions, change_facts=facts)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_blank_dotted_constraint_path_invalid_input(self) -> None:
        for path in ("", "  ", ".a", "a.", "a..b", "a. .b"):
            with self.subTest(path=path):
                packet = base_packet(DECISION_REFS=["D-001"])
                decisions = [
                    base_decision("D-001", fact_constraints=[{"path": path, "allowed_values": [False]}])
                ]
                code, _, err = run_validator(packet, decisions=decisions)
                self.assertEqual(code, 2, err)
                assert err is not None
                self.assertEqual(err["error"]["code"], "invalid_input")

    def test_decision_blank_value_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", value="   ")]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_decision_empty_affects_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", affects=[])]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_decision_duplicate_affects_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", affects=["T1", "T1"])]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_duplicate_dependency_blocked(self) -> None:
        packet = base_packet(DEPENDENCIES=["T2", "T2"])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("duplicate_dependency", "T2"), evidence_kinds(out))

    def test_non_string_write_set_entry_blocked_not_internal(self) -> None:
        packet = base_packet(WRITE_SET=["src/a.py", 42])
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_path", "WRITE_SET"), evidence_kinds(out))

    def test_fact_constraints_missing_means_no_constraints(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001")]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_fact_constraints_explicit_null_invalid_input(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", fact_constraints=None)]
        code, _, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_fact_constraints_empty_list_allowed(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001"])
        decisions = [base_decision("D-001", fact_constraints=[])]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["status"], "VALID")

    def test_non_string_task_id_blocked_not_internal(self) -> None:
        packet = base_packet(TASK_ID=42)
        code, out, err = run_validator(packet)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("invalid_field", "TASK_ID"), evidence_kinds(out))

    def test_duplicate_decision_refs_blocked(self) -> None:
        packet = base_packet(DECISION_REFS=["D-001", "D-001"])
        decisions = [base_decision("D-001")]
        code, out, err = run_validator(packet, decisions=decisions)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertIn(("duplicate_decision_ref", "D-001"), evidence_kinds(out))

    def test_duplicate_task_id_in_tasks_file_invalid_input(self) -> None:
        packet = base_packet()
        task = {
            "TASK_ID": "T1",
            "DELIVERABLE": "d",
            "WHY_ONE_TASK": "w",
            "INDEPENDENT_ACCEPTANCE": "a",
            "WRITE_SET": [],
            "READ_ONLY": [],
            "PREDECESSORS": [],
            "SUCCESSORS": [],
            "VERIFICATION_UNIT": "v",
            "PARALLELISM": "SERIAL",
        }
        code, _, err = run_validator(packet, tasks=[task, task])
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_undecodable_packet_exits_2(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-packet-bad-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b'{"TASK_ID": "\xff\xfe"}')
        path = Path(name)
        try:
            proc = subprocess.run(
                [sys.executable, str(VALIDATOR), "--packet", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
