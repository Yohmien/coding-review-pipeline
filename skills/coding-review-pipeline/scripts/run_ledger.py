#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 9-10 Persistent Run Ledger + Verification Router.

Deterministic JSON persistence for a coding-review-pipeline run and a
programmatic verification-tier router. The ledger is stored in the git
metadata area (``git rev-parse --git-path``) or, for NON_GIT workspaces, under
``$CODEX_HOME/state/coding-review-pipeline/<workspace-id>/`` (never TEMP).
Writes are atomic (temp + flush + os.replace via ``crp_common``) so a failed
write always leaves the previous ledger valid.

Machine-readable UTF-8 JSON on stdout; structured errors on stderr. Exit
codes follow ``crp_common``: 0 ok / 2 invalid_input / 3 policy_blocked /
1 internal_error. This module reuses ``crp_common`` plumbing and does not
reimplement atomic write, hashing, or git wrappers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


SCHEMA_VERSION = 1

LEDGER_KEYS = (
    "schema_version",
    "run_id",
    "repo_root",
    "plan",
    "baseline",
    "models",
    "decisions",
    "tasks",
    "agents",
    "integration",
    "events",
)

PLAN_FINGERPRINT_FIELDS = (
    "objective",
    "tasks",
    "dependencies",
    "interfaces",
    "constraints",
    "acceptance",
    "decisions",
)

DIFF_FINGERPRINT_FIELDS = ("changed_files", "untracked_files", "diff_ranges")

_HIGH_RISK_KEYS = (
    "transaction_candidate",
    "security_candidate",
    "concurrency_candidate",
    "external_side_effect_candidate",
)
_RISK_STATES = frozenset({"candidate", "confirmed"})
_AGENT_RUNNING_STATES = frozenset(
    {"ACTIVE", "RUNNING", "WAITING_VERIFICATION", "WAITING_AUDIT", "FIX_REQUIRED"}
)
_MERGE_DICT_KEYS = ("tasks", "agents", "decisions", "integration")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")

_MAX_RUN_ID_LENGTH = 64

# Windows reserved device names: CON/PRN/AUX/NUL and COM1-9/LPT1-9, including
# with any extension suffix; matching is case-insensitive on the name part
# before the first dot.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def _validate_run_id(run_id: object) -> None:
    if not isinstance(run_id, str):
        raise CrpError("invalid_input", "run_id must be a non-empty string")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise CrpError("invalid_input", "run_id contains invalid characters", run_id=run_id)
    if run_id in (".", ".."):
        raise CrpError("invalid_input", "run_id must not be a dot or dot-dot segment", run_id=run_id)
    if len(run_id) > _MAX_RUN_ID_LENGTH:
        raise CrpError(
            "invalid_input",
            "run_id exceeds 64 characters",
            run_id=run_id,
        )
    if run_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise CrpError(
            "invalid_input",
            "run_id is a Windows reserved device name",
            run_id=run_id,
        )


def _git_toplevel(start: Path) -> Path | None:
    """Return the repository top-level, or None when not inside a git repo."""

    try:
        proc = crp_common.run_git(["rev-parse", "--show-toplevel"], cwd=str(start))
    except CrpError:
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def _codex_home(codex_home: str | Path | None) -> Path:
    if codex_home is not None:
        return Path(codex_home).expanduser()
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def workspace_id(path: str | Path) -> str:
    """Stable NON_GIT workspace identifier (deterministic across sessions)."""

    return crp_common.sha256_text(str(Path(path).resolve()))


def runs_dir(start: str | Path | None = None, codex_home: str | Path | None = None) -> Path:
    """Directory that contains one ``<run-id>/ledger.json`` per run."""

    start_path = Path(start).resolve() if start is not None else Path.cwd().resolve()
    top = _git_toplevel(start_path)
    if top is not None:
        proc = crp_common.run_git(
            ["rev-parse", "--git-path", "coding-review-pipeline/runs"],
            cwd=str(top),
        )
        if proc.returncode != 0:
            raise CrpError(
                "internal_error",
                "git rev-parse --git-path failed",
                git_error=proc.stderr.strip(),
            )
        return (top / proc.stdout.strip()).resolve()
    home = _codex_home(codex_home)
    return home / "state" / "coding-review-pipeline" / workspace_id(start_path) / "runs"


