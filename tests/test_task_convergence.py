# -*- coding: utf-8 -*-
"""任务级确定性收敛路由的封闭契约测试。"""

import copy
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "coding-review-pipeline", "scripts")
SCRIPT = os.path.join(SCRIPTS, "task_convergence.py")

sys.path.insert(0, SCRIPTS)
import task_convergence as tc  # noqa: E402


MATERIAL_FIELDS = (
    "DELIVERABLE", "INTERFACES", "WRITE_SET", "DEPENDENCIES",
    "CONSTRAINTS", "ACCEPTANCE", "VERIFICATION", "DECISIONS",
)


def contract(**changes):
    value = {
        "DELIVERABLE": "交付确定性收敛路由",
        "INTERFACES": "UTF-8 JSON stdin/stdout",
        "WRITE_SET": ["scripts/task_convergence.py"],
        "DEPENDENCIES": [],
        "CONSTRAINTS": "仅使用 Python 标准库",
        "ACCEPTANCE": "固定路由与退出码符合契约",
        "VERIFICATION": ["python -m unittest tests.test_task_convergence"],
        "DECISIONS": [],
    }
    value.update(changes)
    return value


def payload(**changes):
    value = {
        "task_id": "T-LIFECYCLE",
        "contract_revision": 1,
        "fix_round": 0,
        "total_fix_rounds": 0,
        "review_verdict": "fix-first",
        "original_coder_available": True,
        "implementer_continuity": "preserve",
        "old_contract": contract(),
        "new_contract": contract(),
        "diagnosis_complete": False,
    }
    value.update(changes)
    return value


def recontract_payload(**changes):
    value = payload(
        fix_round=3,
        total_fix_rounds=3,
        rev1_rounds=3,
        review_verdict="rethink",
        diagnosis_complete=True,
        prior_route="ENTER_RETHINK",
        coder_status="PARKED_FOR_RETHINK",
        new_contract=contract(ACCEPTANCE="revision 2 验收契约"),
    )
    value.update(changes)
    return value


def failure_context(**changes):
    value = {
        "goal": "交付确定性生命周期治理",
        "revision_rounds": {"1": 3, "2": 3},
        "diagnosis": {
            "status": "change",
            "summary": "原契约混合 agent 与 task 两层治理",
        },
        "changed_assumptions": ["task route 与 agent action 分离"],
        "current_diff_fingerprint": "sha256:current-diff",
        "verification": {
            "command": "python -m unittest tests.test_task_convergence",
            "exit_code": 1,
            "failure_count": 1,
            "freshness": "fresh",
        },
        "current_blockers": ["revision 2 已用尽三轮"],
        "repeated_signatures": ["fix-first:contract-schema"],
        "unresolved_decisions": [],
        "safe_workspace_state": {
            "dirty": True,
            "coder_status": "PARKED_FOR_RETHINK",
            "write_set_preserved": True,
        },
    }
    value.update(changes)
    return value


def escalation_payload(**changes):
    value = payload(
        contract_revision=2,
        fix_round=3,
        total_fix_rounds=6,
        rev1_rounds=3,
        failure_context=failure_context(),
    )
    value.update(changes)
    return value


def early_escalation_payload(rev1_rounds, fix_round, **changes):
    value = payload(
        contract_revision=2,
        fix_round=fix_round,
        total_fix_rounds=rev1_rounds + fix_round,
        rev1_rounds=rev1_rounds,
        review_verdict="rethink",
        failure_context=failure_context(
            revision_rounds={"1": rev1_rounds, "2": fix_round}),
    )
    value.update(changes)
    return value


