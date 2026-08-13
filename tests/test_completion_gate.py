# -*- coding: utf-8 -*-
"""Phase 12 Completion Gate tests (execution plan section 123).

Coverage:
- all fresh -> COMPLETE_ALLOWED
- stale review -> BLOCKED (stale_review)
- stale verification -> BLOCKED (stale_verification)
- pending coder -> BLOCKED (pending_coder)
- unknown file -> BLOCKED (unknown_file)
- unresolved decision -> BLOCKED (unresolved_decision)
- machine blocker -> BLOCKED (machine_blocker)
- integration missing -> BLOCKED (integration_missing)
- plan fingerprint mismatch -> BLOCKED (plan_stale)
- missing evidence -> fail closed (BLOCKED, never default allow)
- CLI exit codes: 0 (allowed) / 3 (blocked) / 2 (invalid input)

Fixtures build a fully-green single-task ledger; each test then introduces one
defect and asserts the deterministic reason code. All CLI tests use temporary
directories and never touch the real repository.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "coding-review-pipeline" / "scripts"
SCRIPT = SCRIPTS / "completion_gate.py"

sys.path.insert(0, str(SCRIPTS))
import crp_common  # noqa: E402
import completion_gate  # noqa: E402
import run_ledger  # noqa: E402


def _plan(**overrides):
    plan = {
        "objective": "fix login bug",
        "tasks": ["T1"],
        "dependencies": [],
        "interfaces": ["auth"],
        "constraints": ["no new deps"],
        "acceptance": ["login works"],
        "decisions": [],
    }
    plan.update(overrides)
    return plan


def _facts(**overrides):
    facts = {
        "changed_files": ["src/A.java"],
        "untracked_files": [],
        "diff_ranges": {"src/A.java": [{"start": 1, "end": 3}]},
    }
    facts.update(overrides)
    return facts


def _verification(fp, exit_code=0):
    return {
        "command": "python -m unittest tests.test_change_facts",
        "exit_code": exit_code,
        "failure_count": 0,
        "diff_fingerprint": fp,
        "timestamp": "2026-08-13T00:00:00+00:00",
    }


def _task(fp, **overrides):
    task = {
        "task_id": "T1",
        "deliverable": "fix",
        "write_set": ["src/A.java"],
        "read_only": [],
        "dependencies": [],
        "state": "shipped",
        "owner_coder": "coder-1",
        "reuse_policy": "sticky",
        "actual_changed_files": ["src/A.java"],
        "verification": [_verification(fp)],
        "review_round": 1,
        "latest_verdict": "ship",
        "verdict_diff_fingerprint": fp,
        "pending_fix": False,
        "pending_audit": False,
    }
    task.update(overrides)
    return task


def _green(**ledger_overrides):
    """A fully-green single-task run: returns (ledger, change_facts)."""
    facts = _facts()
    fp = run_ledger.diff_fingerprint(facts)
    plan = _plan()
    ledger = {
        "schema_version": 1,
        "run_id": "r1",
        "repo_root": "/repo",
        "plan": plan,
        "baseline": {
            "plan_fingerprint": run_ledger.plan_fingerprint(plan),
            "created_at": "2026-08-13T00:00:00+00:00",
            "stage": "review",
        },
        "models": {},
        "decisions": {},
        "tasks": {"T1": _task(fp)},
        "agents": {},
        "integration": {"latest_verdict": "ship", "verdict_diff_fingerprint": fp},
        "events": [],
    }
    ledger.update(ledger_overrides)
    return ledger, facts


class TestEvaluate(unittest.TestCase):
    def test_all_fresh_allows(self):
        ledger, facts = _green()
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result, {"conclusion": "COMPLETE_ALLOWED"})

    def test_stale_review_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verdict_diff_fingerprint"] = "stale"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.STALE_REVIEW, result["reasons"])

    def test_shipped_verdict_missing_fingerprint_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verdict_diff_fingerprint"] = ""
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.STALE_REVIEW, result["reasons"])

    def test_stale_verification_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verification"][0]["diff_fingerprint"] = "old"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.STALE_VERIFICATION, result["reasons"])

    def test_invalid_verification_record_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verification"] = [{"command": "pytest"}]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.STALE_VERIFICATION, result["reasons"])

    def test_missing_verification_fail_closed(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verification"] = []
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.STALE_VERIFICATION, result["reasons"])

    def test_pending_coder_fix_first_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["latest_verdict"] = "fix-first"
        ledger["tasks"]["T1"]["pending_fix"] = True
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_pending_coder_pending_fix_flag_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["pending_fix"] = True
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_pending_coder_pending_audit_flag_blocks(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["pending_audit"] = True
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_running_required_agent_blocks(self):
        ledger, facts = _green()
        ledger["agents"] = {
            "coder-1": {
                "agent_id": "coder-1",
                "role": "coder",
                "task_id": "T1",
                "lifecycle_state": "RUNNING",
            }
        }
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_unknown_file_blocks(self):
        ledger, facts = _green()
        facts = _facts(changed_files=["src/A.java", "src/B.java"])
        ledger["tasks"]["T1"]["verdict_diff_fingerprint"] = run_ledger.diff_fingerprint(facts)
        ledger["tasks"]["T1"]["verification"] = [
            _verification(run_ledger.diff_fingerprint(facts))
        ]
        ledger["integration"]["verdict_diff_fingerprint"] = run_ledger.diff_fingerprint(facts)
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_unresolved_decision_blocks(self):
        ledger, facts = _green()
        ledger["decisions"] = {"D1": {"status": "open", "domain": "scope"}}
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNRESOLVED_DECISION, result["reasons"])

    def test_machine_blocker_blocks(self):
        ledger, facts = _green()
        fp = run_ledger.diff_fingerprint(facts)
        ledger["tasks"]["T1"]["verification"] = [_verification(fp, exit_code=1)]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.MACHINE_BLOCKER, result["reasons"])

    def test_integration_missing_blocks(self):
        ledger, facts = _green()
        ledger["integration"] = {}
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INTEGRATION_MISSING, result["reasons"])

    def test_integration_stale_verdict_blocks(self):
        ledger, facts = _green()
        ledger["integration"]["verdict_diff_fingerprint"] = "stale"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INTEGRATION_MISSING, result["reasons"])

    def test_plan_stale_blocks(self):
        ledger, facts = _green()
        ledger["baseline"]["plan_fingerprint"] = "bogus"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PLAN_STALE, result["reasons"])

    def test_plan_task_missing_from_ledger_blocks(self):
        # plan declares T2 but the ledger only carries T1: gate completeness
        # must block even though no changed file belongs to T2.
        ledger, facts = _green()
        ledger["plan"]["tasks"] = ["T1", "T2"]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_plan_task_missing_object_list_blocks(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"TASK_ID": "T1"}, {"TASK_ID": "T2"}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_plan_task_missing_dict_mapping_blocks(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = {"T1": {}, "T2": {}}
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_plan_tasks_scalar_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = "T1"
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_tasks_list_with_unparseable_entry_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = ["T1", 123]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_missing_tasks_key_invalid_plan(self):
        # R3 root cause: a dict plan without a tasks key must NOT be treated as
        # "zero tasks = all ship"; it is an invalid plan.
        ledger, facts = _green()
        del ledger["plan"]["tasks"]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_tasks_none_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = None
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_tasks_empty_list_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = []
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_tasks_empty_dict_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = {}
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_object_missing_task_id_key_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"foo": "T1"}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_object_task_id_non_string_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"TASK_ID": 123}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_object_task_id_uppercase_allows(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"TASK_ID": "T1"}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "COMPLETE_ALLOWED")

    def test_plan_object_task_id_lowercase_allows(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"task_id": "T1"}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "COMPLETE_ALLOWED")

    def test_plan_object_id_allows(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"id": "T1"}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "COMPLETE_ALLOWED")

    def test_plan_object_first_nonempty_string_wins(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = [{"TASK_ID": "", "task_id": "T1"}]
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "COMPLETE_ALLOWED")

    def test_plan_dict_mapping_allows(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = {"T1": {}}
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "COMPLETE_ALLOWED")

    def test_plan_dict_mapping_non_string_key_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = {"T1": {}, 1: {}}
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_dict_mapping_empty_string_key_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"]["tasks"] = {"": {}}
        ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(
            ledger["plan"]
        )
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])

    def test_plan_is_list_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"] = ["T1"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])
        self.assertIn(completion_gate.PLAN_STALE, result["reasons"])

    def test_plan_is_string_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"] = "T1"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])
        self.assertIn(completion_gate.PLAN_STALE, result["reasons"])

    def test_plan_is_int_invalid_plan(self):
        ledger, facts = _green()
        ledger["plan"] = 42
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.INVALID_PLAN, result["reasons"])
        self.assertIn(completion_gate.PLAN_STALE, result["reasons"])

    def test_ledger_tasks_non_dict_pending_coder(self):
        ledger, facts = _green()
        ledger["tasks"] = ["T1"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_write_set_non_list_unknown_file(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["write_set"] = "src/A.java"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_actual_changed_files_non_list_unknown_file(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["actual_changed_files"] = "src/A.java"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_unknown_file_non_list_changed_files_fail_closed(self):
        ledger, facts = _green()
        facts = _facts(changed_files="not-a-list")
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_unknown_file_non_list_untracked_files_fail_closed(self):
        ledger, facts = _green()
        facts = _facts(untracked_files={"a": 1})
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_write_set_non_string_entry_fail_closed(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["write_set"] = ["src/A.java", 123]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_actual_changed_files_non_string_entry_fail_closed(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["actual_changed_files"] = ["src/A.java", 123]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_write_set_none_unknown_file(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["write_set"] = None
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_write_set_missing_key_unknown_file(self):
        ledger, facts = _green()
        del ledger["tasks"]["T1"]["write_set"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_actual_changed_files_none_unknown_file(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["actual_changed_files"] = None
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_actual_changed_files_missing_key_unknown_file(self):
        ledger, facts = _green()
        del ledger["tasks"]["T1"]["actual_changed_files"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_facts_changed_files_none_unknown_file(self):
        ledger, facts = _green()
        facts = _facts(changed_files=None)
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_facts_changed_files_missing_key_unknown_file(self):
        ledger, facts = _green()
        facts = _facts()
        del facts["changed_files"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_facts_untracked_files_none_unknown_file(self):
        ledger, facts = _green()
        facts = _facts(untracked_files=None)
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_facts_untracked_files_missing_key_unknown_file(self):
        ledger, facts = _green()
        facts = _facts()
        del facts["untracked_files"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_agents_non_dict_record_pending_coder(self):
        ledger, facts = _green()
        ledger["agents"] = {"a1": "RUNNING"}
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_agent_lifecycle_state_list_is_malformed_pending_coder(self):
        ledger, facts = _green()
        ledger["agents"] = {
            "coder-1": {
                "agent_id": "coder-1",
                "role": "coder",
                "lifecycle_state": ["RUNNING"],
                "last_observed_runtime_state": "idle",
            }
        }
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_agent_runtime_state_dict_is_malformed_pending_coder(self):
        ledger, facts = _green()
        ledger["agents"] = {
            "coder-1": {
                "agent_id": "coder-1",
                "role": "coder",
                "lifecycle_state": "DONE",
                "last_observed_runtime_state": {"state": "running"},
            }
        }
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_pending_fix_non_bool_pending_coder(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["pending_fix"] = 1
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_pending_audit_non_bool_pending_coder(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["pending_audit"] = "true"
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_unknown_file_non_string_list_entry_fail_closed(self):
        ledger, facts = _green()
        facts = _facts(changed_files=["src/A.java", 123])
        ledger["tasks"]["T1"]["verdict_diff_fingerprint"] = run_ledger.diff_fingerprint(facts)
        ledger["tasks"]["T1"]["verification"] = [
            _verification(run_ledger.diff_fingerprint(facts))
        ]
        ledger["integration"]["verdict_diff_fingerprint"] = run_ledger.diff_fingerprint(facts)
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNKNOWN_FILE, result["reasons"])

    def test_missing_change_facts_fail_closed(self):
        ledger, _ = _green()
        result = completion_gate.evaluate(ledger, change_facts=None)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.STALE_REVIEW, result["reasons"])
        self.assertIn(completion_gate.STALE_VERIFICATION, result["reasons"])
        self.assertIn(completion_gate.INTEGRATION_MISSING, result["reasons"])

    def test_missing_plan_key_fail_closed(self):
        ledger, facts = _green()
        del ledger["plan"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PLAN_STALE, result["reasons"])

    def test_missing_tasks_key_fail_closed(self):
        ledger, facts = _green()
        del ledger["tasks"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_missing_decisions_key_fail_closed(self):
        ledger, facts = _green()
        del ledger["decisions"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.UNRESOLVED_DECISION, result["reasons"])

    def test_missing_agents_key_fail_closed(self):
        ledger, facts = _green()
        del ledger["agents"]
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertIn(completion_gate.PENDING_CODER, result["reasons"])

    def test_reasons_have_fixed_order(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verdict_diff_fingerprint"] = "stale"
        ledger["tasks"]["T1"]["pending_fix"] = True
        result = completion_gate.evaluate(ledger, change_facts=facts)
        self.assertEqual(result["conclusion"], "BLOCKED")
        self.assertEqual(
            result["reasons"],
            [completion_gate.PENDING_CODER, completion_gate.STALE_REVIEW],
        )

    def test_reasons_are_stable_codes(self):
        ledger, facts = _green()
        ledger["integration"] = {}
        result = completion_gate.evaluate(ledger, change_facts=facts)
        for reason in result["reasons"]:
            self.assertIn(
                reason,
                completion_gate.REASON_CODES,
            )


class TestPlanValidationConsistency(unittest.TestCase):
    def test_run_ledger_predicate_agrees_with_completion_gate(self):
        cases = (
            (_plan(), True),
            ({"objective": "x"}, False),
            (dict(_plan(), tasks=None), False),
            (dict(_plan(), tasks=[]), False),
            (dict(_plan(), tasks={}), False),
            (dict(_plan(), tasks="T1"), False),
            (dict(_plan(), tasks=["T1", 123]), False),
            (dict(_plan(), tasks=[{"foo": "T1"}]), False),
            (dict(_plan(), tasks=[{"TASK_ID": ""}]), False),
            (dict(_plan(), tasks={"T1": {}}), True),
            (dict(_plan(), tasks=[{"TASK_ID": "T1"}]), True),
            (dict(_plan(), tasks=[{"task_id": "T1"}]), True),
            (dict(_plan(), tasks=[{"id": "T1"}]), True),
            (dict(_plan(), tasks={"T1": {}, "T2": {}}), True),
        )
        for plan, expect_valid in cases:
            with self.subTest(plan=plan):
                try:
                    run_ledger.validate_plan_tasks(plan, require_tasks=True)
                    predicate_valid = True
                except crp_common.CrpError:
                    predicate_valid = False
                self.assertEqual(predicate_valid, expect_valid)

                ledger, facts = _green()
                ledger["plan"] = plan
                ledger["baseline"]["plan_fingerprint"] = run_ledger.plan_fingerprint(plan)
                result = completion_gate.evaluate(ledger, change_facts=facts)
                gate_invalid = completion_gate.INVALID_PLAN in result.get("reasons", [])
                self.assertEqual(gate_invalid, not predicate_valid)


class TestCli(unittest.TestCase):
    def _run(self, ledger_data, facts_data=None):
        tmp = Path(tempfile.mkdtemp(prefix="crp-gate-"))
        ledger_path = tmp / "ledger.json"
        ledger_path.write_text(json.dumps(ledger_data, ensure_ascii=False), encoding="utf-8")
        argv = [sys.executable, "-B", str(SCRIPT), "--ledger", str(ledger_path)]
        if facts_data is not None:
            facts_path = tmp / "facts.json"
            facts_path.write_text(json.dumps(facts_data, ensure_ascii=False), encoding="utf-8")
            argv += ["--facts", str(facts_path)]
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_cli_allowed_exits_0(self):
        ledger, facts = _green()
        proc = self._run(ledger, facts)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"conclusion": "COMPLETE_ALLOWED"})

    def test_cli_blocked_exits_3(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["verdict_diff_fingerprint"] = "stale"
        proc = self._run(ledger, facts)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["conclusion"], "BLOCKED")
        self.assertIn("stale_review", out["reasons"])

    def test_cli_write_set_none_exits_3(self):
        ledger, facts = _green()
        ledger["tasks"]["T1"]["write_set"] = None
        proc = self._run(ledger, facts)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["conclusion"], "BLOCKED")
        self.assertIn("unknown_file", out["reasons"])

    def test_cli_write_set_missing_key_exits_3(self):
        ledger, facts = _green()
        del ledger["tasks"]["T1"]["write_set"]
        proc = self._run(ledger, facts)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["conclusion"], "BLOCKED")
        self.assertIn("unknown_file", out["reasons"])

    def test_cli_agent_field_list_exits_3(self):
        ledger, facts = _green()
        ledger["agents"] = {
            "coder-1": {
                "agent_id": "coder-1",
                "role": "coder",
                "lifecycle_state": ["RUNNING"],
                "last_observed_runtime_state": "idle",
            }
        }
        proc = self._run(ledger, facts)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["conclusion"], "BLOCKED")
        self.assertIn("pending_coder", out["reasons"])

    def test_cli_invalid_ledger_json_exits_2(self):
        tmp = Path(tempfile.mkdtemp(prefix="crp-gate-"))
        ledger_path = tmp / "ledger.json"
        ledger_path.write_text("{ not json", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--ledger", str(ledger_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_undecodable_ledger_exits_2(self):
        tmp = Path(tempfile.mkdtemp(prefix="crp-gate-"))
        ledger_path = tmp / "ledger.json"
        ledger_path.write_bytes(b'{"schema_version": 1, "bad": "\xff"}')
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--ledger", str(ledger_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_undecodable_facts_exits_2(self):
        ledger, _ = _green()
        tmp = Path(tempfile.mkdtemp(prefix="crp-gate-"))
        ledger_path = tmp / "ledger.json"
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        facts_path = tmp / "facts.json"
        facts_path.write_bytes(b'{"changed_files": ["\xff"]}')
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--ledger",
                str(ledger_path),
                "--facts",
                str(facts_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_missing_ledger_arg_exits_2(self):
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid_input", proc.stderr)


if __name__ == "__main__":
    unittest.main()
