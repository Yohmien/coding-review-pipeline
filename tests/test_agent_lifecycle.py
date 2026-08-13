# -*- coding: utf-8 -*-
"""Phase 8 Agent Lifecycle Governance tests.

Contracts covered:
- Section 118 decision matrix (fix-first, waiting audit, reviewer done,
  diff changed, advisor wait, parked capacity eviction).
- Section 119 SapWorkOrderService wait regression: WAIT only, never
  close/cancel/spawn replacement/re-split/change plan.
- Advisor runtime lock: unknown/stale runtime state is fail-closed and can
  only yield KEEP / WAIT / STOP; wait_observation_count is diagnostic only.
- Fixed action vocabulary and machine-readable JSON CLI.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "coding-review-pipeline", "scripts")
SCRIPT = os.path.join(SCRIPTS, "agent_lifecycle.py")

sys.path.insert(0, SCRIPTS)
import agent_lifecycle as al  # noqa: E402

FIXED_ACTIONS = frozenset({
    "KEEP", "WAIT", "RESUME_SAME", "PARK", "CLOSE_ALLOWED",
    "SPAWN_SUCCESSOR", "SPAWN_FRESH_REVIEWER", "STOP",
})


class TestActionVocabulary(unittest.TestCase):
    def test_fixed_action_set_is_exact(self):
        self.assertEqual(al.ACTIONS, FIXED_ACTIONS)

    def test_matrix_decisions_are_always_fixed_actions(self):
        cases = [
            dict(role="coder", status="FIX_REQUIRED", event="fix_first",
                 original_coder_available=True),
            dict(role="coder", status="FIX_REQUIRED", event="fix_first"),
            dict(role="coder", status="WAITING_AUDIT"),
            dict(role="reviewer", status="DONE"),
            dict(role="reviewer", status="DONE", event="diff_changed"),
            dict(role="advisor", status="RUNNING", event="wait_observation_timeout"),
            dict(role="coder", status="PARKED_REUSABLE"),
            dict(role="coder", status="PARKED_REUSABLE", event="slot_pressure",
                 is_oldest_parked=True, task_shipped=True),
            dict(role="advisor", status="RUNNING", terminal="proceed",
                 runtime_state="unknown"),
            dict(role="advisor", status="RUNNING", terminal="change",
                 runtime_state="stale"),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIn(al.decide(**case), FIXED_ACTIONS)


class TestDecisionMatrix(unittest.TestCase):
    """Section 118 matrix, one assertion per row."""

    def test_fix_first_original_coder_alive_resumes_same(self):
        action = al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                           original_coder_available=True)
        self.assertEqual(action, "RESUME_SAME")

    def test_fix_first_with_evidence_spawns_successor(self):
        for reason in ("unavailable", "runtime_gone", "unrecoverable"):
            with self.subTest(reason=reason):
                action = al.decide(role="coder", status="FIX_REQUIRED",
                                   event="fix_first", unavailability=reason)
                self.assertEqual(action, "SPAWN_SUCCESSOR")

    def test_waiting_audit_keeps(self):
        self.assertEqual(al.decide(role="coder", status="WAITING_AUDIT"), "KEEP")

    def test_reviewer_done_closes(self):
        self.assertEqual(al.decide(role="reviewer", status="DONE"), "CLOSE_ALLOWED")

    def test_code_changed_spawns_fresh_reviewer(self):
        self.assertEqual(
            al.decide(role="reviewer", status="DONE", event="diff_changed"),
            "SPAWN_FRESH_REVIEWER",
        )
        self.assertEqual(
            al.decide(role="reviewer", status="RUNNING", event="diff_changed"),
            "SPAWN_FRESH_REVIEWER",
        )

    def test_advisor_running_wait_timeout_waits(self):
        self.assertEqual(
            al.decide(role="advisor", status="RUNNING",
                      event="wait_observation_timeout"),
            "WAIT",
        )

    def test_parked_no_slot_pressure_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE"), "KEEP"
        )

    def test_parked_slot_pressure_closes(self):
        action = al.decide(role="coder", status="PARKED_REUSABLE",
                           event="slot_pressure", is_oldest_parked=True,
                           task_shipped=True, has_pending_dependent_fix=False)
        self.assertEqual(action, "CLOSE_ALLOWED")


class TestFixFirstEvidence(unittest.TestCase):
    """Section 45: successor requires explicit evidence, never by default."""

    def test_fix_first_missing_availability_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first"),
            "KEEP",
        )

    def test_fix_first_explicit_false_without_evidence_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      original_coder_available=False),
            "KEEP",
        )

    def test_fix_first_null_availability_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      original_coder_available=None),
            "KEEP",
        )

    def test_fix_first_evidence_without_flag_spawns_successor(self):
        self.assertEqual(
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      unavailability="runtime_gone"),
            "SPAWN_SUCCESSOR",
        )

    def test_fix_first_contradictory_available_and_evidence_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      original_coder_available=True, unavailability="unavailable")


class TestCapacityEviction(unittest.TestCase):
    """Section 48: only oldest parked + shipped + no pending fix may close."""

    def test_parked_for_rethink_always_keeps(self):
        cases = [
            {},
            {"event": "slot_pressure", "is_oldest_parked": True,
             "task_shipped": True, "has_pending_dependent_fix": False},
            {"event": "fix_first", "original_coder_available": True},
            {"event": "diff_changed"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    al.decide(role="coder", status="PARKED_FOR_RETHINK", **case),
                    "KEEP",
                )

    def test_parked_for_rethink_unknown_runtime_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_FOR_RETHINK",
                      event="slot_pressure", runtime_state="unknown"),
            "KEEP",
        )

    def test_parked_pressure_missing_flags_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE",
                      event="slot_pressure"),
            "KEEP",
        )

    def test_parked_pressure_missing_pending_fix_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE",
                      event="slot_pressure", is_oldest_parked=True,
                      task_shipped=True),
            "KEEP",
        )

    def test_parked_pressure_null_pending_fix_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE",
                      event="slot_pressure", is_oldest_parked=True,
                      task_shipped=True, has_pending_dependent_fix=None),
            "KEEP",
        )

    def test_parked_not_oldest_keeps(self):
        action = al.decide(role="coder", status="PARKED_REUSABLE",
                           event="slot_pressure", is_oldest_parked=False,
                           task_shipped=True)
        self.assertEqual(action, "KEEP")

    def test_parked_not_shipped_keeps(self):
        action = al.decide(role="coder", status="PARKED_REUSABLE",
                           event="slot_pressure", is_oldest_parked=True,
                           task_shipped=False)
        self.assertEqual(action, "KEEP")

    def test_parked_pending_dependent_fix_keeps(self):
        action = al.decide(role="coder", status="PARKED_REUSABLE",
                           event="slot_pressure", is_oldest_parked=True,
                           task_shipped=True, has_pending_dependent_fix=True)
        self.assertEqual(action, "KEEP")

    def test_open_coder_statuses_never_closed_under_pressure(self):
        for status in ("ACTIVE", "WAITING_VERIFICATION", "WAITING_AUDIT",
                       "FIX_REQUIRED"):
            with self.subTest(status=status):
                action = al.decide(role="coder", status=status,
                                   event="slot_pressure", is_oldest_parked=True,
                                   task_shipped=True)
                self.assertEqual(action, "KEEP")

    def test_advisor_running_never_closed_by_pressure(self):
        action = al.decide(role="advisor", status="RUNNING",
                           event="slot_pressure", runtime_state="known")
        self.assertEqual(action, "KEEP")


class TestReviewerFreshness(unittest.TestCase):
    """Sections 49-50: reviewer is fresh-only and never resumed."""

    def test_reviewer_never_resumed_same(self):
        for status in ("RUNNING", "DONE", "STALE"):
            for event in ("", "diff_changed", "slot_pressure", "fix_first"):
                with self.subTest(status=status, event=event):
                    action = al.decide(role="reviewer", status=status, event=event)
                    self.assertNotEqual(action, "RESUME_SAME")

    def test_stale_reviewer_always_replaced(self):
        self.assertEqual(
            al.decide(role="reviewer", status="STALE"), "SPAWN_FRESH_REVIEWER"
        )


class TestAdvisorLifecycle(unittest.TestCase):
    """Sections 51-52: sticky-within-decision, wait != failure."""

    def test_running_no_event_keeps(self):
        self.assertEqual(al.decide(role="advisor", status="RUNNING"), "KEEP")

    def test_verdict_proceed_or_change_closes(self):
        for terminal in ("proceed", "change"):
            with self.subTest(terminal=terminal):
                action = al.decide(role="advisor", status="RUNNING",
                                   terminal=terminal, runtime_state="known")
                self.assertEqual(action, "CLOSE_ALLOWED")

    def test_terminal_stop_cases_stop(self):
        for terminal in ("stop", "blocked", "cancelled", "runtime_failure"):
            with self.subTest(terminal=terminal):
                self.assertEqual(
                    al.decide(role="advisor", status="RUNNING", terminal=terminal),
                    "STOP",
                )

    def test_wait_observation_count_is_diagnostic_only(self):
        self.assertEqual(
            al.decide(role="advisor", status="RUNNING",
                      event="wait_observation_timeout", wait_observation_count=0),
            al.decide(role="advisor", status="RUNNING",
                      event="wait_observation_timeout", wait_observation_count=999),
        )


class TestAdvisorRuntimeLock(unittest.TestCase):
    """Unknown/stale runtime state is fail-closed: only KEEP/WAIT/STOP."""

    def test_fail_closed_before_role_dispatch_all_roles(self):
        cases = [
            dict(role="coder", status="FIX_REQUIRED", event="fix_first",
                 original_coder_available=True),
            dict(role="coder", status="FIX_REQUIRED", event="fix_first",
                 unavailability="runtime_gone"),
            dict(role="coder", status="PARKED_REUSABLE", event="slot_pressure",
                 is_oldest_parked=True, task_shipped=True),
            dict(role="reviewer", status="DONE"),
            dict(role="reviewer", status="STALE"),
            dict(role="reviewer", status="DONE", event="diff_changed"),
            dict(role="advisor", status="RUNNING", terminal="proceed"),
            dict(role="advisor", status="RUNNING",
                 event="wait_observation_timeout"),
        ]
        for runtime_state in ("unknown", "stale"):
            for case in cases:
                with self.subTest(runtime_state=runtime_state, case=case):
                    action = al.decide(runtime_state=runtime_state, **case)
                    self.assertIn(action, {"KEEP", "WAIT", "STOP"})

    def test_known_runtime_allows_normal_decisions(self):
        self.assertEqual(
            al.decide(role="reviewer", status="DONE", runtime_state="known"),
            "CLOSE_ALLOWED",
        )
        self.assertEqual(
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      original_coder_available=True, runtime_state="known"),
            "RESUME_SAME",
        )

    def test_unknown_runtime_never_closes_or_replaces(self):
        action = al.decide(role="advisor", status="RUNNING", terminal="proceed",
                           runtime_state="unknown")
        self.assertEqual(action, "STOP")
        self.assertNotIn(
            action,
            {"CLOSE_ALLOWED", "SPAWN_SUCCESSOR", "SPAWN_FRESH_REVIEWER",
             "RESUME_SAME", "PARK"},
        )

    def test_stale_runtime_never_closes(self):
        action = al.decide(role="advisor", status="RUNNING", terminal="change",
                           runtime_state="stale")
        self.assertEqual(action, "STOP")

    def test_unknown_runtime_wait_timeout_waits(self):
        action = al.decide(role="advisor", status="RUNNING",
                           event="wait_observation_timeout",
                           wait_observation_count=3, runtime_state="unknown")
        self.assertEqual(action, "WAIT")

    def test_unknown_runtime_no_event_keeps(self):
        self.assertEqual(
            al.decide(role="advisor", status="RUNNING", runtime_state="stale"),
            "KEEP",
        )

    def test_unknown_runtime_pressure_keeps(self):
        self.assertEqual(
            al.decide(role="advisor", status="RUNNING", event="slot_pressure",
                      runtime_state="unknown"),
            "KEEP",
        )

    def test_unknown_runtime_wait_count_still_diagnostic(self):
        for count in (0, 999):
            with self.subTest(count=count):
                self.assertEqual(
                    al.decide(role="advisor", status="RUNNING",
                              event="wait_observation_timeout",
                              wait_observation_count=count,
                              runtime_state="unknown"),
                    "WAIT",
                )

    def test_unknown_or_stale_runtime_only_fail_closed_actions(self):
        events = ("", "slot_pressure", "wait_observation_timeout", "diff_changed",
                  "fix_first", "ship", "integration_fix_needed")
        terminals = (None, "proceed", "change", "stop", "blocked", "cancelled",
                     "runtime_failure")
        for runtime_state in ("unknown", "stale"):
            for event in events:
                for terminal in terminals:
                    with self.subTest(runtime_state=runtime_state, event=event,
                                      terminal=terminal):
                        action = al.decide(role="advisor", status="RUNNING",
                                           event=event, terminal=terminal,
                                           runtime_state=runtime_state)
                        self.assertIn(action, {"KEEP", "WAIT", "STOP"})


class TestSapWorkOrderServiceRegression(unittest.TestCase):
    """Section 119: advisor checking SapWorkOrderService long wait."""

    def _fixture(self):
        return {
            "role": "advisor",
            "status": "RUNNING",
            "event": "wait_observation_timeout",
            "context": {
                "target": "SapWorkOrderService local query branch "
                          "and exception-propagation test boundary",
                "response": None,
                "blocker": None,
                "context_request": None,
                "runtime_failure": None,
                "workspace_mutation": None,
            },
            "wait_observation_count": 12,
        }

    def test_wait_observation_without_terminal_result_waits(self):
        action = al.decide(**self._fixture())
        self.assertEqual(action, "WAIT")
        self.assertIn(action, {"KEEP", "WAIT"})

    def test_wait_never_triggers_close_cancel_spawn_resplit_or_plan_change(self):
        forbidden = {
            "CLOSE_ALLOWED", "STOP", "SPAWN_SUCCESSOR", "SPAWN_FRESH_REVIEWER",
            "RESUME_SAME", "PARK",
        }
        action = al.decide(**self._fixture())
        self.assertNotIn(action, forbidden)
        # No replacement role exists in the fixed vocabulary at all.
        self.assertNotIn("SPAWN_ADVISOR", al.ACTIONS)


class TestCoderTransitions(unittest.TestCase):
    """Section 44 lifecycle diagram."""

    def test_core_transitions(self):
        transitions = [
            ("WAITING_AUDIT", "fix_first", "FIX_REQUIRED"),
            ("WAITING_AUDIT", "ship", "PARKED_REUSABLE"),
            ("PARKED_REUSABLE", "integration_fix_needed", "FIX_REQUIRED"),
        ]
        for status, event, expected in transitions:
            with self.subTest(status=status, event=event):
                self.assertEqual(al.transition(status, event), expected)

    def test_unknown_transition_raises(self):
        with self.assertRaises(ValueError):
            al.transition("ACTIVE", "fix_first")

    def test_close_transition_removed(self):
        with self.assertRaises(ValueError):
            al.transition("PARKED_REUSABLE", "close")

    def test_close_allowed_only_via_eviction_conditions(self):
        # CLOSE_ELIGIBLE is no longer a status: no unconditional bypass.
        with self.assertRaises(ValueError):
            al.decide(role="coder", status="CLOSE_ELIGIBLE")

    def test_ship_parks_and_integration_fix_resumes_original(self):
        self.assertEqual(
            al.decide(role="coder", status="WAITING_AUDIT", event="ship"), "PARK"
        )
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE",
                      event="integration_fix_needed"),
            "RESUME_SAME",
        )

    def test_double_ship_on_parked_keeps(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE", event="ship"),
            "KEEP",
        )


class TestValidation(unittest.TestCase):
    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="bogus", status="RUNNING")

    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="coder", status="ACTIVE", event="bogus")

    def test_unknown_status_for_role_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="reviewer", status="PARKED_REUSABLE")

    def test_string_where_bool_expected_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="coder", status="PARKED_REUSABLE",
                      event="slot_pressure", is_oldest_parked="true",
                      task_shipped=True)

    def test_number_where_bool_expected_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      original_coder_available=1)

    def test_string_wait_observation_count_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="advisor", status="RUNNING",
                      event="wait_observation_timeout",
                      wait_observation_count="12")

    def test_float_wait_observation_count_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="advisor", status="RUNNING",
                      event="wait_observation_timeout",
                      wait_observation_count=12.5)

    def test_bool_wait_observation_count_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="advisor", status="RUNNING",
                      event="wait_observation_timeout",
                      wait_observation_count=True)

    def test_null_runtime_state_treated_as_known(self):
        self.assertEqual(
            al.decide(role="reviewer", status="DONE", runtime_state=None),
            "CLOSE_ALLOWED",
        )

    def test_null_event_treated_as_empty(self):
        self.assertEqual(
            al.decide(role="coder", status="WAITING_AUDIT", event=None),
            "KEEP",
        )

    def test_null_bool_flags_do_not_close(self):
        self.assertEqual(
            al.decide(role="coder", status="PARKED_REUSABLE",
                      event="slot_pressure", is_oldest_parked=None,
                      task_shipped=None),
            "KEEP",
        )

    def test_unknown_unavailability_raises(self):
        with self.assertRaises(ValueError):
            al.decide(role="coder", status="FIX_REQUIRED", event="fix_first",
                      unavailability="maybe")

    def test_context_must_be_object(self):
        with self.assertRaises(ValueError):
            al.decide(role="advisor", status="RUNNING", context="details")

    def test_context_allows_only_sap_fixture_fields(self):
        al.decide(role="advisor", status="RUNNING", context={})
        cases = [
            {"runtime_stte": "known"},
            {"terminal": "stop"},
            {"wait_observation_count": 1},
        ]
        for context in cases:
            with self.subTest(context=context):
                with self.assertRaises(ValueError):
                    al.decide(role="advisor", status="RUNNING",
                              context=context)

    def test_context_field_types_are_strict(self):
        cases = [
            {"target": " "},
            {"target": 1},
            {"response": 1},
            {"blocker": False},
            {"context_request": []},
            {"runtime_failure": "false"},
            {"workspace_mutation": "false"},
        ]
        for context in cases:
            with self.subTest(context=context):
                with self.assertRaises(ValueError):
                    al.decide(role="advisor", status="RUNNING",
                              context=context)

    def test_unknown_top_level_field_raises(self):
        with self.assertRaises(TypeError):
            al.decide(role="advisor", status="RUNNING", runtime_stte="known")


class TestCli(unittest.TestCase):
    def _run(self, payload):
        return subprocess.run(
            [sys.executable, SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_cli_round_trip(self):
        proc = self._run({
            "role": "advisor",
            "status": "RUNNING",
            "event": "wait_observation_timeout",
            "wait_observation_count": 12,
            "runtime_state": "unknown",
        })
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {"action": "WAIT"})

    def test_cli_invalid_json_exits_2_with_machine_readable_error(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT],
            input="not-json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["action"], None)
        self.assertIn("error", json.loads(proc.stdout))

    def test_cli_unknown_role_exits_2(self):
        proc = self._run({"role": "bogus", "status": "RUNNING"})
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["action"], None)

    def test_cli_string_bool_field_exits_2(self):
        proc = self._run({"role": "coder", "status": "PARKED_REUSABLE",
                          "event": "slot_pressure", "is_oldest_parked": "true",
                          "task_shipped": True})
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_missing_status_exits_2(self):
        proc = self._run({"role": "coder"})
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_unknown_top_level_typo_exits_2(self):
        proc = self._run({
            "role": "advisor",
            "status": "RUNNING",
            "runtime_stte": "known",
        })
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_unknown_context_field_exits_2(self):
        proc = self._run({
            "role": "advisor",
            "status": "RUNNING",
            "context": {"runtime_stte": "known"},
        })
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_eviction_fields_rejected_outside_coder_slot_pressure(self):
        cases = [
            {"role": "coder", "status": "ACTIVE", "event": "ship"},
            {"role": "reviewer", "status": "RUNNING",
             "event": "slot_pressure"},
            {"role": "advisor", "status": "RUNNING",
             "event": "slot_pressure"},
        ]
        for case in cases:
            with self.subTest(case=case):
                proc = self._run({
                    **case,
                    "is_oldest_parked": True,
                    "task_shipped": True,
                    "has_pending_dependent_fix": False,
                })
                self.assertEqual(proc.returncode, 2)
                self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_wait_count_rejected_outside_advisor_wait_timeout(self):
        cases = [
            {"role": "coder", "status": "ACTIVE",
             "event": "wait_observation_timeout"},
            {"role": "reviewer", "status": "RUNNING",
             "event": "wait_observation_timeout"},
            {"role": "advisor", "status": "RUNNING", "event": ""},
        ]
        for case in cases:
            with self.subTest(case=case):
                proc = self._run({**case, "wait_observation_count": 1})
                self.assertEqual(proc.returncode, 2)
                self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_terminal_rejected_for_non_advisor_before_fail_closed(self):
        cases = [
            ("coder", "ACTIVE", "known"),
            ("coder", "ACTIVE", "unknown"),
            ("coder", "ACTIVE", "stale"),
            ("reviewer", "RUNNING", "known"),
            ("reviewer", "RUNNING", "unknown"),
            ("reviewer", "RUNNING", "stale"),
        ]
        for role, status, runtime_state in cases:
            with self.subTest(role=role, runtime_state=runtime_state):
                proc = self._run({
                    "role": role,
                    "status": status,
                    "terminal": "stop",
                    "runtime_state": runtime_state,
                })
                self.assertEqual(proc.returncode, 2)
                self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_unavailability_rejected_outside_coder_fix_first(self):
        cases = [
            ("coder", "ACTIVE", "ship", "known"),
            ("coder", "ACTIVE", "ship", "unknown"),
            ("coder", "ACTIVE", "ship", "stale"),
            ("reviewer", "RUNNING", "", "known"),
            ("reviewer", "RUNNING", "", "unknown"),
            ("reviewer", "RUNNING", "", "stale"),
            ("advisor", "RUNNING", "", "known"),
            ("advisor", "RUNNING", "", "unknown"),
            ("advisor", "RUNNING", "", "stale"),
        ]
        for role, status, event, runtime_state in cases:
            with self.subTest(role=role, event=event,
                              runtime_state=runtime_state):
                proc = self._run({
                    "role": role,
                    "status": status,
                    "event": event,
                    "unavailability": "runtime_gone",
                    "runtime_state": runtime_state,
                })
                self.assertEqual(proc.returncode, 2)
                self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_original_coder_available_rejected_outside_coder_fix_first(self):
        cases = [
            ("coder", "ACTIVE", "ship", "known", True),
            ("coder", "ACTIVE", "ship", "unknown", False),
            ("coder", "ACTIVE", "ship", "stale", True),
            ("reviewer", "RUNNING", "", "known", True),
            ("reviewer", "RUNNING", "", "unknown", False),
            ("reviewer", "RUNNING", "", "stale", True),
            ("advisor", "RUNNING", "", "known", True),
            ("advisor", "RUNNING", "", "unknown", False),
            ("advisor", "RUNNING", "", "stale", True),
        ]
        for role, status, event, runtime_state, available in cases:
            with self.subTest(role=role, event=event,
                              runtime_state=runtime_state, available=available):
                proc = self._run({
                    "role": role,
                    "status": status,
                    "event": event,
                    "original_coder_available": available,
                    "runtime_state": runtime_state,
                })
                self.assertEqual(proc.returncode, 2)
                self.assertIsNone(json.loads(proc.stdout)["action"])

    def test_cli_advisor_terminal_remains_valid(self):
        for runtime_state, expected in (("known", "CLOSE_ALLOWED"),
                                        ("unknown", "STOP")):
            with self.subTest(runtime_state=runtime_state):
                proc = self._run({
                    "role": "advisor",
                    "status": "RUNNING",
                    "terminal": "proceed",
                    "runtime_state": runtime_state,
                })
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(json.loads(proc.stdout), {"action": expected})


if __name__ == "__main__":
    unittest.main()
