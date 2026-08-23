#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 12 Completion Gate (plan sections 98-101).

Deterministic completion gate: checks that every required gate is present and
current, and otherwise emits BLOCKED with a deterministic reason list. It does
NOT make business-semantics judgments (that belongs to a fresh reviewer); it
only checks gate completeness.

Input: a run ledger JSON (the ``run_ledger.py`` schema) and optional change
facts (to compute the current diff fingerprint). Output is machine-readable
UTF-8 JSON on stdout; structured errors on stderr. Exit codes follow
``crp_common``: 0 COMPLETE_ALLOWED / 3 BLOCKED (policy_blocked) /
2 invalid_input / 1 internal_error.

This module reuses ``crp_common`` plumbing and ``run_ledger``'s fingerprint /
verification-validity definitions; it does not reimplement the ledger schema,
atomic write, hashing, or git wrappers.

Section 99 check -> deterministic reason mapping:
  plan schema valid?                        -> invalid_plan
  plan fingerprint current?                 -> plan_stale
  all tasks ship?                           -> pending_coder
  task verdict fingerprints current?        -> stale_review
  integration verdict current?              -> integration_missing
  verification fingerprints current?        -> stale_verification
  unknown changed files?                    -> unknown_file
  pending fix / pending audit /
  running required agent?                   -> pending_coder
  unresolved decision?                      -> unresolved_decision
  deterministic blocker (verification fail) -> machine_blocker
  required verification missing?            -> stale_verification

Fail-closed: missing required evidence always yields BLOCKED; a fresh
fingerprint can only be proven against a real current diff fingerprint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import crp_common
import run_ledger
from crp_common import (
    CrpError,
    EXIT_OK,
    emit_error,
    exit_code,
    reconfigure_stdio,
)


COMPLETE_ALLOWED = "COMPLETE_ALLOWED"
BLOCKED = "BLOCKED"

INVALID_PLAN = "invalid_plan"
PLAN_STALE = "plan_stale"
STALE_REVIEW = "stale_review"
STALE_VERIFICATION = "stale_verification"
PENDING_CODER = "pending_coder"
UNKNOWN_FILE = "unknown_file"
UNRESOLVED_DECISION = "unresolved_decision"
MACHINE_BLOCKER = "machine_blocker"
INTEGRATION_MISSING = "integration_missing"

REASON_CODES = frozenset({
    INVALID_PLAN,
    PLAN_STALE,
    STALE_REVIEW,
    STALE_VERIFICATION,
    PENDING_CODER,
    UNKNOWN_FILE,
    UNRESOLVED_DECISION,
    MACHINE_BLOCKER,
    INTEGRATION_MISSING,
})

# Mirrors run_ledger's running-agent semantics (section 60): an agent still in
# a non-terminal lifecycle state, or last observed running, blocks completion.
_AGENT_RUNNING_STATES = frozenset({
    "ACTIVE",
    "RUNNING",
    "WAITING_VERIFICATION",
    "WAITING_AUDIT",
    "FIX_REQUIRED",
})
_AGENT_RUNNING_RUNTIME = frozenset({"running", "active"})

# A decision is only resolved when its status is an explicit terminal status.
_DECISION_RESOLVED = frozenset({
    "resolved",
    "decided",
    "approved",
    "accepted",
    "confirmed",
    "closed",
    "done",
})


def _fingerprint_current(stored_fp: object, current_fp: object) -> bool:
    """A stored fingerprint is current only against a real current fingerprint."""

    if not isinstance(current_fp, str) or not current_fp:
        return False
    return isinstance(stored_fp, str) and stored_fp == current_fp


def _agent_is_running(agent: object) -> bool:
    """Block when an agent may still be running or its state fields are malformed.

    Both state fields must be strings before any membership test. A missing or
    non-string field (list/dict/...) cannot prove a non-running state, so it
    fails closed as still-running and surfaces as ``pending_coder`` - never as
    a TypeError bubbling up to ``internal_error``.
    """

    if not isinstance(agent, dict):
        return False
    state = agent.get("lifecycle_state")
    if not isinstance(state, str):
        return True
    if state in _AGENT_RUNNING_STATES:
        return True
    runtime = agent.get("last_observed_runtime_state")
    if not isinstance(runtime, str):
        return True
    return runtime in _AGENT_RUNNING_RUNTIME


def _check_plan_validity(planned_ids: set[str] | None, reasons: list[str]) -> None:
    if planned_ids is None:
        reasons.append("invalid_plan")


