#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 8 Agent Lifecycle Governance.

Deterministic lifecycle decisions for coder / reviewer / advisor agents.
The output is always exactly one of the fixed actions; free-form advice is
not allowed. The CLI reads one JSON object from stdin (UTF-8) and writes one
JSON object to stdout.

Invariants:
- Coder is sticky: fix-first returns to the original coder when available.
- Reviewer is fresh-only: any diff change invalidates the old verdict.
- Advisor is sticky within one decision; wait observation timeout is NOT an
  execution failure, and wait_observation_count is diagnostic only.
- Unknown/stale runtime state is fail-closed before role dispatch: no role
  may close, replace or redispatch; only KEEP/WAIT/STOP are allowed.
- CLOSE_ALLOWED for a coder only comes from the eviction triple
  (PARKED_REUSABLE + slot_pressure + oldest + shipped + no pending fix).
- SPAWN_SUCCESSOR requires explicit unavailability evidence
  (unavailable / runtime_gone / unrecoverable); missing evidence keeps.
"""

import json
import sys

ACTIONS = frozenset({
    "KEEP", "WAIT", "RESUME_SAME", "PARK", "CLOSE_ALLOWED",
    "SPAWN_SUCCESSOR", "SPAWN_FRESH_REVIEWER", "STOP",
})

ROLES = frozenset({"coder", "reviewer", "advisor"})

EVENTS = frozenset({
    "", "fix_first", "ship", "slot_pressure", "diff_changed",
    "wait_observation_timeout", "integration_fix_needed",
})

CODER_STATUSES = frozenset({
    "UNSPAWNED", "ACTIVE", "WAITING_VERIFICATION", "WAITING_AUDIT",
    "FIX_REQUIRED", "PARKED_REUSABLE", "PARKED_FOR_RETHINK",
})

REVIEWER_STATUSES = frozenset({"RUNNING", "DONE", "STALE"})

ADVISOR_STATUSES = frozenset({"RUNNING", "DONE"})

ADVISOR_TERMINALS = frozenset({
    "proceed", "change", "stop", "blocked", "cancelled", "runtime_failure",
})

RUNTIME_STATES = frozenset({"known", "unknown", "stale"})

UNAVAILABILITY = frozenset({"unavailable", "runtime_gone", "unrecoverable"})

CONTEXT_FIELDS = frozenset({
    "target", "response", "blocker", "context_request", "runtime_failure",
    "workspace_mutation",
})

# Coder statuses that must never be closed, whatever the slot pressure.
CODER_OPEN_STATUSES = frozenset({
    "ACTIVE", "WAITING_VERIFICATION", "WAITING_AUDIT", "FIX_REQUIRED",
})

# Section 44 coder lifecycle transitions.
CODER_TRANSITIONS = {
    ("UNSPAWNED", "spawn"): "ACTIVE",
    ("ACTIVE", "verification_started"): "WAITING_VERIFICATION",
    ("WAITING_VERIFICATION", "audit_started"): "WAITING_AUDIT",
    ("WAITING_AUDIT", "fix_first"): "FIX_REQUIRED",
    ("WAITING_AUDIT", "ship"): "PARKED_REUSABLE",
    ("FIX_REQUIRED", "fix_complete"): "WAITING_VERIFICATION",
    ("PARKED_REUSABLE", "integration_fix_needed"): "FIX_REQUIRED",
}


def _bool_or_none(name, value):
    if value is not None and not isinstance(value, bool):
        raise ValueError("%s must be bool or null, got %s"
                         % (name, type(value).__name__))


def _validate_context(context):
    if context is None:
        return
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    unknown = set(context) - CONTEXT_FIELDS
    if unknown:
        raise ValueError("context has unknown fields: %s"
                         % ", ".join(sorted(unknown)))
    if "target" in context:
        target = context["target"]
        if not isinstance(target, str) or not target.strip():
            raise ValueError("context.target must be a nonblank string")
    for field in ("response", "blocker", "context_request"):
        value = context.get(field)
        if value is not None and (
                not isinstance(value, str) or not value.strip()):
            raise ValueError("context.%s must be a nonblank string or null"
                             % field)
    for field in ("runtime_failure", "workspace_mutation"):
        _bool_or_none("context.%s" % field, context.get(field))


def _validate(role, status, event, original_coder_available, is_oldest_parked,
              task_shipped, has_pending_dependent_fix, wait_observation_count,
              terminal, runtime_state, unavailability, context):
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError("role must be a string in coder/reviewer/advisor")
    statuses = {
        "coder": CODER_STATUSES,
        "reviewer": REVIEWER_STATUSES,
        "advisor": ADVISOR_STATUSES,
    }[role]
    if not isinstance(status, str) or status not in statuses:
        raise ValueError("unknown %s status: %r" % (role, status))
    if event is not None and (not isinstance(event, str) or event not in EVENTS):
        raise ValueError("unknown event: %r" % (event,))
    _bool_or_none("original_coder_available", original_coder_available)
    _bool_or_none("is_oldest_parked", is_oldest_parked)
    _bool_or_none("task_shipped", task_shipped)
    _bool_or_none("has_pending_dependent_fix", has_pending_dependent_fix)
    if wait_observation_count is not None and (
            isinstance(wait_observation_count, bool)
            or not isinstance(wait_observation_count, int)):
        raise ValueError("wait_observation_count must be int or null")
    if terminal is not None and (
            not isinstance(terminal, str) or terminal not in ADVISOR_TERMINALS):
        raise ValueError("unknown terminal: %r" % (terminal,))
    if runtime_state is not None and (
            not isinstance(runtime_state, str)
            or runtime_state not in RUNTIME_STATES):
        raise ValueError("unknown runtime_state: %r" % (runtime_state,))
    if unavailability is not None and (
            not isinstance(unavailability, str)
            or unavailability not in UNAVAILABILITY):
        raise ValueError("unknown unavailability: %r" % (unavailability,))
    _validate_context(context)
    fix_first = role == "coder" and event == "fix_first"
    eviction = role == "coder" and event == "slot_pressure"
    wait_observation = (role == "advisor"
                        and event == "wait_observation_timeout")
    if terminal is not None and role != "advisor":
        raise ValueError("terminal is only valid for advisor")
    if unavailability is not None and not fix_first:
        raise ValueError("unavailability is only valid for coder fix_first")
    if original_coder_available is not None and not fix_first:
        raise ValueError(
            "original_coder_available is only valid for coder fix_first")
    if (any(value is not None for value in (
            is_oldest_parked, task_shipped, has_pending_dependent_fix))
            and not eviction):
        raise ValueError("eviction fields are only valid for coder slot_pressure")
    if wait_observation_count is not None and not wait_observation:
        raise ValueError(
            "wait_observation_count is only valid for advisor wait timeout")
    if unavailability is not None and original_coder_available is True:
        raise ValueError("contradictory: original_coder_available=True "
                         "with unavailability=%r" % (unavailability,))


def decide(role, status, event="", original_coder_available=None,
           is_oldest_parked=None, task_shipped=None,
           has_pending_dependent_fix=None, wait_observation_count=None,
           terminal=None, runtime_state=None, unavailability=None,
           context=None):
    """Return exactly one fixed lifecycle action for the given agent state."""
    _validate(role, status, event, original_coder_available, is_oldest_parked,
              task_shipped, has_pending_dependent_fix, wait_observation_count,
              terminal, runtime_state, unavailability, context)
    event = event or ""
    if runtime_state in ("unknown", "stale"):
        # Fail closed before role dispatch: no close/replace/redispatch.
        return _decide_fail_closed(terminal, event)
    if role == "coder":
        return _decide_coder(
            status, event, original_coder_available, is_oldest_parked,
            task_shipped, has_pending_dependent_fix, unavailability,
        )
    if role == "reviewer":
        return _decide_reviewer(status, event)
    return _decide_advisor(status, event, terminal)


def _decide_coder(status, event, original_coder_available, is_oldest_parked,
                  task_shipped, has_pending_dependent_fix, unavailability):
    if status == "PARKED_FOR_RETHINK":
        return "KEEP"
    if event == "fix_first":
        if original_coder_available is True:
            return "RESUME_SAME"
        if unavailability in UNAVAILABILITY:
            return "SPAWN_SUCCESSOR"
        return "KEEP"
    if event == "integration_fix_needed":
        if status == "PARKED_REUSABLE":
            return "RESUME_SAME"
    if event == "ship":
        if status in ("ACTIVE", "WAITING_VERIFICATION", "WAITING_AUDIT"):
            return "PARK"
        return "KEEP"
    if event == "slot_pressure":
        if status == "PARKED_REUSABLE":
            if (is_oldest_parked is True and task_shipped is True
                    and has_pending_dependent_fix is False):
                return "CLOSE_ALLOWED"
            return "KEEP"
        return "KEEP"
    return "KEEP"


def _decide_reviewer(status, event):
    if event == "diff_changed" or status == "STALE":
        return "SPAWN_FRESH_REVIEWER"
    if status == "DONE":
        return "CLOSE_ALLOWED"
    return "KEEP"


def _decide_advisor(status, event, terminal):
    if terminal in ("stop", "blocked", "cancelled", "runtime_failure"):
        return "STOP"
    if terminal in ("proceed", "change"):
        return "CLOSE_ALLOWED"
    if event == "wait_observation_timeout":
        return "WAIT"
    return "KEEP"


def _decide_fail_closed(terminal, event):
    """Unknown/stale runtime: keep the agent, never close/replace/redispatch."""
    if terminal is not None:
        return "STOP"
    if event == "wait_observation_timeout":
        return "WAIT"
    return "KEEP"


def transition(status, event):
    """Apply one section 44 coder lifecycle transition."""
    try:
        return CODER_TRANSITIONS[(status, event)]
    except KeyError:
        raise ValueError("no transition for (%r, %r)" % (status, event))


def main(argv=None):
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        data = json.loads(raw)
        action = decide(**data)
        _write({"action": action})
        return 0
    except json.JSONDecodeError as exc:
        _write({"action": None, "error": "invalid JSON: %s" % exc})
        return 2
    except (ValueError, TypeError) as exc:
        _write({"action": None, "error": str(exc)})
        return 2


def _write(obj):
    payload = json.dumps(obj, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    sys.exit(main())
