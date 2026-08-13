#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务级确定性收敛路由。"""

import hashlib
import json
import sys


ROUTES = frozenset({
    "CONTINUE_FIX", "ENTER_RETHINK", "RESUME_SAME", "SPAWN_SUCCESSOR",
    "SHIP", "STOP", "TASK_ESCALATION_REQUIRED",
})

MATERIAL_FIELDS = (
    "DELIVERABLE", "INTERFACES", "WRITE_SET", "DEPENDENCIES",
    "CONSTRAINTS", "ACCEPTANCE", "VERIFICATION", "DECISIONS",
)
MATERIAL_METADATA = frozenset({"task_id", "name"})
STRING_FIELDS = frozenset({
    "DELIVERABLE", "INTERFACES", "CONSTRAINTS", "ACCEPTANCE",
})
NONEMPTY_LIST_FIELDS = frozenset({"WRITE_SET", "VERIFICATION"})
OPTIONAL_LIST_FIELDS = frozenset({"DEPENDENCIES", "DECISIONS"})

VERDICTS = frozenset({"ship", "fix-first", "rethink"})
CONTINUITIES = frozenset({"preserve", "successor_recommended"})
DIAGNOSIS_STATUSES = frozenset({"proceed", "change", "stop"})
UNAVAILABILITY = frozenset({"unavailable", "runtime_gone", "unrecoverable"})
SENSITIVE_KEY_PARTS = ("raw", "conversation", "history", "secret")

FAILURE_CONTEXT_FIELDS = frozenset({
    "goal", "revision_rounds", "diagnosis", "changed_assumptions",
    "current_diff_fingerprint", "verification", "current_blockers",
    "repeated_signatures", "unresolved_decisions", "safe_workspace_state",
})
VERIFICATION_FIELDS = frozenset({
    "command", "exit_code", "failure_count", "freshness",
})
SAFE_WORKSPACE_FIELDS = frozenset({
    "dirty", "coder_status", "write_set_preserved",
})

EXIT_INVALID_INPUT = 2
EXIT_POLICY_BLOCKED = 3
EXIT_ESCALATION_REQUIRED = 4


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON serializable: %s" % exc)


