"""Context Router CLI (plan sections 25-27).

Programmatically routes a stage's context needs: references, skills, tools,
and gate reasons. G1-G5 are orthogonal; only a genuine user decision routes to
grill-with-docs. Machine-readable JSON on stdout; success exits 0, failures
exit with a stable nonzero code and a structured error on stderr.

Contract (revision 1, fix round 2):
- Input JSON is validated against required/allowed schemas: unknown keys are
  rejected (an explicit ``schema_version`` key is allowed for evolution), and
  consumed nested fields are recursively type-checked; schema errors are
  invalid_input / exit 2.
- argparse usage errors are emitted as UTF-8 structured JSON with exit 2.
- ``parallel-safe`` requires at least two complete, valid, pairwise-disjoint
  explicit task write sets and no dependency; missing/empty/unknown resolves
  to serial, malformed nested JSON resolves to invalid_input.
- ``advisor_candidate`` is true only when at least one high-risk candidate has
  state ``candidate``; dependency/lockfile-only elevation is not an advisor
  candidate. Only confirmed facts (or task-level confirmed risk) decide HIGH.
- References are verified to exist under the skill root before being returned;
  a missing reference is a stable structured error, never a silently trimmed
  route.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import crp_common
from crp_common import (
    CrpError,
    EXIT_OK,
    emit_error,
    exit_code,
    reconfigure_stdio,
)


STAGES = {
    "explore",
    "plan",
    "decompose",
    "execute",
    "verify",
    "review",
    "complete",
    "resume",
}

HIGH_RISK_CANDIDATE_KEYS = (
    "transaction_candidate",
    "public_api_candidate",
    "security_candidate",
    "concurrency_candidate",
    "external_side_effect_candidate",
)

RECOVERY_KEYS = (
    "incomplete_ledger",
    "running_agent",
    "dirty_baseline",
    "interrupted_run",
    "context_recovery",
    "unknown_mutation",
)

# Facts that must never trigger G1 by themselves, regardless of how large the
# values are; they are reported back only as rejected non-triggers.
G1_NON_TRIGGER_KEYS = (
    "files_read",
    "tool_count",
    "skill_count",
    "test_count",
    "task_count",
    "needs_graph_evidence",
    "needs_text_search",
    "codegraph",
)

_FOUR_STATES = {"confirmed", "candidate", "not_detected", "unknown"}
_OVERLAP_STATES = {"unknown", "not_detected", "confirmed"}
_RISK_ENUM = {"NORMAL", "ELEVATED", "HIGH"}

CHANGE_FACTS_REQUIRED_KEYS = (
    "repo_root",
    "base",
    "head",
    "changed_files",
    "untracked_files",
    "changed_file_classes",
    "changed_languages",
    "modules",
    "tests_changed",
    "dependency_manifest_changed",
    "lockfile_changed",
    "migration_changed",
    "generated_file_candidates",
    "write_set_overlap",
    "diff_ranges",
    *HIGH_RISK_CANDIDATE_KEYS,
)
CHANGE_FACTS_ALLOWED_KEYS = CHANGE_FACTS_REQUIRED_KEYS + (
    "generated_at",
    "schema_version",
    "cache_fingerprint",
    "cache_hit",
)

_BOOL_TASK_KEYS = (
    "genuine_ambiguity",
    "user_decision_required",
    "needs_graph_evidence",
    "needs_call_flow",
    "needs_text_search",
    "testable",
    "completion_claim",
    "known_transaction_bug",
)
_INT_TASK_KEYS = (
    "files_read",
    "tool_count",
    "skill_count",
    "test_count",
    "task_count",
    "behavior_count",
)
_STR_TASK_KEYS = ("root_cause", "ambiguous_decision")
TASK_FACTS_ALLOWED_KEYS = (
    *_BOOL_TASK_KEYS,
    *_INT_TASK_KEYS,
    *_STR_TASK_KEYS,
    "risk",
    "write_sets",
    "dependencies",
    "schema_version",
)
LEDGER_ALLOWED_KEYS = (*RECOVERY_KEYS, "schema_version")

_STR_LIST_FACT_KEYS = (
    "changed_files",
    "untracked_files",
    "modules",
    "changed_languages",
    "generated_file_candidates",
)
_BOOL_FACT_KEYS = (
    "tests_changed",
    "dependency_manifest_changed",
    "lockfile_changed",
    "migration_changed",
)


def _invalid(label: str, key: str, expected: str) -> CrpError:
    return CrpError("invalid_input", f"{label}: {key} must be {expected}", key=key)


def _reject_unknown_keys(data: dict[str, object], label: str, allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise CrpError("invalid_input", f"{label}: unknown keys", unknown=unknown)


def _expect_schema_version(data: dict[str, object], label: str) -> None:
    value = data.get("schema_version")
    if value is not None and not isinstance(value, (str, int)):
        raise _invalid(label, "schema_version", "a string or integer")


def _validate_range_item(label: str, item: object) -> None:
    if not isinstance(item, dict):
        raise _invalid(label, "diff_ranges[]", "an object")
    for key, value in item.items():
        if key in ("start", "end"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise _invalid(label, f"diff_ranges[].{key}", "an integer")
        elif key == "full_file":
            if not isinstance(value, bool):
                raise _invalid(label, "diff_ranges[].full_file", "a boolean")
        elif key == "state":
            if not isinstance(value, str):
                raise _invalid(label, "diff_ranges[].state", "a string")
        else:
            raise CrpError(
                "invalid_input",
                f"{label}: unknown key in diff_ranges[]",
                key=key,
            )


def _validate_candidate(label: str, key: str, candidate: object) -> None:
    if not isinstance(candidate, dict) or candidate.get("state") not in _FOUR_STATES:
        raise _invalid(
            label,
            key,
            "an object with state in confirmed/candidate/not_detected/unknown",
        )
    unknown = sorted(set(candidate) - {"state", "evidence"})
    if unknown:
        raise CrpError("invalid_input", f"{label}: unknown keys in {key}", unknown=unknown)
    evidence = candidate.get("evidence", [])
    if not isinstance(evidence, list):
        raise _invalid(label, f"{key}.evidence", "a list")
    for entry in evidence:
        if not isinstance(entry, dict):
            raise _invalid(label, f"{key}.evidence[]", "an object")
        for field, value in entry.items():
            if field == "file":
                if not isinstance(value, str):
                    raise _invalid(label, f"{key}.evidence[].file", "a string")
            elif field == "line":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise _invalid(label, f"{key}.evidence[].line", "an integer")
            elif field == "match":
                if not isinstance(value, str):
                    raise _invalid(label, f"{key}.evidence[].match", "a string")
            else:
                raise CrpError(
                    "invalid_input",
                    f"{label}: unknown key in {key}.evidence[]",
                    key=field,
                )


def _validate_overlap_semantics(label: str, overlap: dict[str, object]) -> None:
    """Closed, state-specific schema for write_set_overlap."""

    state = overlap.get("state")
    if state not in _OVERLAP_STATES:
        raise _invalid(
            label,
            "write_set_overlap.state",
            "one of unknown/not_detected/confirmed",
        )
    task_count = overlap.get("task_count")
    if not isinstance(task_count, int) or isinstance(task_count, bool) or task_count < 0:
        raise _invalid(label, "write_set_overlap.task_count", "a non-negative integer")
    pairs = overlap.get("pairs")
    if not isinstance(pairs, list):
        raise _invalid(label, "write_set_overlap.pairs", "a list")
    if state == "unknown":
        if task_count != 0 or pairs:
            raise CrpError(
                "invalid_input",
                f"{label}: unknown write_set_overlap must have task_count=0 and empty pairs",
            )
    elif state == "not_detected":
        if task_count < 1 or pairs:
            raise CrpError(
                "invalid_input",
                f"{label}: not_detected write_set_overlap must have task_count>=1 and empty pairs",
            )
    else:  # confirmed
        if task_count < 2 or not pairs:
            raise CrpError(
                "invalid_input",
                f"{label}: confirmed write_set_overlap must have task_count>=2 and non-empty pairs",
            )
    seen_pairs: set[tuple[str, str]] = set()
    previous: tuple[str, str] | None = None
    for pair in pairs:
        if not isinstance(pair, dict):
            raise _invalid(label, "write_set_overlap.pairs[]", "an object")
        pair_unknown = sorted(set(pair) - {"task_a", "task_b", "intersection"})
        if pair_unknown:
            raise CrpError(
                "invalid_input",
                f"{label}: unknown keys in write_set_overlap.pairs[]",
                unknown=pair_unknown,
            )
        pair_missing = sorted({"task_a", "task_b", "intersection"} - set(pair))
        if pair_missing:
            raise CrpError(
                "invalid_input",
                f"{label}: missing keys in write_set_overlap.pairs[]",
                missing=pair_missing,
            )
        task_a = pair.get("task_a")
        task_b = pair.get("task_b")
        if not isinstance(task_a, str) or not task_a.strip():
            raise _invalid(label, "write_set_overlap.pairs[].task_a", "a non-blank string")
        if not isinstance(task_b, str) or not task_b.strip():
            raise _invalid(label, "write_set_overlap.pairs[].task_b", "a non-blank string")
        if task_a == task_b:
            raise CrpError(
                "invalid_input",
                f"{label}: task ids in a pair must differ",
                task_a=task_a,
            )
        if task_a > task_b:
            raise CrpError(
                "invalid_input",
                f"{label}: pair task ids must be canonical (task_a < task_b)",
                task_a=task_a,
                task_b=task_b,
            )
        key = (task_a, task_b)
        if key in seen_pairs:
            raise CrpError("invalid_input", f"{label}: duplicate pair", pair=key)
        if previous is not None and key <= previous:
            raise CrpError("invalid_input", f"{label}: pairs must be sorted canonically")
        seen_pairs.add(key)
        previous = key
        intersection = pair.get("intersection")
        if not isinstance(intersection, list) or not intersection:
            raise _invalid(
                label,
                "write_set_overlap.pairs[].intersection",
                "a non-empty list of non-blank strings",
            )
        if not all(isinstance(item, str) and item.strip() for item in intersection):
            raise _invalid(
                label,
                "write_set_overlap.pairs[].intersection",
                "a non-empty list of non-blank strings",
            )
        if len(set(intersection)) != len(intersection):
            raise CrpError(
                "invalid_input",
                f"{label}: intersection must not contain duplicates",
            )
    unique_ids = {
        pair_id
        for pair in pairs
        for pair_id in (pair.get("task_a"), pair.get("task_b"))
    }
    if len(unique_ids) > task_count:
        raise CrpError(
            "invalid_input",
            f"{label}: unique task ids exceed task_count",
            unique=len(unique_ids),
            task_count=task_count,
        )


def _validate_change_facts(facts: dict[str, object]) -> None:
    _reject_unknown_keys(facts, "change facts", CHANGE_FACTS_ALLOWED_KEYS)
    _expect_schema_version(facts, "change facts")
    missing = sorted(set(CHANGE_FACTS_REQUIRED_KEYS) - set(facts))
    if missing:
        raise CrpError("invalid_input", "change facts: missing required keys", missing=missing)
    for key in _STR_LIST_FACT_KEYS:
        value = facts.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _invalid("change facts", key, "a list of strings")
    for key in _BOOL_FACT_KEYS:
        value = facts.get(key)
        if not isinstance(value, bool):
            raise _invalid("change facts", key, "a boolean")
    for key in ("repo_root", "base", "head"):
        if not isinstance(facts.get(key), str):
            raise _invalid("change facts", key, "a string")
    changed_file_classes = facts.get("changed_file_classes")
    if not isinstance(changed_file_classes, dict) or not all(
        isinstance(key, str) and isinstance(values, list)
        and all(isinstance(item, str) for item in values)
        for key, values in changed_file_classes.items()
    ):
        raise _invalid("change facts", "changed_file_classes", "a map of string lists")
    diff_ranges = facts.get("diff_ranges")
    if not isinstance(diff_ranges, dict):
        raise _invalid("change facts", "diff_ranges", "an object")
    for path, ranges in diff_ranges.items():
        if not isinstance(path, str) or not isinstance(ranges, list):
            raise _invalid("change facts", "diff_ranges", "a map of path -> list")
        for item in ranges:
            _validate_range_item("change facts", item)
    overlap = facts.get("write_set_overlap")
    if not isinstance(overlap, dict):
        raise _invalid("change facts", "write_set_overlap", "an object")
    overlap_unknown = sorted(set(overlap) - {"state", "task_count", "pairs"})
    if overlap_unknown:
        raise CrpError(
            "invalid_input",
            "change facts: unknown keys in write_set_overlap",
            unknown=overlap_unknown,
        )
    overlap_missing = sorted({"state", "task_count", "pairs"} - set(overlap))
    if overlap_missing:
        raise CrpError(
            "invalid_input",
            "change facts: missing keys in write_set_overlap",
            missing=overlap_missing,
        )
    _validate_overlap_semantics("change facts", overlap)
    for key in HIGH_RISK_CANDIDATE_KEYS:
        _validate_candidate("change facts", key, facts.get(key))


def _validate_task_facts(task: dict[str, object]) -> None:
    _reject_unknown_keys(task, "task facts", TASK_FACTS_ALLOWED_KEYS)
    _expect_schema_version(task, "task facts")
    for key in _BOOL_TASK_KEYS:
        value = task.get(key)
        if value is not None and not isinstance(value, bool):
            raise _invalid("task facts", key, "a boolean")
    for key in _INT_TASK_KEYS:
        value = task.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise _invalid("task facts", key, "an integer")
    for key in _STR_TASK_KEYS:
        value = task.get(key)
        if value is not None and not isinstance(value, str):
            raise _invalid("task facts", key, "a string")
    risk = task.get("risk")
    if risk is not None and risk not in _RISK_ENUM:
        raise _invalid("task facts", "risk", "one of NORMAL/ELEVATED/HIGH")
    write_sets = task.get("write_sets")
    if write_sets is not None:
        if not isinstance(write_sets, list):
            raise _invalid("task facts", "write_sets", "a list of non-empty string lists")
        for index, write_set in enumerate(write_sets):
            if (
                not isinstance(write_set, list)
                or not write_set
                or not all(isinstance(item, str) and item.strip() for item in write_set)
            ):
                raise _invalid(
                    "task facts",
                    f"write_sets[{index}]",
                    "a non-empty list of non-empty path strings",
                )
    dependencies = task.get("dependencies")
    if dependencies is not None and not (
        isinstance(dependencies, list) and all(isinstance(item, str) for item in dependencies)
    ):
        raise _invalid("task facts", "dependencies", "a list of strings")


def _validate_ledger_state(ledger: dict[str, object]) -> None:
    _reject_unknown_keys(ledger, "ledger state", LEDGER_ALLOWED_KEYS)
    _expect_schema_version(ledger, "ledger state")
    for key in RECOVERY_KEYS:
        value = ledger.get(key)
        if value is not None and not isinstance(value, bool):
            raise _invalid("ledger state", key, "a boolean")


def _load_json(path: str | None, label: str) -> dict[str, object]:
    if path is None:
        return {}
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrpError("invalid_input", f"{label} file not found", path=str(target)) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError(
            "invalid_input",
            f"{label} file is not valid JSON",
            path=str(target),
            error=str(error),
        ) from error
    if not isinstance(data, dict):
        raise CrpError(
            "invalid_input",
            f"{label} file must contain a JSON object",
            path=str(target),
        )
    return data


def _int_or(data: dict[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _g1_user_decision(task_facts: dict[str, object]) -> str:
    if task_facts.get("genuine_ambiguity") is True or task_facts.get("user_decision_required") is True:
        return "REQUIRES_USER_DECISION"
    return "NONE"


def _g1_non_triggers(
    task_facts: dict[str, object],
    change_facts: dict[str, object],
) -> list[str]:
    triggers: list[str] = []
    for key in G1_NON_TRIGGER_KEYS:
        value = task_facts.get(key)
        if value is True or value == "true":
            triggers.append(key)
        elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
            triggers.append(key)
    if len(change_facts.get("changed_files", [])) > 1:
        triggers.append("multi_file")
    if len(change_facts.get("modules", [])) > 1:
        triggers.append("multi_module")
    return sorted(set(triggers))


def _g2_risk(
    change_facts: dict[str, object],
    task_facts: dict[str, object],
) -> str:
    explicit = task_facts.get("risk")
    if explicit in ("HIGH", "ELEVATED", "NORMAL"):
        return explicit
    if change_facts.get("migration_changed") is True:
        return "HIGH"
    for key in HIGH_RISK_CANDIDATE_KEYS:
        if change_facts.get(key, {}).get("state") == "confirmed":  # type: ignore[union-attr]
            return "HIGH"
    for key in HIGH_RISK_CANDIDATE_KEYS:
        if change_facts.get(key, {}).get("state") == "candidate":  # type: ignore[union-attr]
            return "ELEVATED"
    if (
        change_facts.get("dependency_manifest_changed") is True
        or change_facts.get("lockfile_changed") is True
    ):
        return "ELEVATED"
    return "NORMAL"


def _advisor_candidate(change_facts: dict[str, object]) -> bool:
    return any(
        change_facts.get(key, {}).get("state") == "candidate"  # type: ignore[union-attr]
        for key in HIGH_RISK_CANDIDATE_KEYS
    )


def _g3_decomposition(
    change_facts: dict[str, object],
    task_facts: dict[str, object],
) -> str:
    if _int_or(task_facts, "task_count", 0) > 1:
        return "multiple"
    behavior_count = _int_or(task_facts, "behavior_count", 1)
    file_count = len(change_facts.get("changed_files", [])) + len(
        change_facts.get("untracked_files", [])
    )
    module_count = len(change_facts.get("modules", []))
    if behavior_count <= 1 and file_count <= 1 and module_count <= 1:
        return "single"
    return "multiple"


def _g4_execution(
    change_facts: dict[str, object],
    task_facts: dict[str, object],
) -> str:
    overlap = change_facts.get("write_set_overlap")
    if isinstance(overlap, dict) and overlap.get("state") == "confirmed":
        return "serial"
    write_sets = task_facts.get("write_sets")
    if write_sets is None or not write_sets:
        return "serial"
    if len(write_sets) == 1:
        return "single"
    sets = [frozenset(write_set) for write_set in write_sets]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if sets[i] & sets[j]:
                return "serial"
    if task_facts.get("dependencies"):
        return "serial"
    return "parallel-safe"


def _g5_recovery(ledger_state: dict[str, object]) -> str:
    if any(ledger_state.get(key) is True for key in RECOVERY_KEYS):
        return "required"
    return "none"


def _route_references(
    stage: str,
    task_facts: dict[str, object],
    g2_risk: str,
    g5_recovery: str,
) -> list[str]:
    references = ["references/routing-gates.md"]
    if g5_recovery == "required":
        references.append("references/recovery-and-failures.md")
    if stage in {"decompose", "execute", "review"} or g2_risk == "HIGH":
        references.append("references/task-contracts.md")
    if stage in {"verify", "complete"} or task_facts.get("completion_claim") is True:
        references.append("references/verification-routing.md")
    return sorted(set(references))


def _route_skills(
    stage: str,
    task_facts: dict[str, object],
    g1_user_decision: str,
) -> list[str]:
    skills: list[str] = []
    if stage in {"execute", "review"}:
        skills.append("ponytail")
    if (
        task_facts.get("needs_graph_evidence") is True
        or task_facts.get("needs_call_flow") is True
        or task_facts.get("needs_text_search") is True
    ):
        skills.append("search-gates")
    if task_facts.get("root_cause") in (None, "not_established") and stage in {
        "explore",
        "execute",
    }:
        skills.append("systematic-debugging")
    if task_facts.get("testable") is True and stage in {"decompose", "execute"}:
        skills.append("test-driven-development")
    if g1_user_decision == "REQUIRES_USER_DECISION":
        skills.append("grill-with-docs")
    if task_facts.get("completion_claim") is True or stage == "complete":
        skills.append("verification-before-completion")
    return sorted(set(skills))


def _route_tools(
    stage: str,
    task_facts: dict[str, object],
    g1_user_decision: str,
) -> list[str]:
    tools: list[str] = []
    if g1_user_decision == "REQUIRES_USER_DECISION":
        tools.append("request_user_input")
    if task_facts.get("needs_graph_evidence") is True:
        tools.append("codegraph explore")
    if task_facts.get("needs_text_search") is True:
        tools.append("rg")
    return sorted(set(tools))


def route_context(
    stage: str,
    change_facts: dict[str, object],
    task_facts: dict[str, object],
    ledger_state: dict[str, object],
) -> dict[str, object]:
    g1_user_decision = _g1_user_decision(task_facts)
    g2_risk = _g2_risk(change_facts, task_facts)
    g3_decomposition = _g3_decomposition(change_facts, task_facts)
    g4_execution = _g4_execution(change_facts, task_facts)
    g5_recovery = _g5_recovery(ledger_state)
    return {
        "references": _route_references(stage, task_facts, g2_risk, g5_recovery),
        "skills": _route_skills(stage, task_facts, g1_user_decision),
        "tools": _route_tools(stage, task_facts, g1_user_decision),
        "reasons": {
            "stage": stage,
            "g1_user_decision": g1_user_decision,
            "g1_non_triggers": _g1_non_triggers(task_facts, change_facts),
            "g2_risk": g2_risk,
            "g2_advisor": "required" if g2_risk == "HIGH" else "not_required",
            "advisor_candidate": _advisor_candidate(change_facts),
            "g3_task_count": g3_decomposition,
            "g4_execution": g4_execution,
            "g5_recovery": g5_recovery,
            "grill": "required" if g1_user_decision == "REQUIRES_USER_DECISION" else "not_required",
        },
    }


def _check_references(references: list[str], skill_root: Path) -> None:
    missing = [reference for reference in references if not (skill_root / reference).is_file()]
    if missing:
        raise CrpError(
            "missing_reference",
            "referenced file not found under skill root",
            skill_root=str(skill_root),
            missing=sorted(missing),
        )


class _CrpArgumentParser(argparse.ArgumentParser):
    """argparse subclass that emits UTF-8 structured JSON on usage errors."""

    def error(self, message: str) -> None:
        emit_error(CrpError("invalid_input", "invalid arguments", detail=message))
        raise SystemExit(exit_code("invalid_input"))


def main(argv: list[str] | None = None) -> int:
    reconfigure_stdio()
    parser = _CrpArgumentParser(
        prog="route_context.py",
        description="Route a stage's references, skills, tools, and gate reasons.",
    )
    parser.add_argument("--stage", required=True, help="one of: " + ", ".join(sorted(STAGES)))
    parser.add_argument("--facts", default=None, help="change facts JSON file from change_facts.py")
    parser.add_argument("--task-facts", default=None, help="task facts JSON file")
    parser.add_argument("--ledger", default=None, help="ledger state JSON file")
    parser.add_argument(
        "--skill-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="skill root used to verify reference files exist",
    )
    args = parser.parse_args(argv)
    try:
        if args.stage not in STAGES:
            raise CrpError(
                "invalid_input",
                "unknown stage",
                stage=args.stage,
                allowed=sorted(STAGES),
            )
        change_facts = _load_json(args.facts, "change facts")
        task_facts = _load_json(args.task_facts, "task facts")
        ledger_state = _load_json(args.ledger, "ledger state")
        if args.facts is not None:
            _validate_change_facts(change_facts)
        if args.task_facts is not None:
            _validate_task_facts(task_facts)
        if args.ledger is not None:
            _validate_ledger_state(ledger_state)
        result = route_context(args.stage, change_facts, task_facts, ledger_state)
        _check_references(result["references"], Path(args.skill_root))
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