def ledger_path(
    run_id: str,
    start: str | Path | None = None,
    codex_home: str | Path | None = None,
) -> Path:
    _validate_run_id(run_id)
    return runs_dir(start=start, codex_home=codex_home) / run_id / "ledger.json"


def plan_fingerprint(plan: dict) -> str:
    """Stable hash over objective/tasks/dependencies/interfaces/constraints/acceptance/decisions."""

    if not isinstance(plan, dict):
        raise CrpError("invalid_input", "plan must be an object")
    material = {field: plan.get(field) for field in PLAN_FINGERPRINT_FIELDS}
    return crp_common.hash_json(material)


_PLAN_TASK_ID_KEYS = ("TASK_ID", "task_id", "id")


def _plan_task_id(item: dict) -> str | None:
    """Resolve one plan task object's ID via TASK_ID -> task_id -> id."""

    for key in _PLAN_TASK_ID_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def validate_plan_tasks(plan: object, require_tasks: bool) -> set[str] | None:
    """Single-owner authoritative ``plan.tasks`` shape validation.

    Returns the declared task ID set, or None when ``require_tasks`` is False
    and the plan carries no tasks key (early-ledger plans may lack tasks).
    Raises CrpError(invalid_input) for any shape violation: a non-dict plan, a
    missing/None/empty/scalar tasks value, a list entry that is neither a
    non-empty string nor an object resolving to a non-empty string task ID,
    or a mapping with a non-string key. Mapping values are not inspected.
    """

    if not isinstance(plan, dict):
        raise CrpError("invalid_input", "plan must be an object")
    if "tasks" not in plan:
        if require_tasks:
            raise CrpError("invalid_input", "plan.tasks is required")
        return None
    tasks = plan["tasks"]
    if tasks is None:
        raise CrpError("invalid_input", "plan.tasks must not be null")
    if isinstance(tasks, list):
        if not tasks:
            raise CrpError("invalid_input", "plan.tasks must not be an empty list")
        ids: set[str] = set()
        for item in tasks:
            if isinstance(item, str):
                if not item:
                    raise CrpError("invalid_input", "plan.tasks contains an empty task ID")
                ids.add(item)
            elif isinstance(item, dict):
                task_id = _plan_task_id(item)
                if task_id is None:
                    raise CrpError(
                        "invalid_input",
                        "plan.tasks entry has no non-empty string task ID",
                    )
                ids.add(task_id)
            else:
                raise CrpError(
                    "invalid_input",
                    "plan.tasks entry must be a string or an object",
                )
        return ids
    if isinstance(tasks, dict):
        if not tasks:
            raise CrpError("invalid_input", "plan.tasks must not be an empty object")
        for key in tasks:
            if not isinstance(key, str) or not key:
                raise CrpError("invalid_input", "plan.tasks keys must be non-empty strings")
        return set(tasks)
    raise CrpError("invalid_input", "plan.tasks must be a list or an object")


def diff_fingerprint(change_facts: dict) -> str:
    """Stable fingerprint of the change set (changed/untracked files + ranges)."""

    if not isinstance(change_facts, dict):
        raise CrpError("invalid_input", "change facts must be an object")
    material = {field: change_facts.get(field) for field in DIFF_FINGERPRINT_FIELDS}
    return crp_common.hash_json(material)