def _check_plan_freshness(ledger: dict, reasons: list[str]) -> None:
    baseline = ledger.get("baseline")
    stored = baseline.get("plan_fingerprint") if isinstance(baseline, dict) else None
    plan = ledger.get("plan")
    if not isinstance(stored, str) or not stored:
        reasons.append("plan_stale")
        return
    if not isinstance(plan, dict):
        reasons.append("plan_stale")
        return
    try:
        current = run_ledger.plan_fingerprint(plan)
    except (TypeError, ValueError, RecursionError):
        # A plan whose content is not JSON-serializable (or too deeply nested)
        # cannot prove freshness: fail closed. Narrowly scoped so unrelated
        # programming errors in run_ledger are not mislabeled plan_stale.
        reasons.append("plan_stale")
        return
    if current != stored:
        reasons.append("plan_stale")


def _check_verification_records(
    records: object,
    current_fp: object,
    reasons: list[str],
) -> None:
    records = records if isinstance(records, list) else []
    if not records:
        # Required verification missing: fail closed, never pass by default.
        reasons.append("stale_verification")
        return
    for record in records:
        if not run_ledger.valid_verification(record):
            reasons.append("stale_verification")
            continue
        if record.get("exit_code") != 0:
            reasons.append("machine_blocker")
        if not _fingerprint_current(record.get("diff_fingerprint"), current_fp):
            reasons.append("stale_verification")


def _check_scenario_checks(
    ledger: dict,
    current_fp: object,
    reasons: list[str],
) -> None:
    """Check scenario validation completeness.

    Fail-closed when plan declares has_scenario_conditions but tasks lack
    scenario_checks or any check is not all_pass=True on current fingerprint.
    """
    plan = ledger.get("plan")
    if not isinstance(plan, dict):
        return
    if not plan.get("has_scenario_conditions"):
        return
    tasks = ledger.get("tasks")
    if not isinstance(tasks, dict):
        return
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        checks = task.get("scenario_checks")
        if not isinstance(checks, list) or not checks:
            reasons.append("stale_verification")
            continue
        for check in checks:
            if not isinstance(check, dict):
                reasons.append("stale_verification")
                continue
            if check.get("all_pass") is not True:
                reasons.append("machine_blocker")
            if not _fingerprint_current(
                check.get("diff_fingerprint"), current_fp
            ):
                reasons.append("stale_verification")


def _check_constraint_mappings(ledger: dict, reasons: list[str]) -> None:
    """Fail closed when plan declares MUST constraints but tasks lost mappings.

    Mapping completeness against the registry is enforced at packet validation
    time; here we only verify that tasks still carry their non-empty
    constraint_mappings block at completion.
    """
    plan = ledger.get("plan")
    if not isinstance(plan, dict):
        return
    if not plan.get("has_must_constraints"):
        return
    tasks = ledger.get("tasks")
    if not isinstance(tasks, dict):
        return
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        mappings = task.get("constraint_mappings")
        if not isinstance(mappings, dict) or not mappings:
            reasons.append("missing_constraint_mappings")


def _check_tasks(
    ledger: dict,
    planned_ids: set[str] | None,
    current_fp: object,
    reasons: list[str],
) -> None:
    tasks = ledger.get("tasks")
    if not isinstance(tasks, dict):
        reasons.append("pending_coder")
        return
    if planned_ids is not None:
        missing = planned_ids - set(tasks)
        if missing:
            reasons.append("pending_coder")
    for task in tasks.values():
        if not isinstance(task, dict):
            reasons.append("pending_coder")
            continue
        for field in ("pending_fix", "pending_audit"):
            if field in task and task[field] is not False:
                # True, or any non-bool value, is fail-closed: the flag is only
                # safe when it is explicitly False (or absent).
                reasons.append("pending_coder")
        if task.get("latest_verdict") != "ship":
            reasons.append("pending_coder")
            continue
        if not _fingerprint_current(task.get("verdict_diff_fingerprint"), current_fp):
            reasons.append("stale_review")
        _check_verification_records(task.get("verification"), current_fp, reasons)


def _check_agents(ledger: dict, reasons: list[str]) -> None:
    agents = ledger.get("agents")
    if not isinstance(agents, dict):
        reasons.append("pending_coder")
        return
    for agent in agents.values():
        if not isinstance(agent, dict):
            # A malformed agent record could hide a still-running agent: fail
            # closed rather than silently skipping it.
            reasons.append("pending_coder")
            return
        if _agent_is_running(agent):
            reasons.append("pending_coder")
            return


def _check_integration(ledger: dict, current_fp: object, reasons: list[str]) -> None:
    integration = ledger.get("integration")
    if not isinstance(integration, dict):
        reasons.append("integration_missing")
        return
    if integration.get("latest_verdict") != "ship":
        reasons.append("integration_missing")
        return
    if not _fingerprint_current(integration.get("verdict_diff_fingerprint"), current_fp):
        reasons.append("integration_missing")