def _nonblank(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a nonblank string" % name)


def _string_list(name, value, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError("%s must be %sa list" % (
            name, "a nonempty " if nonempty else ""))
    for item in value:
        _nonblank("%s item" % name, item)


def _exact_int(name, value, minimum, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % name)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError("%s out of range" % name)


def _exact_keys(name, value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("%s must contain exactly: %s" % (
            name, ", ".join(sorted(expected))))


def _reject_sensitive_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if (not isinstance(key, str)
                    or any(part in key.lower() for part in SENSITIVE_KEY_PARTS)):
                raise ValueError("failure_context contains forbidden key")
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested)


def _validate_material_contract(value):
    if not isinstance(value, dict):
        raise ValueError("contract must be an object")
    missing = set(MATERIAL_FIELDS) - set(value)
    unknown = set(value) - set(MATERIAL_FIELDS) - MATERIAL_METADATA
    if missing:
        raise ValueError("contract missing fields: %s" % ", ".join(sorted(missing)))
    if unknown:
        raise ValueError("contract has unknown fields: %s" % ", ".join(sorted(unknown)))
    for field in STRING_FIELDS:
        _nonblank(field, value[field])
    for field in NONEMPTY_LIST_FIELDS:
        _string_list(field, value[field], nonempty=True)
    for field in OPTIONAL_LIST_FIELDS:
        _string_list(field, value[field])
    for field in MATERIAL_METADATA & set(value):
        _nonblank(field, value[field])
    return value


def contract_fingerprint(contract):
    """仅对封闭 material schema 生成稳定指纹。"""
    value = _validate_material_contract(contract)
    material = {field: value[field] for field in MATERIAL_FIELDS}
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def _changed_fields(old_contract, new_contract):
    return [
        field for field in MATERIAL_FIELDS
        if _canonical(old_contract[field]) != _canonical(new_contract[field])
    ]


def _validate_failure_context(value, rev1_rounds, fix_round):
    _reject_sensitive_keys(value)
    _exact_keys("failure_context", value, FAILURE_CONTEXT_FIELDS)
    _nonblank("failure_context.goal", value["goal"])

    rounds = value["revision_rounds"]
    _exact_keys("failure_context.revision_rounds", rounds, {"1", "2"})
    if rounds != {"1": rev1_rounds, "2": fix_round}:
        raise ValueError(
            "failure_context.revision_rounds must match actual rounds")

    diagnosis = value["diagnosis"]
    _exact_keys("failure_context.diagnosis", diagnosis,
                {"status", "summary"})
    if diagnosis["status"] not in DIAGNOSIS_STATUSES:
        raise ValueError("unknown diagnosis status")
    _nonblank("failure_context.diagnosis.summary", diagnosis["summary"])

    _string_list("failure_context.changed_assumptions",
                 value["changed_assumptions"])
    _nonblank("failure_context.current_diff_fingerprint",
              value["current_diff_fingerprint"])

    verification = value["verification"]
    _exact_keys("failure_context.verification", verification,
                VERIFICATION_FIELDS)
    _nonblank("failure_context.verification.command", verification["command"])
    _exact_int("failure_context.verification.exit_code",
               verification["exit_code"], -2147483648, 2147483647)
    _exact_int("failure_context.verification.failure_count",
               verification["failure_count"], 0)
    _nonblank("failure_context.verification.freshness",
              verification["freshness"])
    if verification["freshness"] != "fresh":
        raise ValueError("failure_context.verification.freshness must be fresh")

    for field in ("current_blockers", "repeated_signatures",
                  "unresolved_decisions"):
        _string_list("failure_context.%s" % field, value[field])

    workspace = value["safe_workspace_state"]
    _exact_keys("failure_context.safe_workspace_state", workspace,
                SAFE_WORKSPACE_FIELDS)
    if not isinstance(workspace["dirty"], bool):
        raise ValueError("safe_workspace_state.dirty must be bool")
    if workspace["coder_status"] != "PARKED_FOR_RETHINK":
        raise ValueError("safe_workspace_state.coder_status must be parked")
    if not isinstance(workspace["write_set_preserved"], bool):
        raise ValueError("safe_workspace_state.write_set_preserved must be bool")
    return value


def _validate(task_id, contract_revision, fix_round, total_fix_rounds,
              review_verdict, original_coder_available,
              implementer_continuity, old_contract, new_contract,
              diagnosis_complete, responsibility_boundary_changed,
              coder_unavailability,
              rev1_rounds, prior_route, coder_status, failure_context):
    _nonblank("task_id", task_id)
    _exact_int("contract_revision", contract_revision, 1, 2)
    _exact_int("fix_round", fix_round, 0, 3)
    _exact_int("total_fix_rounds", total_fix_rounds, 0, 6)
    if review_verdict not in VERDICTS:
        raise ValueError("unknown review_verdict: %r" % review_verdict)
    if original_coder_available is not None and not isinstance(
            original_coder_available, bool):
        raise ValueError("original_coder_available must be bool or null")
    if implementer_continuity not in CONTINUITIES:
        raise ValueError("unknown implementer_continuity: %r"
                         % implementer_continuity)
    if not isinstance(diagnosis_complete, bool):
        raise ValueError("diagnosis_complete must be bool")
    if (responsibility_boundary_changed is not None
            and not isinstance(responsibility_boundary_changed, bool)):
        raise ValueError("responsibility_boundary_changed must be bool or null")
    if coder_unavailability is not None and (
            not isinstance(coder_unavailability, str)
            or coder_unavailability not in UNAVAILABILITY):
        raise ValueError("unknown coder_unavailability: %r"
                         % coder_unavailability)
    if rev1_rounds is not None:
        _exact_int("rev1_rounds", rev1_rounds, 0, 3)

    old_contract = _validate_material_contract(old_contract)
    new_contract = _validate_material_contract(new_contract)

    if contract_revision == 1:
        if total_fix_rounds != fix_round:
            raise ValueError("revision 1 counters are inconsistent")
        if diagnosis_complete:
            if rev1_rounds != fix_round:
                raise ValueError("diagnosis must record actual revision 1 rounds")
        elif rev1_rounds is not None:
            raise ValueError("revision 1 counters are inconsistent")
    elif rev1_rounds is None or total_fix_rounds != rev1_rounds + fix_round:
        raise ValueError("revision 2 counters are inconsistent")

    if diagnosis_complete:
        if review_verdict == "ship":
            raise ValueError("ship cannot carry completed diagnosis")
        if (contract_revision != 1
                or prior_route != "ENTER_RETHINK"
                or coder_status != "PARKED_FOR_RETHINK"):
            raise ValueError("diagnosis prior state is invalid")
        if review_verdict == "fix-first" and fix_round != 3:
            raise ValueError("fix-first can enter rethink only at round 3")
    elif prior_route is not None or coder_status is not None:
        raise ValueError("diagnosis prior state requires completed diagnosis")

    if responsibility_boundary_changed is not None and not diagnosis_complete:
        raise ValueError("responsibility boundary evidence requires diagnosis")

    if coder_unavailability is not None:
        if not diagnosis_complete:
            raise ValueError("coder_unavailability requires completed diagnosis")
        if original_coder_available is True:
            raise ValueError("contradictory: original_coder_available=True "
                             "with coder_unavailability=%r"
                             % (coder_unavailability,))

    escalation = (contract_revision == 2
                  and (review_verdict == "rethink"
                       or (review_verdict == "fix-first" and fix_round == 3)))
    if escalation:
        if failure_context is None:
            raise ValueError("failure_context is required for escalation")
        _validate_failure_context(failure_context, rev1_rounds, fix_round)
    elif failure_context is not None:
        raise ValueError("failure_context is only valid for escalation")
    return old_contract, new_contract


def _base_result(task_id, contract_revision, fix_round, total_fix_rounds,
                 rev1_rounds, old_fingerprint, new_fingerprint,
                 changed_fields):
    return {
        "route": None,
        "task_id": task_id,
        "contract_revision": contract_revision,
        "fix_round": fix_round,
        "total_fix_rounds": total_fix_rounds,
        "rev1_rounds": rev1_rounds,
        "old_contract_fingerprint": old_fingerprint,
        "new_contract_fingerprint": new_fingerprint,
        "changed_contract_fields": changed_fields,
    }


def _failure_capsule(result, old_fingerprint, new_fingerprint,
                     changed_fields, failure_context):
    return {
        "task": {
            "task_id": result["task_id"],
            "goal": failure_context["goal"],
        },
        "rev1": {"rounds": failure_context["revision_rounds"]["1"]},
        "diagnosis": failure_context["diagnosis"],
        "rev2": {
            "changed_assumptions": failure_context["changed_assumptions"],
            "contract": {
                "old_fingerprint": old_fingerprint,
                "new_fingerprint": new_fingerprint,
                "changed_fields": changed_fields,
            },
            "rounds": failure_context["revision_rounds"]["2"],
        },
        "current_diff_fingerprint":
            failure_context["current_diff_fingerprint"],
        "verification": failure_context["verification"],
        "current_blockers": failure_context["current_blockers"],
        "repeated_signatures": failure_context["repeated_signatures"],
        "unresolved_decisions": failure_context["unresolved_decisions"],
        "safe_workspace_state": failure_context["safe_workspace_state"],
    }


def route_task(task_id, contract_revision, fix_round, total_fix_rounds,
               review_verdict, original_coder_available,
               implementer_continuity, old_contract, new_contract,
               diagnosis_complete, responsibility_boundary_changed=None,
               coder_unavailability=None,
               rev1_rounds=None, prior_route=None, coder_status=None,
               failure_context=None):
    """返回 task route；顶层字段由显式签名形成 allowlist。"""
    old_contract, new_contract = _validate(
        task_id, contract_revision, fix_round, total_fix_rounds,
        review_verdict, original_coder_available, implementer_continuity,
        old_contract, new_contract, diagnosis_complete,
        responsibility_boundary_changed, coder_unavailability,
        rev1_rounds, prior_route,
        coder_status, failure_context,
    )
    old_fingerprint = contract_fingerprint(old_contract)
    new_fingerprint = contract_fingerprint(new_contract)
    changed_fields = _changed_fields(old_contract, new_contract)
    result = _base_result(
        task_id, contract_revision, fix_round, total_fix_rounds,
        rev1_rounds, old_fingerprint, new_fingerprint, changed_fields,
    )

    if review_verdict == "ship":
        if old_fingerprint == new_fingerprint:
            result["route"] = "SHIP"
        else:
            result["route"] = "STOP"
        return result

    if (contract_revision == 2
            and (review_verdict == "rethink" or fix_round == 3)):
        result["route"] = "TASK_ESCALATION_REQUIRED"
        result["failure_capsule"] = _failure_capsule(
            result, old_fingerprint, new_fingerprint, changed_fields,
            failure_context,
        )
        return result

    if diagnosis_complete:
        if old_fingerprint == new_fingerprint:
            result["route"] = "STOP"
            return result
        result.update({
            "contract_revision": 2,
            "fix_round": 0,
            "total_fix_rounds": total_fix_rounds,
            "rev1_rounds": rev1_rounds,
        })
        if (coder_unavailability is not None
                and original_coder_available is not True):
            result["route"] = "SPAWN_SUCCESSOR"
        elif (original_coder_available is True
                and implementer_continuity == "preserve"):
            result["route"] = "RESUME_SAME"
        elif (implementer_continuity == "successor_recommended"
              and responsibility_boundary_changed is True):
            result["route"] = "SPAWN_SUCCESSOR"
        else:
            result["route"] = "STOP"
        return result

    if review_verdict == "rethink" or fix_round == 3:
        result["route"] = "ENTER_RETHINK"
        result["coder_status"] = "PARKED_FOR_RETHINK"
        return result

    result["route"] = "CONTINUE_FIX"
    return result


def _write(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        result = route_task(**data)
        _write(result)
        if result["route"] == "STOP":
            return EXIT_POLICY_BLOCKED
        if result["route"] == "TASK_ESCALATION_REQUIRED":
            return EXIT_ESCALATION_REQUIRED
        return 0
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _write({"route": None, "error": str(exc)})
        return EXIT_INVALID_INPUT


if __name__ == "__main__":
    sys.exit(main())