def new_ledger(
    run_id: str,
    repo_root: str,
    plan: dict | None = None,
    baseline: dict | None = None,
    models: dict | None = None,
) -> dict:
    """Construct a fresh schema_version=1 ledger document."""

    _validate_run_id(run_id)
    if not isinstance(repo_root, str) or not repo_root:
        raise CrpError("invalid_input", "repo_root must be a non-empty string")
    plan = plan if isinstance(plan, dict) else {}
    baseline = dict(baseline) if isinstance(baseline, dict) else {}
    models = dict(models) if isinstance(models, dict) else {}
    baseline.setdefault("created_at", crp_common.utc_timestamp())
    baseline["plan_fingerprint"] = plan_fingerprint(plan)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "repo_root": repo_root,
        "plan": plan,
        "baseline": baseline,
        "models": models,
        "decisions": {},
        "tasks": {},
        "agents": {},
        "integration": {},
        "events": [],
    }


def _validate_ledger(ledger: dict, expected_run_id: str | None = None) -> None:
    """Single-owner full nested shape validation for ledger documents.

    Hard container shapes raise CrpError(invalid_input); baseline violations
    carry ``section="baseline"`` so resume can map them to plan
    reconfirmation. Soft shapes (non-dict values inside tasks/agents/decisions)
    are classified by ``soft_shape_violations`` and are deliberately NOT
    rejected here; integration values are gated at write time by
    ``_validate_integration_write`` instead.
    """

    if not isinstance(ledger, dict):
        raise CrpError("invalid_input", "ledger must be an object")
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise CrpError(
            "invalid_input",
            "unsupported ledger schema_version",
            schema_version=ledger.get("schema_version"),
        )
    baseline = ledger.get("baseline")
    if not isinstance(baseline, dict):
        raise CrpError(
            "invalid_input",
            "ledger.baseline must be an object",
            section="baseline",
        )
    missing = [key for key in LEDGER_KEYS if key not in ledger]
    if missing:
        raise CrpError("invalid_input", "ledger missing required keys", missing=missing)
    if expected_run_id is not None and ledger.get("run_id") != expected_run_id:
        raise CrpError(
            "invalid_input",
            "ledger run_id does not match",
            expected=expected_run_id,
            actual=ledger.get("run_id"),
        )
    repo_root = ledger.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        raise CrpError("invalid_input", "ledger.repo_root must be a non-empty string")
    plan_fingerprint_value = baseline.get("plan_fingerprint")
    if not isinstance(plan_fingerprint_value, str) or not plan_fingerprint_value:
        raise CrpError(
            "invalid_input",
            "ledger.baseline.plan_fingerprint must be a non-empty string",
            section="baseline",
        )
    if not isinstance(baseline.get("created_at"), str):
        raise CrpError(
            "invalid_input",
            "ledger.baseline.created_at must be a string",
            section="baseline",
        )
    for key in ("base", "stage"):
        value = baseline.get(key)
        if value is not None and not isinstance(value, str):
            raise CrpError(
                "invalid_input",
                f"ledger.baseline.{key} must be a string",
                section="baseline",
            )
    models = ledger.get("models")
    if models is not None and not isinstance(models, dict):
        raise CrpError("invalid_input", "ledger.models must be an object")
    validate_plan_tasks(ledger.get("plan"), require_tasks=False)
    for key in ("tasks", "agents", "decisions", "integration"):
        if not isinstance(ledger.get(key), dict):
            raise CrpError("invalid_input", f"ledger.{key} must be an object", key=key)
    if not isinstance(ledger.get("events"), list):
        raise CrpError("invalid_input", "ledger.events must be a list")


_SOFT_SHAPE_SECTIONS = ("tasks", "agents", "decisions")


def soft_shape_violations(ledger: dict) -> set[str]:
    """Classify soft-shape violations: sections whose values include non-dicts.

    integration is intentionally absent: its values are string-scalar gated at
    write time by ``_validate_integration_write`` and tolerated on read paths.

    Single-owner classification consumed by the per-command mapping: update
    rejects newly written violations (exit 2), resume degrades per its locked
    R1/R2 semantics (exit 0), load/list tolerate them.
    """

    violating: set[str] = set()
    for section in _SOFT_SHAPE_SECTIONS:
        mapping = ledger.get(section)
        if not isinstance(mapping, dict):
            continue
        if any(not isinstance(value, dict) for value in mapping.values()):
            violating.add(section)
    return violating