def _check_unknown_files(
    ledger: dict,
    change_facts: object,
    reasons: list[str],
) -> None:
    if not isinstance(change_facts, dict):
        return
    changed: set[str] = set()
    for field in ("changed_files", "untracked_files"):
        value = change_facts.get(field)
        if not isinstance(value, list):
            # A missing/None/non-list file set cannot be verified against the
            # write sets: fail closed by flagging an unknown file.
            reasons.append("unknown_file")
            return
        for path in value:
            if isinstance(path, str):
                changed.add(path)
            else:
                # A non-string path cannot be matched to any write set: fail
                # closed rather than silently skipping it.
                reasons.append("unknown_file")
                return
    if not changed:
        return
    tasks = ledger.get("tasks")
    tasks = tasks if isinstance(tasks, dict) else {}
    covered: set[str] = set()
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        for field in ("write_set", "actual_changed_files"):
            value = task.get(field)
            if not isinstance(value, list):
                # A missing/None/non-list ownership list cannot be verified:
                # fail closed by flagging an unknown file.
                reasons.append("unknown_file")
                return
            for path in value:
                if not isinstance(path, str):
                    reasons.append("unknown_file")
                    return
                covered.add(path)
    if changed - covered:
        reasons.append("unknown_file")


def _check_decisions(ledger: dict, reasons: list[str]) -> None:
    decisions = ledger.get("decisions")
    if not isinstance(decisions, dict):
        reasons.append("unresolved_decision")
        return
    for decision in decisions.values():
        if not isinstance(decision, dict):
            reasons.append("unresolved_decision")
            continue
        status = decision.get("status")
        if not isinstance(status, str) or status.strip().lower() not in _DECISION_RESOLVED:
            reasons.append("unresolved_decision")


def evaluate(ledger: dict, change_facts: object = None) -> dict:
    """Run every completion-gate check and return the deterministic conclusion."""

    if not isinstance(ledger, dict):
        raise CrpError("invalid_input", "ledger must be a JSON object")
    current_fp = (
        run_ledger.diff_fingerprint(change_facts)
        if isinstance(change_facts, dict)
        else None
    )
    planned_ids: set[str] | None = None
    try:
        planned_ids = run_ledger.validate_plan_tasks(
            ledger.get("plan"), require_tasks=True
        )
    except CrpError:
        planned_ids = None
    reasons: list[str] = []
    _check_plan_validity(planned_ids, reasons)
    _check_plan_freshness(ledger, reasons)
    _check_tasks(ledger, planned_ids, current_fp, reasons)
    _check_scenario_checks(ledger, current_fp, reasons)
    _check_constraint_mappings(ledger, reasons)
    _check_agents(ledger, reasons)
    _check_integration(ledger, current_fp, reasons)
    _check_unknown_files(ledger, change_facts, reasons)
    _check_decisions(ledger, reasons)
    reasons = sorted(set(reasons))
    if reasons:
        return {"conclusion": BLOCKED, "reasons": reasons}
    return {"conclusion": COMPLETE_ALLOWED}


def _load_json_arg(path: str, label: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrpError("invalid_input", f"{label} file not found", path=path) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError(
            "invalid_input",
            f"{label} file is not valid JSON",
            path=path,
            error=str(error),
        ) from error


class _CrpArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit_error(CrpError("invalid_input", "invalid arguments", detail=message))
        raise SystemExit(exit_code("invalid_input"))


def main(argv: list[str] | None = None) -> int:
    reconfigure_stdio()
    parser = _CrpArgumentParser(
        prog="completion_gate.py",
        description="Deterministic completion gate: check required gates, never judge semantics.",
    )
    parser.add_argument("--ledger", required=True, help="run ledger JSON file")
    parser.add_argument(
        "--facts",
        default=None,
        help="change facts JSON file (optional; computes the current diff fingerprint)",
    )
    args = parser.parse_args(argv)
    try:
        ledger = _load_json_arg(args.ledger, "ledger")
        if not isinstance(ledger, dict):
            raise CrpError("invalid_input", "ledger must be a JSON object", path=args.ledger)
        change_facts = None
        if args.facts is not None:
            change_facts = _load_json_arg(args.facts, "change facts")
            if not isinstance(change_facts, dict):
                raise CrpError(
                    "invalid_input",
                    "change facts must be a JSON object",
                    path=args.facts,
                )
        result = evaluate(ledger, change_facts)
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    if result["conclusion"] == COMPLETE_ALLOWED:
        return EXIT_OK
    return exit_code("policy_blocked")


if __name__ == "__main__":
    sys.exit(main())
