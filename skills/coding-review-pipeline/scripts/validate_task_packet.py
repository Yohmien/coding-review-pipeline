"""Task Packet Validator CLI (plan sections 33-39).

Validates a coder task packet before spawn: all required fields with
field-specific schemas, dependency integrity, decision registry references
and deterministic DECISION_CONFLICT checks against change facts. Missing
DECISION_BUDGET defaults to MECHANICAL. Any blocking issue returns the stable
BLOCKED structure with decision_required and evidence on stdout and a nonzero
exit code.

Unified failure policy: packet content problems (missing fields, invalid
paths, duplicates, decision references, conflicts) return BLOCKED/exit 3 with
evidence; decision registry and input file schema errors return
invalid_input/exit 2. No input ever produces internal_error/exit 1; packet
path elements are pre-checked (type/blank) before the shared normalizer runs.

fact_constraints semantics: key missing means no constraints; explicit null
is a schema error (invalid_input/exit 2); an empty list is allowed and means
no constraints were declared (same runtime result as missing).

Exit codes: 0 valid, 3 (policy_blocked) when spawn must be blocked, 2
(invalid_input) for malformed files or registry schema errors, 1
(internal_error) for unexpected failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import crp_common
from crp_common import (
    CrpError,
    EXIT_OK,
    emit_error,
    exit_code,
)
from task_graph import normalize_repo_path


REQUIRED_FIELDS = (
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
    "STOP_CONDITIONS",
    "RETURN_FORMAT",
)

STRING_FIELDS = (
    "RUN_ID",
    "TASK_ID",
    "DELIVERABLE",
    "OBJECTIVE",
    "INTERFACES",
    "CONSTRAINTS",
    "INDEPENDENT_ACCEPTANCE",
    "RETURN_FORMAT",
)

LIST_FIELDS = ("WRITE_SET", "READ_ONLY", "DEPENDENCIES")
LIST_OR_STRING_FIELDS = ("VERIFICATION", "STOP_CONDITIONS")

DECISION_BUDGETS = ("MECHANICAL", "LOCAL_LOW_RISK")
DEFAULT_DECISION_BUDGET = "MECHANICAL"
DECISION_OWNERS = ("user", "main", "advisor")
DECISION_STATUS_RESOLVED = "resolved"
REQUIRED_DECISION_FIELDS = ("id", "domain", "owner", "status", "value", "evidence", "affects")

RED_CLAIM_PATTERN = re.compile(r"(?<!\w)RED\s*[:：]", re.IGNORECASE)
INVALID_RED_PATTERNS = (
    re.compile(
        r"实现缺失|implementation\s+(?:is\s+)?missing|missing\s+implementation|"
        r"not\s+implemented|unimplemented|fails?\s+until\s+implemented|未实现|尚未实现|缺少实现",
        re.IGNORECASE,
    ),
    re.compile(r"生产(?:类|类型).{0,40}(?:不存在|缺失|未定义|找不到)", re.IGNORECASE),
    re.compile(
        r"production\s+(?:class|type).{0,40}(?:missing|does\s+not\s+exist|not\s+found)",
        re.IGNORECASE,
    ),
    re.compile(r"cannot\s+find\s+symbol|missing\s+symbol|symbol\s+not\s+found|找不到符号|缺失符号", re.IGNORECASE),
    re.compile(r"testCompile", re.IGNORECASE),
    re.compile(r"(?:compilation|compile)\s+(?:failed|failure|error)|编译.{0,20}(?:失败|错误)", re.IGNORECASE),
)
NEW_PRODUCTION_TYPE_PATTERN = re.compile(
    r"新增生产(?:类|类型)|new\s+production\s+(?:class|type)", re.IGNORECASE
)
COMPILABLE_SHELL_PATTERN = re.compile(
    r"最小可编译(?:的)?签名壳|minimal\s+compilable\s+signature\s+shell", re.IGNORECASE
)
POSITIVE_TESTS_RUN_PATTERN = re.compile(
    r"tests_run\s*(?:>\s*0|={1,2}\s*[1-9]\d*|:\s*[1-9]\d*)", re.IGNORECASE
)
TARGET_ASSERTION_FAILURE_PATTERN = re.compile(
    r"目标行为.{0,20}断言.{0,10}失败|target\s+behaviou?r.{0,20}assertion.{0,10}(?:failed|failure)",
    re.IGNORECASE,
)

def _load_json(path: str, label: str) -> object:
    target = Path(path)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrpError("invalid_input", f"{label} file not found", path=str(target)) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError(
            "invalid_input",
            f"{label} file is not valid JSON",
            path=str(target),
            error=str(error),
        ) from error


def _validate_decision_record(record: dict, decision_id: str) -> None:
    for field in REQUIRED_DECISION_FIELDS:
        if field not in record:
            raise CrpError(
                "invalid_input",
                "decision record is missing a required field",
                decision_id=decision_id,
                field=field,
            )
    for field in ("id", "domain", "owner", "status", "value"):
        if not isinstance(record[field], str):
            raise CrpError(
                "invalid_input",
                "decision field must be a string",
                decision_id=decision_id,
                field=field,
            )
    if not record["id"] or not record["domain"] or not record["status"]:
        raise CrpError(
            "invalid_input",
            "decision id, domain and status must be non-empty",
            decision_id=decision_id,
        )
    if not record["value"].strip():
        raise CrpError(
            "invalid_input",
            "decision value must be non-empty",
            decision_id=decision_id,
        )
    if record["owner"] not in DECISION_OWNERS:
        raise CrpError(
            "invalid_input",
            "decision owner must be one of user/main/advisor",
            decision_id=decision_id,
            owner=record["owner"],
        )
    if not isinstance(record["evidence"], list):
        raise CrpError(
            "invalid_input",
            "decision evidence must be a list",
            decision_id=decision_id,
        )
    if not isinstance(record["affects"], list) or not record["affects"]:
        raise CrpError(
            "invalid_input",
            "decision affects must be a non-empty list of task ids",
            decision_id=decision_id,
        )
    seen_affects: set[str] = set()
    for task_id in record["affects"]:
        if not isinstance(task_id, str) or not task_id:
            raise CrpError(
                "invalid_input",
                "decision affects must contain only non-empty task ids",
                decision_id=decision_id,
            )
        if task_id in seen_affects:
            raise CrpError(
                "invalid_input",
                "duplicate task id in decision affects",
                decision_id=decision_id,
                task_id=task_id,
            )
        seen_affects.add(task_id)
    if "fact_constraints" in record:
        constraints = record["fact_constraints"]
        if constraints is None:
            raise CrpError(
                "invalid_input",
                "fact_constraints must not be null; omit the key for no constraints",
                decision_id=decision_id,
            )
        if not isinstance(constraints, list):
            raise CrpError(
                "invalid_input",
                "decision fact_constraints must be a list",
                decision_id=decision_id,
            )
        for constraint in constraints:
            if not isinstance(constraint, dict):
                raise CrpError(
                    "invalid_input",
                    "each fact_constraint must be an object",
                    decision_id=decision_id,
                )
            path = constraint.get("path")
            allowed = constraint.get("allowed_values")
            if not isinstance(path, str) or not _is_valid_dotted_path(path) or not isinstance(allowed, list):
                raise CrpError(
                    "invalid_input",
                    "each fact_constraint needs a non-empty dotted path and an allowed_values list",
                    decision_id=decision_id,
                )


def _load_decisions(path: str | None) -> dict[str, dict] | None:
    if path is None:
        return None
    data = _load_json(path, "decisions")
    if isinstance(data, dict) and "decisions" in data:
        data = data["decisions"]
    if isinstance(data, dict):
        records: dict[str, dict] = {}
        for key, record in data.items():
            if not isinstance(record, dict):
                raise CrpError("invalid_input", "each decision must be a JSON object")
            if record.get("id") != key:
                raise CrpError(
                    "invalid_input",
                    "decision mapping key must equal its id",
                    key=key,
                    decision_id=record.get("id"),
                )
            _validate_decision_record(record, key)
            records[key] = record
        return records
    if not isinstance(data, list):
        raise CrpError(
            "invalid_input",
            "decisions file must be a list or an object with a 'decisions' mapping",
        )
    records = {}
    for record in data:
        if not isinstance(record, dict):
            raise CrpError("invalid_input", "each decision must be a JSON object")
        _validate_decision_record(record, str(record.get("id")))
        decision_id = record["id"]
        if decision_id in records:
            raise CrpError("invalid_input", "duplicate decision id", decision_id=decision_id)
        records[decision_id] = record
    return records


def _load_task_ids(path: str | None) -> set[str] | None:
    if path is None:
        return None
    data = _load_json(path, "tasks")
    if isinstance(data, dict) and "tasks" in data:
        data = data["tasks"]
    if not isinstance(data, list):
        raise CrpError("invalid_input", "tasks file must be a list or an object with a 'tasks' list")
    task_ids = set()
    seen_ids: set[str] = set()
    for task in data:
        if not isinstance(task, dict) or not isinstance(task.get("TASK_ID"), str) or not task["TASK_ID"]:
            raise CrpError("invalid_input", "each task must be an object with a non-empty TASK_ID")
        if task["TASK_ID"] in seen_ids:
            raise CrpError(
                "invalid_input",
                "duplicate TASK_ID in tasks file",
                task_id=task["TASK_ID"],
            )
        seen_ids.add(task["TASK_ID"])
        task_ids.add(task["TASK_ID"])
    return task_ids


def _load_change_facts(path: str | None) -> dict | None:
    if path is None:
        return None
    data = _load_json(path, "change facts")
    if not isinstance(data, dict):
        raise CrpError("invalid_input", "change facts file must be a JSON object")
    return data


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_list(value: object, require_nonempty: bool = False) -> bool:
    if not isinstance(value, list):
        return False
    if require_nonempty and not value:
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in value)


def _red_scan_text(entry: str) -> str:
    normalized = re.sub(r"\s+", " ", entry)
    marker = RED_CLAIM_PATTERN.search(normalized)
    if marker is None:
        return normalized
    quoted = re.compile(r'''("[^"\r\n]*"|'[^'\r\n]*'|`[^`\r\n]*`)''')
    return normalized[: marker.end()] + quoted.sub(" ", normalized[marker.end() :])


def _validate_red_evidence(verification: object, evidence: list[dict]) -> None:
    if isinstance(verification, str):
        entries = [verification]
    elif isinstance(verification, list):
        entries = [entry for entry in verification if isinstance(entry, str)]
    else:
        return

    for entry in entries:
        scan_text = _red_scan_text(entry)
        if not RED_CLAIM_PATTERN.search(scan_text):
            continue
        if any(pattern.search(scan_text) for pattern in INVALID_RED_PATTERNS):
            problem = (
                "RED must not use missing production code, symbols, testCompile, or compilation "
                "failures; establish a compilable test fixture first"
            )
        elif not POSITIVE_TESTS_RUN_PATTERN.search(scan_text) or not TARGET_ASSERTION_FAILURE_PATTERN.search(scan_text):
            problem = "RED must state tests_run > 0 and a target behavior assertion failure"
        elif NEW_PRODUCTION_TYPE_PATTERN.search(scan_text) and not COMPILABLE_SHELL_PATTERN.search(scan_text):
            problem = "RED for a new production type must first state a minimal compilable signature shell"
        else:
            continue
        evidence.append(
            {
                "kind": "invalid_red_evidence",
                "field": "VERIFICATION",
                "problem": problem,
            }
        )


def _validate_path_fields(packet: dict, evidence: list[dict]) -> None:
    write_keys: dict[str, str] = {}
    read_keys: dict[str, str] = {}
    for field, bucket in (("WRITE_SET", write_keys), ("READ_ONLY", read_keys)):
        values = packet.get(field)
        if not isinstance(values, list):
            continue
        for entry in values:
            if not isinstance(entry, str) or not entry.strip():
                evidence.append(
                    {
                        "kind": "invalid_path",
                        "field": field,
                        "path": entry,
                        "problem": "path element must be a non-empty string",
                    }
                )
                continue
            try:
                normalized, key = normalize_repo_path(entry)
            except CrpError as error:
                evidence.append(
                    {"kind": "invalid_path", "field": field, "path": entry, "problem": error.message}
                )
                continue
            if key in bucket:
                evidence.append({"kind": "duplicate_path", "field": field, "path": normalized})
                continue
            bucket[key] = normalized
    for key in sorted(set(write_keys) & set(read_keys)):
        evidence.append({"kind": "write_read_conflict", "path": write_keys[key]})


def _is_valid_dotted_path(path: str) -> bool:
    if not path or path.strip() != path:
        return False
    return all(segment and segment.strip() == segment for segment in path.split("."))


def _dotted_get(data: dict, path: str) -> tuple[bool, object]:
    """Resolve a dotted path; returns (found, value) with a missing sentinel."""

    current: object = data
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return False, None
    return True, current


def validate_packet(
    packet: dict,
    decisions: dict[str, dict] | None,
    task_ids: set[str] | None,
    change_facts: dict | None,
) -> dict:
    evidence: list[dict] = []
    decision_required: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in packet or packet[field] in (None, ""):
            evidence.append({"kind": "missing_field", "field": field})
    for field in STRING_FIELDS:
        if field in packet and not _is_nonempty_string(packet[field]):
            evidence.append(
                {"kind": "invalid_field", "field": field, "problem": "must be a non-empty string"}
            )
    for field in LIST_FIELDS:
        if field in packet and not _is_string_list(packet[field]):
            evidence.append(
                {
                    "kind": "invalid_field",
                    "field": field,
                    "problem": "must be a list of non-empty strings",
                }
            )
    for field in LIST_OR_STRING_FIELDS:
        if field in packet and not (
            _is_nonempty_string(packet[field]) or _is_string_list(packet[field], require_nonempty=True)
        ):
            evidence.append(
                {
                    "kind": "invalid_field",
                    "field": field,
                    "problem": "must be a non-empty string or a non-empty list of non-empty strings",
                }
            )
    _validate_red_evidence(packet.get("VERIFICATION"), evidence)
    if isinstance(packet.get("WRITE_SET"), list) and not packet["WRITE_SET"]:
        evidence.append(
            {"kind": "invalid_field", "field": "WRITE_SET", "problem": "must contain at least one repo-relative path"}
        )
    _validate_path_fields(packet, evidence)

    budget = packet.get("DECISION_BUDGET", DEFAULT_DECISION_BUDGET)
    if budget not in DECISION_BUDGETS:
        evidence.append({"kind": "invalid_decision_budget", "value": budget})

    task_id = packet.get("TASK_ID")
    dependencies = packet.get("DEPENDENCIES")
    if _is_string_list(dependencies):
        seen_dependencies: set[str] = set()
        for dependency in dependencies:
            stripped = dependency.strip()
            if stripped in seen_dependencies:
                evidence.append(
                    {
                        "kind": "duplicate_dependency",
                        "field": dependency,
                        "problem": "DEPENDENCIES must not contain duplicates",
                    }
                )
                continue
            seen_dependencies.add(stripped)
            if isinstance(task_id, str) and stripped == task_id.strip():
                evidence.append(
                    {
                        "kind": "invalid_dependency",
                        "field": dependency,
                        "problem": "task cannot depend on itself",
                    }
                )
            elif task_ids is not None and stripped not in task_ids:
                evidence.append({"kind": "unknown_task_dependency", "field": dependency})

    refs_raw = packet.get("DECISION_REFS", [])
    if not _is_string_list(refs_raw):
        if "DECISION_REFS" in packet:
            evidence.append(
                {
                    "kind": "invalid_field",
                    "field": "DECISION_REFS",
                    "problem": "must be a list of non-empty strings",
                }
            )
        refs: list[str] = []
    else:
        refs = [ref.strip() for ref in refs_raw]
    seen_refs: set[str] = set()
    for ref in refs:
        if ref in seen_refs:
            evidence.append(
                {
                    "kind": "duplicate_decision_ref",
                    "field": ref,
                    "problem": "DECISION_REFS must not contain duplicates",
                }
            )
            continue
        seen_refs.add(ref)
        record = decisions.get(ref) if decisions is not None else None
        if decisions is None or record is None:
            evidence.append({"kind": "unknown_decision_reference", "decision_id": ref})
            decision_required.append(ref)
            continue
        if record["status"] != DECISION_STATUS_RESOLVED:
            evidence.append({"kind": "unresolved_decision_reference", "decision_id": ref})
            decision_required.append(ref)
            continue
        if not isinstance(task_id, str) or task_id not in record["affects"]:
            evidence.append(
                {
                    "kind": "decision_affects_mismatch",
                    "decision_id": ref,
                    "task_id": task_id,
                    "affects": record["affects"],
                }
            )
            decision_required.append(ref)
            continue
        constraints = record.get("fact_constraints") or []
        if not constraints:
            continue
        if change_facts is None:
            evidence.append({"kind": "missing_change_facts", "decision_id": ref})
            decision_required.append(ref)
            continue
        for constraint in constraints:
            found, actual = _dotted_get(change_facts, constraint["path"])
            if not found:
                evidence.append(
                    {
                        "kind": "DECISION_CONFLICT",
                        "decision_id": ref,
                        "path": constraint["path"],
                        "expected": constraint["allowed_values"],
                        "actual": None,
                    }
                )
                decision_required.append(ref)
                continue
            if actual not in constraint["allowed_values"]:
                evidence.append(
                    {
                        "kind": "DECISION_CONFLICT",
                        "decision_id": ref,
                        "path": constraint["path"],
                        "expected": constraint["allowed_values"],
                        "actual": actual,
                    }
                )
                decision_required.append(ref)

    if evidence:
        return {
            "ok": False,
            "status": "BLOCKED",
            "decision_required": sorted(set(decision_required)),
            "evidence": evidence,
        }
    return {
        "ok": True,
        "status": "VALID",
        "run_id": packet["RUN_ID"],
        "task_id": packet["TASK_ID"],
        "decision_budget": budget,
        "decision_refs": sorted(set(refs)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_task_packet.py",
        description="Validate a coder task packet before spawn.",
    )
    parser.add_argument("--packet", required=True, help="task packet JSON file")
    parser.add_argument("--decisions", default=None, help="decision registry JSON file (optional)")
    parser.add_argument("--tasks", default=None, help="task graph JSON file (optional)")
    parser.add_argument("--change-facts", default=None, help="change facts JSON file from change_facts.py (optional)")
    args = parser.parse_args(argv)
    try:
        packet = _load_json(args.packet, "packet")
        if not isinstance(packet, dict):
            raise CrpError("invalid_input", "packet must be a JSON object")
        decisions = _load_decisions(args.decisions)
        task_ids = _load_task_ids(args.tasks)
        change_facts = _load_change_facts(args.change_facts)
        result = validate_packet(packet, decisions, task_ids, change_facts)
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return EXIT_OK if result["ok"] else exit_code("policy_blocked")


if __name__ == "__main__":
    sys.exit(main())