def _validate_integration_write(changes_integration: object) -> None:
    """Reject any non-string value written into the integration section.

    completion_gate requires top-level ``integration.latest_verdict == "ship"``
    and a string ``verdict_diff_fingerprint``, so a non-string value can never
    produce a legal state. Any dict/list/number/null value written into the
    integration section is rejected before the update lands; legacy non-scalar
    values already present in a stored ledger remain tolerated on read paths.
    """

    if not isinstance(changes_integration, dict):
        return
    for key, value in changes_integration.items():
        if not isinstance(value, str):
            raise CrpError(
                "invalid_input",
                "integration entries must be strings",
                key=key,
            )


def _load_ledger_file(path: Path) -> dict:
    try:
        data = crp_common.json_read(path)
    except FileNotFoundError as exc:
        raise CrpError("invalid_input", "ledger not found", path=str(path)) from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CrpError(
            "invalid_input",
            "ledger file is not valid JSON",
            path=str(path),
            error=str(exc),
        ) from exc
    if not isinstance(data, dict):
        raise CrpError("invalid_input", "ledger must be a JSON object", path=str(path))
    return data


def load_ledger(run_id: str, start: str | Path | None = None, codex_home: str | Path | None = None) -> dict:
    _validate_run_id(run_id)
    path = ledger_path(run_id, start=start, codex_home=codex_home)
    ledger = _load_ledger_file(path)
    _validate_ledger(ledger, run_id)
    return ledger


def write_ledger(
    ledger: dict,
    run_id: str,
    start: str | Path | None = None,
    codex_home: str | Path | None = None,
) -> Path:
    _validate_run_id(run_id)
    _validate_ledger(ledger, run_id)
    path = ledger_path(run_id, start=start, codex_home=codex_home)
    crp_common.atomic_json_write(path, ledger)
    return path


def _apply_changes(ledger: dict, changes: dict) -> dict:
    for key, value in changes.items():
        if key in _MERGE_DICT_KEYS and isinstance(value, dict) and isinstance(ledger.get(key), dict):
            ledger[key].update(value)
        elif key == "events" and isinstance(value, list):
            ledger.setdefault("events", []).extend(value)
        else:
            ledger[key] = value
    return ledger


def update_ledger(
    run_id: str,
    changes: dict,
    start: str | Path | None = None,
    codex_home: str | Path | None = None,
) -> dict:
    if not isinstance(changes, dict):
        raise CrpError("invalid_input", "changes must be an object")
    ledger = load_ledger(run_id, start=start, codex_home=codex_home)
    _validate_integration_write(changes.get("integration"))
    written_sections = set(changes) & set(_SOFT_SHAPE_SECTIONS)
    _apply_changes(ledger, changes)
    _validate_ledger(ledger, run_id)
    violating = soft_shape_violations(ledger) & written_sections
    if violating:
        raise CrpError(
            "invalid_input",
            "update would write malformed entries",
            sections=sorted(violating),
        )
    crp_common.atomic_json_write(
        ledger_path(run_id, start=start, codex_home=codex_home), ledger
    )
    return ledger


def valid_verification(record: dict) -> bool:
    """A verification record is only valid when it carries an integer exit_code."""

    return (
        isinstance(record, dict)
        and isinstance(record.get("exit_code"), int)
        and not isinstance(record.get("exit_code"), bool)
    )


def is_verification_fresh(record: dict, current_fingerprint: str) -> bool:
    """Fresh only when the record's diff_fingerprint equals the current one."""

    return isinstance(record, dict) and record.get("diff_fingerprint") == current_fingerprint