class TestFixedRoutesAndCounters(unittest.TestCase):
    def test_task_routes_are_separate_from_agent_actions(self):
        self.assertEqual(tc.ROUTES, frozenset({
            "CONTINUE_FIX", "ENTER_RETHINK", "RESUME_SAME",
            "SPAWN_SUCCESSOR", "SHIP", "STOP",
            "TASK_ESCALATION_REQUIRED",
        }))

    def test_revision_one_rounds_zero_to_two_continue(self):
        for fix_round in (0, 1, 2):
            with self.subTest(fix_round=fix_round):
                result = tc.route_task(**payload(
                    fix_round=fix_round,
                    total_fix_rounds=fix_round,
                ))
                self.assertEqual(result["route"], "CONTINUE_FIX")

    def test_revision_one_round_three_enters_rethink(self):
        result = tc.route_task(**payload(fix_round=3,
                                         total_fix_rounds=3))
        self.assertEqual(result["route"], "ENTER_RETHINK")
        self.assertEqual(result["coder_status"], "PARKED_FOR_RETHINK")

    def test_revision_one_rethink_enters_at_any_round(self):
        for fix_round in (0, 1, 2, 3):
            with self.subTest(fix_round=fix_round):
                result = tc.route_task(**payload(
                    fix_round=fix_round,
                    total_fix_rounds=fix_round,
                    review_verdict="rethink",
                ))
                self.assertEqual(result["route"], "ENTER_RETHINK")
                self.assertEqual(result["coder_status"],
                                 "PARKED_FOR_RETHINK")

    def test_revision_two_rounds_zero_to_two_continue(self):
        for fix_round in (0, 1, 2):
            with self.subTest(fix_round=fix_round):
                result = tc.route_task(**payload(
                    contract_revision=2,
                    fix_round=fix_round,
                    total_fix_rounds=3 + fix_round,
                    rev1_rounds=3,
                ))
                self.assertEqual(result["route"], "CONTINUE_FIX")

    def test_revision_two_round_three_escalates(self):
        result = tc.route_task(**escalation_payload())
        self.assertEqual(result["route"], "TASK_ESCALATION_REQUIRED")

    def test_revision_two_rethink_escalates_at_any_round(self):
        cases = ((0, 0), (1, 0), (2, 1), (3, 2), (3, 3))
        for rev1_rounds, fix_round in cases:
            with self.subTest(rev1_rounds=rev1_rounds,
                              fix_round=fix_round):
                result = tc.route_task(**early_escalation_payload(
                    rev1_rounds, fix_round))
                self.assertEqual(result["route"],
                                 "TASK_ESCALATION_REQUIRED")
                self.assertEqual(
                    result["failure_capsule"]["rev1"]["rounds"],
                    rev1_rounds,
                )
                self.assertEqual(
                    result["failure_capsule"]["rev2"]["rounds"],
                    fix_round,
                )

    def test_ship_uses_consistent_counter_state(self):
        cases = [
            payload(fix_round=2, total_fix_rounds=2,
                    review_verdict="ship"),
            payload(contract_revision=2, fix_round=2,
                    total_fix_rounds=5, rev1_rounds=3,
                    review_verdict="ship"),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(tc.route_task(**case)["route"], "SHIP")

    def test_ship_with_material_change_stops_with_evidence(self):
        result = tc.route_task(**payload(
            review_verdict="ship",
            new_contract=contract(ACCEPTANCE="revision 2 验收契约"),
        ))
        self.assertEqual(result["route"], "STOP")
        self.assertEqual(result["changed_contract_fields"], ["ACCEPTANCE"])

    def test_ship_with_consistent_fingerprint_ships(self):
        result = tc.route_task(**payload(review_verdict="ship"))
        self.assertEqual(result["route"], "SHIP")
        self.assertEqual(result["changed_contract_fields"], [])

    def test_inconsistent_or_skipped_counters_raise(self):
        cases = [
            {"total_fix_rounds": 7},
            {"fix_round": 1, "total_fix_rounds": 0},
            {"fix_round": 1, "total_fix_rounds": 2},
            {"fix_round": 1, "total_fix_rounds": 1, "rev1_rounds": 3},
            {"contract_revision": 2, "fix_round": 0,
             "total_fix_rounds": 3},
            {"contract_revision": 2, "fix_round": 0,
             "total_fix_rounds": 3, "rev1_rounds": None},
            {"contract_revision": 2, "fix_round": 0,
             "total_fix_rounds": 3, "rev1_rounds": 2},
            {"contract_revision": 2, "fix_round": 2,
             "total_fix_rounds": 4, "rev1_rounds": 3},
            {"contract_revision": 2, "fix_round": 2,
             "total_fix_rounds": 6, "rev1_rounds": 3},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    tc.route_task(**payload(**changes))

    def test_revision_two_accepts_actual_rev1_rounds(self):
        for rev1_rounds in (0, 1, 2, 3):
            for fix_round in (0, 1, 2):
                with self.subTest(rev1_rounds=rev1_rounds,
                                  fix_round=fix_round):
                    result = tc.route_task(**payload(
                        contract_revision=2,
                        fix_round=fix_round,
                        total_fix_rounds=rev1_rounds + fix_round,
                        rev1_rounds=rev1_rounds,
                    ))
                    self.assertEqual(result["route"], "CONTINUE_FIX")


class TestMaterialContractSchema(unittest.TestCase):
    def test_all_material_fields_are_required(self):
        for field in MATERIAL_FIELDS:
            with self.subTest(field=field):
                value = contract()
                del value[field]
                with self.assertRaises(ValueError):
                    tc.contract_fingerprint(value)

    def test_null_is_not_equivalent_to_missing(self):
        for field in MATERIAL_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    tc.contract_fingerprint(contract(**{field: None}))

    def test_unknown_contract_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            tc.contract_fingerprint(contract(WRITE_SETS=["typo"]))

    def test_task_id_and_name_metadata_are_allowed_and_not_material(self):
        old = contract(task_id="T1", name="旧名称")
        new = contract(task_id="T2", name="新名称")
        self.assertEqual(tc.contract_fingerprint(old),
                         tc.contract_fingerprint(new))

    def test_nonblank_string_fields_are_enforced(self):
        for field in ("DELIVERABLE", "INTERFACES", "CONSTRAINTS",
                      "ACCEPTANCE"):
            for value in ("", "   ", 1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        tc.contract_fingerprint(contract(**{field: value}))

    def test_nonempty_list_fields_are_enforced(self):
        for field in ("WRITE_SET", "VERIFICATION"):
            for value in ([], [""], ["   "], [1], "item"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        tc.contract_fingerprint(contract(**{field: value}))

    def test_optional_list_fields_allow_empty_but_require_strings(self):
        for field in ("DEPENDENCIES", "DECISIONS"):
            tc.contract_fingerprint(contract(**{field: []}))
            for value in ([""], [1], "item"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        tc.contract_fingerprint(contract(**{field: value}))

    def test_each_locked_field_changes_fingerprint(self):
        replacements = {
            "DELIVERABLE": "新交付物",
            "INTERFACES": "新接口",
            "WRITE_SET": ["new.py"],
            "DEPENDENCIES": ["T-PREV"],
            "CONSTRAINTS": "新约束",
            "ACCEPTANCE": "新验收",
            "VERIFICATION": ["python -m unittest"],
            "DECISIONS": ["锁定新决定"],
        }
        old = contract()
        for field, value in replacements.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    tc.contract_fingerprint(old),
                    tc.contract_fingerprint(contract(**{field: value})),
                )


class TestDiagnosisAndRecontract(unittest.TestCase):
    def test_early_rethink_diagnosis_is_valid_at_any_round(self):
        for fix_round in (0, 1, 2, 3):
            with self.subTest(fix_round=fix_round):
                result = tc.route_task(**recontract_payload(
                    fix_round=fix_round,
                    total_fix_rounds=fix_round,
                    rev1_rounds=fix_round,
                ))
                self.assertEqual(result["route"], "RESUME_SAME")
                self.assertEqual(result["contract_revision"], 2)
                self.assertEqual(result["fix_round"], 0)
                self.assertEqual(result["total_fix_rounds"], fix_round)
                self.assertEqual(result["rev1_rounds"], fix_round)

    def test_diagnosis_requires_exact_parked_prior_state(self):
        cases = [
            {"fix_round": 2, "total_fix_rounds": 2},
            {"prior_route": None},
            {"prior_route": "CONTINUE_FIX"},
            {"coder_status": None},
            {"coder_status": "PARKED_REUSABLE"},
            {"contract_revision": 2, "fix_round": 0,
             "total_fix_rounds": 3, "rev1_rounds": 3},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    tc.route_task(**recontract_payload(**changes))

    def test_prior_state_fields_rejected_without_complete_diagnosis(self):
        for field, value in (("prior_route", "ENTER_RETHINK"),
                             ("coder_status", "PARKED_FOR_RETHINK")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    tc.route_task(**payload(**{field: value}))

    def test_material_change_resets_only_current_round(self):
        result = tc.route_task(**recontract_payload())
        self.assertEqual(result["route"], "RESUME_SAME")
        self.assertEqual(result["contract_revision"], 2)
        self.assertEqual(result["fix_round"], 0)
        self.assertEqual(result["total_fix_rounds"], 3)
        self.assertEqual(result["rev1_rounds"], 3)

    def test_diagnosis_complete_ship_is_invalid(self):
        with self.assertRaises(ValueError):
            tc.route_task(**recontract_payload(review_verdict="ship"))

    def test_no_material_change_stops_without_reset(self):
        result = tc.route_task(**recontract_payload(new_contract=contract()))
        self.assertEqual(result["route"], "STOP")
        self.assertEqual(result["contract_revision"], 1)
        self.assertEqual(result["fix_round"], 3)

    def test_rename_only_cannot_reset(self):
        result = tc.route_task(**recontract_payload(
            old_contract=contract(task_id="T1", name="旧名称"),
            new_contract=contract(task_id="T2", name="新名称"),
        ))
        self.assertEqual(result["route"], "STOP")

    def test_successor_conditions_are_strict(self):
        cases = [
            ({"original_coder_available": False}, "STOP"),
            ({"original_coder_available": None}, "STOP"),
            ({"implementer_continuity": "successor_recommended",
              "responsibility_boundary_changed": False}, "STOP"),
            ({"implementer_continuity": "successor_recommended",
              "responsibility_boundary_changed": True}, "SPAWN_SUCCESSOR"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(tc.route_task(
                    **recontract_payload(**changes))["route"], expected)

    def test_available_coder_can_use_diagnosed_successor_exception(self):
        result = tc.route_task(**recontract_payload(
            original_coder_available=True,
            implementer_continuity="successor_recommended",
            responsibility_boundary_changed=True,
        ))
        self.assertEqual(result["route"], "SPAWN_SUCCESSOR")

    def test_false_plus_unavailability_evidence_spawns_successor(self):
        result = tc.route_task(**recontract_payload(
            original_coder_available=False,
            coder_unavailability="unavailable",
        ))
        self.assertEqual(result["route"], "SPAWN_SUCCESSOR")

    def test_unavailability_evidence_with_available_true_raises(self):
        with self.assertRaises(ValueError):
            tc.route_task(**recontract_payload(
                coder_unavailability="unavailable"))

    def test_unavailability_evidence_requires_completed_diagnosis(self):
        with self.assertRaises(ValueError):
            tc.route_task(**payload(coder_unavailability="unavailable"))

    def test_successor_exception_persists_when_coder_flag_false(self):
        result = tc.route_task(**recontract_payload(
            original_coder_available=False,
            implementer_continuity="successor_recommended",
            responsibility_boundary_changed=True,
        ))
        self.assertEqual(result["route"], "SPAWN_SUCCESSOR")


class TestFailureContext(unittest.TestCase):
    def test_escalation_requires_nonempty_failure_context(self):
        for value in (None, {}):
            with self.subTest(value=value):
                data = escalation_payload()
                if value is None:
                    del data["failure_context"]
                else:
                    data["failure_context"] = value
                with self.assertRaises(ValueError):
                    tc.route_task(**data)

    def test_failure_context_is_rejected_for_non_escalation(self):
        with self.assertRaises(ValueError):
            tc.route_task(**payload(failure_context=failure_context()))

    def test_failure_context_requires_exact_top_level_keys(self):
        required = tuple(failure_context())
        for field in required:
            with self.subTest(field=field):
                value = failure_context()
                del value[field]
                with self.assertRaises(ValueError):
                    tc.route_task(**escalation_payload(failure_context=value))
        with self.assertRaises(ValueError):
            tc.route_task(**escalation_payload(
                failure_context=failure_context(extra="typo")))

    def test_nested_schema_and_sensitive_keys_are_rejected(self):
        cases = [
            failure_context(diagnosis={
                "status": "change", "summary": "有效", "raw_notes": "x"}),
            failure_context(verification={
                "command": "test", "exit_code": 1, "failure_count": 1,
                "freshness": "fresh", "history": []}),
            failure_context(safe_workspace_state={
                "dirty": True, "coder_status": "PARKED_FOR_RETHINK",
                "write_set_preserved": True, "secret": "x"}),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tc.route_task(**escalation_payload(failure_context=value))

    def test_failure_context_types_are_strict(self):
        cases = [
            {"goal": " "},
            {"revision_rounds": {"1": 3}},
            {"revision_rounds": {"1": 3, "2": 2}},
            {"diagnosis": {"status": "unknown", "summary": "x"}},
            {"changed_assumptions": [1]},
            {"current_diff_fingerprint": ""},
            {"verification": {"command": "test", "exit_code": True,
                              "failure_count": 0, "freshness": "fresh"}},
            {"verification": {"command": "test", "exit_code": 1,
                              "failure_count": 1, "freshness": "stale"}},
            {"current_blockers": "none"},
            {"repeated_signatures": [""]},
            {"unresolved_decisions": [1]},
            {"safe_workspace_state": {"dirty": "true",
                                      "coder_status": "PARKED_FOR_RETHINK",
                                      "write_set_preserved": True}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    tc.route_task(**escalation_payload(
                        failure_context=failure_context(**changes)))

    def test_failure_capsule_is_complete_and_contains_no_sensitive_keys(self):
        result = tc.route_task(**escalation_payload())
        capsule = result["failure_capsule"]
        self.assertEqual(capsule["task"], {
            "task_id": "T-LIFECYCLE",
            "goal": "交付确定性生命周期治理",
        })
        self.assertEqual(capsule["rev1"]["rounds"], 3)
        self.assertEqual(capsule["rev2"]["rounds"], 3)
        self.assertEqual(capsule["diagnosis"]["status"], "change")
        rendered = json.dumps(capsule, ensure_ascii=False).lower()
        for forbidden in ("raw", "conversation", "history", "secret"):
            self.assertNotIn(forbidden, rendered)


class TestValidationAndCli(unittest.TestCase):
    def _run(self, data):
        return subprocess.run(
            [sys.executable, SCRIPT],
            input=json.dumps(data, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_required_top_level_types_are_strict(self):
        cases = [
            {"task_id": None},
            {"task_id": 1},
            {"contract_revision": None},
            {"contract_revision": True},
            {"fix_round": None},
            {"fix_round": True},
            {"total_fix_rounds": None},
            {"total_fix_rounds": True},
            {"review_verdict": None},
            {"review_verdict": "maybe"},
            {"original_coder_available": "true"},
            {"implementer_continuity": None},
            {"implementer_continuity": "replace"},
            {"old_contract": None},
            {"old_contract": []},
            {"new_contract": None},
            {"new_contract": []},
            {"diagnosis_complete": None},
            {"diagnosis_complete": 1},
            {"responsibility_boundary_changed": "true"},
            {"rev1_rounds": True},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    tc.route_task(**payload(**changes))

    def test_unknown_top_level_typo_is_rejected_by_function_and_cli(self):
        data = payload(runtime_stte="known")
        with self.assertRaises(TypeError):
            tc.route_task(**data)
        proc = self._run(data)
        self.assertEqual(proc.returncode, tc.EXIT_INVALID_INPUT)
        self.assertIsNone(json.loads(proc.stdout)["route"])

    def test_cli_missing_required_top_level_field_is_invalid(self):
        data = payload()
        del data["task_id"]
        proc = self._run(data)
        self.assertEqual(proc.returncode, tc.EXIT_INVALID_INPUT)
        self.assertIsNone(json.loads(proc.stdout)["route"])

    def test_repeated_input_is_idempotent(self):
        data = escalation_payload()
        self.assertEqual(tc.route_task(**copy.deepcopy(data)),
                         tc.route_task(**copy.deepcopy(data)))

    def test_cli_exit_codes_are_stable(self):
        regular = self._run(payload(fix_round=1, total_fix_rounds=1))
        blocked = self._run(recontract_payload(new_contract=contract()))
        escalated = self._run(escalation_payload())
        self.assertEqual(regular.returncode, 0)
        self.assertEqual(blocked.returncode, tc.EXIT_POLICY_BLOCKED)
        self.assertEqual(escalated.returncode,
                         tc.EXIT_ESCALATION_REQUIRED)

    def test_cli_nested_raw_leak_is_invalid(self):
        value = failure_context()
        value["diagnosis"]["conversation"] = "raw history"
        proc = self._run(escalation_payload(failure_context=value))
        self.assertEqual(proc.returncode, tc.EXIT_INVALID_INPUT)
        self.assertIsNone(json.loads(proc.stdout)["route"])


if __name__ == "__main__":
    unittest.main()