def verification_tier(change_facts: dict, task_facts: dict | None = None) -> dict | None:
    """Route a verification tier from change facts + task facts; None when unprovable."""

    if not isinstance(change_facts, dict):
        raise CrpError("invalid_input", "change facts must be an object")
    task_facts = task_facts if isinstance(task_facts, dict) else {}

    def candidate_state(key: str):
        candidate = change_facts.get(key)
        return candidate.get("state") if isinstance(candidate, dict) else None

    if task_facts.get("risk") == "HIGH":
        return {"tier": "FULL", "reasons": ["task risk HIGH"]}

    for key in _HIGH_RISK_KEYS:
        state = candidate_state(key)
        if state in _RISK_STATES:
            return {"tier": "FULL", "reasons": [f"{key} state={state}"]}

    if change_facts.get("migration_changed") is True:
        return {"tier": "INTEGRATION", "reasons": ["migration changed"]}
    if (
        change_facts.get("dependency_manifest_changed") is True
        or change_facts.get("lockfile_changed") is True
    ):
        return {"tier": "INTEGRATION", "reasons": ["dependency/lockfile changed"]}

    classes = change_facts.get("changed_file_classes") or {}
    if isinstance(classes, dict) and classes.get("contract/interface candidate"):
        return {"tier": "INTEGRATION", "reasons": ["public contract/interface changed"]}
    if candidate_state("public_api_candidate") in _RISK_STATES:
        return {"tier": "INTEGRATION", "reasons": ["public API candidate detected"]}

    modules = change_facts.get("modules") or []
    if isinstance(modules, list) and modules:
        if len(modules) == 1:
            return {"tier": "MODULE", "reasons": ["single module changed"]}
        return {"tier": "INTEGRATION", "reasons": ["multiple modules changed"]}

    if change_facts.get("tests_changed") is True:
        return {"tier": "TARGETED", "reasons": ["tests changed"]}

    changed = list(change_facts.get("changed_files") or []) + list(
        change_facts.get("untracked_files") or []
    )
    if len(changed) == 1:
        return {"tier": "TARGETED", "reasons": ["single file changed"]}

    return None


def _agent_is_running(agent: dict) -> bool:
    """Fail-closed running check for ``resume_state``.

    Both state fields must be strings before any membership test. A missing
    or non-string field (list/dict/int/None) cannot prove a non-running
    agent, so it is treated as running; a non-dict record is likewise
    treated as running. Never raises: malformed records surface as
    structured ``running_agents`` entries, never as TypeError/internal_error.
    """

    if not isinstance(agent, dict):
        return True
    state = agent.get("lifecycle_state")
    if not isinstance(state, str):
        return True
    if state in _AGENT_RUNNING_STATES:
        return True
    runtime = agent.get("last_observed_runtime_state")
    if not isinstance(runtime, str):
        return True
    return runtime in ("running", "active")


def resume_state(
    run_id: str,
    start: str | Path | None = None,
    current_plan: dict | None = None,
    codex_home: str | Path | None = None,
) -> dict:
    """Compute the safe resume state for one run: never guess, never redispatch."""

    ledger = _load_ledger_file(
        ledger_path(run_id, start=start, codex_home=codex_home)
    )
    try:
        _validate_ledger(ledger, run_id)
    except CrpError as error:
        if error.details.get("section") == "baseline":
            # Cannot prove plan freshness without a well-formed baseline:
            # require reconfirmation (structured, exit 3 via the CLI).
            return {
                "ok": False,
                "run_id": ledger.get("run_id", run_id),
                "plan_reconfirmation_required": True,
                "running_agents": [],
                "tasks": {},
            }
        raise
    result = {
        "ok": True,
        "run_id": ledger.get("run_id", run_id),
        "plan_reconfirmation_required": False,
        "running_agents": [],
        "tasks": {},
    }
    if current_plan is not None:
        stored = (ledger.get("baseline") or {}).get("plan_fingerprint")
        if stored != plan_fingerprint(current_plan):
            result["ok"] = False
            result["plan_reconfirmation_required"] = True
            return result

    running_by_task: dict[str, dict] = {}
    for agent_id, agent in sorted((ledger.get("agents") or {}).items()):
        if not isinstance(agent, dict):
            # Malformed record: fail closed as still running while keeping
            # the output structured (no .get on non-dicts, no TypeError).
            result["running_agents"].append(
                {
                    "agent_id": str(agent_id),
                    "role": None,
                    "task_id": None,
                    "lifecycle_state": None,
                }
            )
            continue
        if not _agent_is_running(agent):
            continue
        info = {
            "agent_id": agent.get("agent_id", agent_id),
            "role": agent.get("role"),
            "task_id": agent.get("task_id"),
            "lifecycle_state": agent.get("lifecycle_state"),
        }
        result["running_agents"].append(info)
        task_id = agent.get("task_id")
        if isinstance(task_id, str) and task_id:
            running_by_task[task_id] = info

    for task_id in sorted(ledger.get("tasks") or {}):
        task = ledger["tasks"][task_id]
        if not isinstance(task, dict):
            # Malformed task record: keep the output structured and fail
            # closed without guessing state; mirrors completion_gate's
            # fail-closed treatment of non-dict task records. Never call
            # .get on a non-dict. query_first never redispatches and never
            # asserts a shipped/fix-first fact we cannot prove.
            result["tasks"][task_id] = {
                "state": None,
                "latest_verdict": None,
                "pending_fix": None,
                "owner_coder": None,
                "resume_action": "query_first",
            }
            continue
        verdict = task.get("latest_verdict")
        pending_fix = task.get("pending_fix") is True
        owner = task.get("owner_coder")
        if task_id in running_by_task:
            action = "query_first"
        elif verdict == "ship" and not pending_fix:
            action = "no_redispatch"
        elif verdict == "fix-first" and pending_fix:
            action = "resume_same" if owner else "blocked_no_owner"
        else:
            action = "continue"
        result["tasks"][task_id] = {
            "state": task.get("state"),
            "latest_verdict": verdict,
            "pending_fix": task.get("pending_fix"),
            "owner_coder": owner,
            "resume_action": action,
        }
    return result


def _plan_summary(plan: dict) -> str:
    if not isinstance(plan, dict):
        return ""
    for key in ("summary", "objective", "name"):
        value = plan.get(key)
        if isinstance(value, str) and value.strip():
            return value
    tasks = plan.get("tasks")
    if isinstance(tasks, list) and tasks:
        return f"{len(tasks)} task(s)"
    return ""


def list_runs(start: str | Path | None = None, codex_home: str | Path | None = None) -> list[dict]:
    """List all ledgered runs without guessing which one to resume."""

    directory = runs_dir(start=start, codex_home=codex_home)
    if not directory.is_dir():
        return []
    runs: list[dict] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        ledger_file = child / "ledger.json"
        if not ledger_file.is_file():
            continue
        try:
            ledger = _load_ledger_file(ledger_file)
        except CrpError as error:
            runs.append(
                {
                    "run_id": child.name,
                    "corrupt": True,
                    "error": error.message,
                    "error_code": error.code,
                }
            )
            continue
        try:
            _validate_ledger(ledger)
        except CrpError as error:
            runs.append(
                {
                    "run_id": child.name,
                    "corrupt": True,
                    "error": error.message,
                    "error_code": error.code,
                }
            )
            continue
        baseline = ledger.get("baseline") or {}
        runs.append(
            {
                "run_id": ledger.get("run_id", child.name),
                "plan_summary": _plan_summary(ledger.get("plan") or {}),
                "created_at": baseline.get("created_at"),
                "base": baseline.get("base"),
                "stage": baseline.get("stage"),
            }
        )
    return runs


def _load_json_arg(path: str, label: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CrpError("invalid_input", f"{label} file not found", path=path) from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CrpError(
            "invalid_input",
            f"{label} file is not valid JSON",
            path=path,
            error=str(exc),
        ) from exc


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))


class _CrpArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit_error(CrpError("invalid_input", "invalid arguments", detail=message))
        raise SystemExit(exit_code("invalid_input"))


def main(argv: list[str] | None = None) -> int:
    reconfigure_stdio()
    parser = _CrpArgumentParser(
        prog="run_ledger.py",
        description="Persistent run ledger and programmatic verification router.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_p = subparsers.add_parser("init", help="create a new run ledger")
    init_p.add_argument("--run-id", required=True)
    init_p.add_argument("--repo", default=None, help="repository path (default: cwd)")
    init_p.add_argument("--plan", default=None, help="plan JSON file")
    init_p.add_argument("--baseline", default=None, help="baseline JSON file")
    init_p.add_argument("--models", default=None, help="models JSON file")
    init_p.add_argument("--codex-home", default=None, help="CODEX_HOME override for NON_GIT")

    update_p = subparsers.add_parser("update", help="atomically update a ledger")
    update_p.add_argument("--run-id", required=True)
    update_p.add_argument("--changes", required=True, help="partial changes JSON file")
    update_p.add_argument("--repo", default=None)
    update_p.add_argument("--codex-home", default=None)

    load_p = subparsers.add_parser("load", help="load a ledger")
    load_p.add_argument("--run-id", required=True)
    load_p.add_argument("--repo", default=None)
    load_p.add_argument("--codex-home", default=None)

    list_p = subparsers.add_parser("list", help="list runs")
    list_p.add_argument("--repo", default=None)
    list_p.add_argument("--codex-home", default=None)

    resume_p = subparsers.add_parser("resume", help="compute resume state")
    resume_p.add_argument("--run-id", required=True)
    resume_p.add_argument("--repo", default=None)
    resume_p.add_argument("--plan", default=None, help="current plan JSON file")
    resume_p.add_argument("--codex-home", default=None)

    tier_p = subparsers.add_parser("verification-tier", help="route verification tier")
    tier_p.add_argument("--facts", required=True, help="change facts JSON file")
    tier_p.add_argument("--task-facts", default=None, help="task facts JSON file")

    fp_p = subparsers.add_parser("plan-fingerprint", help="compute plan fingerprint")
    fp_p.add_argument("--plan", required=True, help="plan JSON file")

    dfp_p = subparsers.add_parser("diff-fingerprint", help="compute diff fingerprint")
    dfp_p.add_argument("--facts", required=True, help="change facts JSON file")

    args = parser.parse_args(argv)
    status = EXIT_OK
    try:
        output: object = None
        if args.command == "init":
            repo_root = args.repo if args.repo is not None else str(Path.cwd())
            ledger = new_ledger(
                args.run_id,
                repo_root,
                plan=_load_json_arg(args.plan, "plan") if args.plan else None,
                baseline=_load_json_arg(args.baseline, "baseline") if args.baseline else None,
                models=_load_json_arg(args.models, "models") if args.models else None,
            )
            write_ledger(ledger, args.run_id, start=args.repo, codex_home=args.codex_home)
            output = ledger
        elif args.command == "update":
            output = update_ledger(
                args.run_id,
                _load_json_arg(args.changes, "changes"),
                start=args.repo,
                codex_home=args.codex_home,
            )
        elif args.command == "load":
            output = load_ledger(args.run_id, start=args.repo, codex_home=args.codex_home)
        elif args.command == "list":
            output = {"runs": list_runs(start=args.repo, codex_home=args.codex_home)}
        elif args.command == "resume":
            plan = _load_json_arg(args.plan, "plan") if args.plan else None
            output = resume_state(
                args.run_id, start=args.repo, current_plan=plan, codex_home=args.codex_home
            )
            if output.get("plan_reconfirmation_required"):
                status = exit_code("policy_blocked")
        elif args.command == "verification-tier":
            output = verification_tier(
                _load_json_arg(args.facts, "change facts"),
                _load_json_arg(args.task_facts, "task facts") if args.task_facts else None,
            )
        elif args.command == "plan-fingerprint":
            output = {"plan_fingerprint": plan_fingerprint(_load_json_arg(args.plan, "plan"))}
        elif args.command == "diff-fingerprint":
            output = {"diff_fingerprint": diff_fingerprint(_load_json_arg(args.facts, "change facts"))}
        _print(output)
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")
    return status


if __name__ == "__main__":
    sys.exit(main())
