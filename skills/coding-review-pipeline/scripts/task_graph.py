"""Task Graph CLI (plan sections 28-32).

Computes deterministic scheduling facts for a fixed task schema: cycle
detection via strongly connected components, topological order, transitive
ancestors, ready queue, blocked successors, write-set overlap and parallel
feasibility. The program decides CAN (split/parallel/overlap/dependency);
the main agent decides SHOULD and business cohesion.

Parallel-safe pairs are restricted to acyclic graphs and require both tasks
unfinished and ready, no direct or transitive dependency, and disjoint write
sets. A cycle clears ready and parallel-safe; a non-predecessor-closed
completed set is invalid input.

Output: machine-readable JSON on stdout. Exit 0 on a valid acyclic graph,
3 (policy_blocked) when the graph contains a cycle, 2 (invalid_input) for
malformed input, 1 (internal_error) for unexpected failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from itertools import combinations
from pathlib import Path

import crp_common
from crp_common import (
    CrpError,
    EXIT_OK,
    emit_error,
    exit_code,
)


REQUIRED_TASK_FIELDS = (
    "TASK_ID",
    "DELIVERABLE",
    "WHY_ONE_TASK",
    "INDEPENDENT_ACCEPTANCE",
    "WRITE_SET",
    "READ_ONLY",
    "PREDECESSORS",
    "SUCCESSORS",
    "VERIFICATION_UNIT",
    "PARALLELISM",
)

LIST_FIELDS = ("WRITE_SET", "READ_ONLY", "PREDECESSORS", "SUCCESSORS")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
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


def _load_tasks(path: str) -> list[dict]:
    data = _load_json(path, "tasks")
    if isinstance(data, dict) and "tasks" in data:
        data = data["tasks"]
    if not isinstance(data, list):
        raise CrpError("invalid_input", "tasks file must be a list or an object with a 'tasks' list")
    tasks: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            raise CrpError("invalid_input", "each task must be a JSON object")
        tasks.append(item)
    return tasks


def _load_completed(path: str | None) -> list[str]:
    if path is None:
        return []
    data = _load_json(path, "completed")
    if isinstance(data, dict) and "completed" in data:
        data = data["completed"]
    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        raise CrpError("invalid_input", "completed file must be a list of task ids")
    if len(data) != len(set(data)):
        raise CrpError("invalid_input", "completed must not contain duplicate task ids")
    return data


def _is_reserved_device_segment(segment: str) -> bool:
    return segment.split(".", 1)[0].upper() in _RESERVED_DEVICE_NAMES


def normalize_repo_path(path: str) -> tuple[str, str]:
    """Return (normalized_posix, casefold_key) or raise CrpError for unsafe paths.

    Shared Windows-safe repo path normalization. Per-segment rules reject
    trailing dots/spaces, colons (alternate data streams) and Windows
    reserved device names (CON/PRN/AUX/NUL and COM1-9/LPT1-9, including with
    extensions). Comparison keys use casefold for Windows case-insensitive
    semantics.
    """

    raw = path.replace("\\", "/")
    if not raw.strip():
        raise CrpError("invalid_input", "path must be a non-empty repo-relative path", path=path)
    if _DRIVE_PREFIX.match(raw.lstrip()) or raw.lstrip().startswith("/"):
        raise CrpError("invalid_input", "path must be repo-relative, not absolute", path=path)
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise CrpError("invalid_input", "path escapes the repository", path=path)
            parts.pop()
            continue
        if segment.endswith((" ", ".")):
            raise CrpError(
                "invalid_input",
                "path segment has a trailing dot or space",
                path=path,
                segment=segment,
            )
        if ":" in segment:
            raise CrpError(
                "invalid_input",
                "path contains a colon (alternate data streams are not allowed)",
                path=path,
                segment=segment,
            )
        if _is_reserved_device_segment(segment):
            raise CrpError(
                "invalid_input",
                "path uses a Windows reserved device name",
                path=path,
                segment=segment,
            )
        parts.append(segment)
    if not parts:
        raise CrpError("invalid_input", "path must name a file inside the repository", path=path)
    normalized = "/".join(parts)
    return normalized, normalized.casefold()


def _validate_paths(task: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Validate WRITE_SET/READ_ONLY; return {casefold_key: normalized} maps."""

    task_id = task.get("TASK_ID")
    write_keys: dict[str, str] = {}
    read_keys: dict[str, str] = {}
    for field, bucket in (("WRITE_SET", write_keys), ("READ_ONLY", read_keys)):
        for entry in task[field]:
            normalized, key = normalize_repo_path(entry)
            if key in bucket:
                raise CrpError(
                    "invalid_input",
                    "duplicate normalized path in task ownership",
                    task_id=task_id,
                    field=field,
                    path=entry,
                    normalized=normalized,
                )
            bucket[key] = normalized
    common = set(write_keys) & set(read_keys)
    if common:
        key = sorted(common)[0]
        raise CrpError(
            "invalid_input",
            "WRITE_SET and READ_ONLY conflict for the same path",
            task_id=task_id,
            path=write_keys[key],
        )
    return write_keys, read_keys


def _validate_task(task: dict, known_ids: set[str]) -> None:
    task_id = task.get("TASK_ID")
    for field in REQUIRED_TASK_FIELDS:
        value = task.get(field)
        if field in LIST_FIELDS:
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise CrpError(
                    "invalid_input",
                    "task field must be a list of non-empty strings",
                    task_id=task_id,
                    field=field,
                )
        elif not isinstance(value, str) or not value:
            raise CrpError(
                "invalid_input",
                "task field must be a non-empty string",
                task_id=task_id,
                field=field,
            )
    for field in ("PREDECESSORS", "SUCCESSORS"):
        values = task[field]
        duplicates = sorted({ref for ref in values if values.count(ref) > 1})
        if duplicates:
            raise CrpError(
                "invalid_input",
                "task dependency set must not contain duplicates",
                task_id=task_id,
                field=field,
                duplicates=duplicates,
            )
        for ref in task[field]:
            if ref not in known_ids:
                raise CrpError(
                    "invalid_input",
                    "task references unknown task id",
                    task_id=task_id,
                    field=field,
                    reference=ref,
                )


def _validate_tasks(tasks: list[dict]) -> tuple[dict[str, dict], dict[str, tuple[dict[str, str], dict[str, str]]]]:
    if not tasks:
        raise CrpError("invalid_input", "tasks must not be empty")
    ids = [task.get("TASK_ID") for task in tasks]
    if any(not isinstance(task_id, str) or not task_id for task_id in ids):
        raise CrpError("invalid_input", "TASK_ID must be a non-empty string")
    if len(set(ids)) != len(ids):
        raise CrpError("invalid_input", "TASK_ID values must be unique")
    known = set(ids)
    for task in tasks:
        _validate_task(task, known)
    by_id = {task["TASK_ID"]: task for task in tasks}
    for task in tasks:
        task_id = task["TASK_ID"]
        for successor in task["SUCCESSORS"]:
            if task_id not in by_id[successor]["PREDECESSORS"]:
                raise CrpError(
                    "invalid_input",
                    "successor declaration has no matching predecessor",
                    task_id=task_id,
                    successor=successor,
                )
        for predecessor in task["PREDECESSORS"]:
            if task_id not in by_id[predecessor]["SUCCESSORS"]:
                raise CrpError(
                    "invalid_input",
                    "predecessor declaration has no matching successor",
                    task_id=task_id,
                    predecessor=predecessor,
                )
    path_maps = {task["TASK_ID"]: _validate_paths(task) for task in tasks}
    return by_id, path_maps


def _strongly_connected_components(
    ids: list[str],
    successors: dict[str, list[str]],
) -> list[list[str]]:
    """Kosaraju SCC decomposition with deterministic input-order iteration."""

    adjacency = {task_id: list(successors[task_id]) for task_id in ids}
    reverse = {task_id: [] for task_id in ids}
    for task_id in ids:
        for child in successors[task_id]:
            reverse[child].append(task_id)

    visited: set[str] = set()
    finish: list[str] = []
    for start in ids:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                finish.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for child in adjacency[node]:
                if child not in visited:
                    stack.append((child, False))

    assigned: set[str] = set()
    components: list[list[str]] = []
    for start in reversed(finish):
        if start in assigned:
            continue
        component: list[str] = []
        stack = [start]
        assigned.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for parent in reverse[node]:
                if parent not in assigned:
                    assigned.add(parent)
                    stack.append(parent)
        components.append(sorted(component))
    components.sort(key=lambda component: component[0])
    return components


def _cycles_from_components(
    components: list[list[str]],
    successors: dict[str, list[str]],
) -> list[list[str]]:
    return [
        component
        for component in components
        if len(component) > 1 or (len(component) == 1 and component[0] in successors[component[0]])
    ]


def analyze(tasks: list[dict], completed: list[str]) -> dict:
    """Build the deterministic scheduling analysis for the task graph."""

    by_id, path_maps = _validate_tasks(tasks)
    ids = [task["TASK_ID"] for task in tasks]
    predecessors = {task_id: list(task["PREDECESSORS"]) for task_id, task in by_id.items()}
    successors = {task_id: list(task["SUCCESSORS"]) for task_id, task in by_id.items()}

    unknown_completed = [task_id for task_id in completed if task_id not in by_id]
    if unknown_completed:
        raise CrpError(
            "invalid_input",
            "completed references unknown task id",
            completed=unknown_completed,
        )
    done = set(completed)

    transitive_ancestors: dict[str, list[str]] = {}
    for task_id in ids:
        seen: set[str] = set()
        stack = list(predecessors[task_id])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(predecessors[node])
        transitive_ancestors[task_id] = sorted(seen)

    for task_id in done:
        missing = [ancestor for ancestor in transitive_ancestors[task_id] if ancestor not in done]
        if missing:
            raise CrpError(
                "invalid_input",
                "completed set is not predecessor-closed",
                task_id=task_id,
                missing_predecessors=missing,
            )

    components = _strongly_connected_components(ids, successors)
    cycles = _cycles_from_components(components, successors)
    has_cycle = bool(cycles)

    if not has_cycle:
        indegree = {task_id: len(predecessors[task_id]) for task_id in ids}
        queue = [task_id for task_id in ids if indegree[task_id] == 0]
        order: list[str] = []
        while queue:
            task_id = queue.pop(0)
            order.append(task_id)
            for successor in successors[task_id]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    queue.append(successor)
    else:
        order = []

    if has_cycle:
        ready: list[str] = []
        parallel_safe: list[list[str]] = []
    else:
        ready = [
            task_id
            for task_id in ids
            if task_id not in done and all(pred in done for pred in predecessors[task_id])
        ]
        parallel_safe = []
        for first, second in combinations(ids, 2):
            if first in done or second in done:
                continue
            if first not in ready or second not in ready:
                continue
            if second in predecessors[first] or first in predecessors[second]:
                continue
            if first in transitive_ancestors[second] or second in transitive_ancestors[first]:
                continue
            if not (set(path_maps[first][0]) & set(path_maps[second][0])):
                parallel_safe.append([first, second])

    blocked = [
        {
            "task_id": task_id,
            "missing_predecessors": [pred for pred in predecessors[task_id] if pred not in done],
        }
        for task_id in ids
        if task_id not in done and not all(pred in done for pred in predecessors[task_id])
    ]
    blocked_ids = {item["task_id"] for item in blocked}
    blocked_successors = {
        task_id: [succ for succ in successors[task_id] if succ in blocked_ids]
        for task_id in ids
        if any(succ in blocked_ids for succ in successors[task_id])
    }

    write_set_overlaps = []
    for first, second in combinations(ids, 2):
        common_keys = set(path_maps[first][0]) & set(path_maps[second][0])
        if common_keys:
            common = sorted({path_maps[first][0][key] for key in common_keys})
            write_set_overlaps.append({"tasks": [first, second], "overlap": common})

    dependencies = sorted([pred, task_id] for task_id in ids for pred in predecessors[task_id])
    return {
        "ok": not has_cycle,
        "status": "BLOCKED" if has_cycle else "OK",
        "task_ids": ids,
        "has_cycle": has_cycle,
        "cycles": cycles,
        "topological_order": order,
        "transitive_ancestors": transitive_ancestors,
        "ready": ready,
        "blocked": blocked,
        "blocked_successors": blocked_successors,
        "dependencies": dependencies,
        "write_set_overlaps": write_set_overlaps,
        "parallel_safe": parallel_safe,
        "can_split": not has_cycle and len(ids) >= 2,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task_graph.py",
        description="Compute task graph scheduling facts for a fixed task schema.",
    )
    parser.add_argument("--tasks", required=True, help="tasks JSON file (list or {'tasks': [...]})")
    parser.add_argument("--completed", default=None, help="completed task ids JSON file (optional)")
    args = parser.parse_args(argv)
    try:
        tasks = _load_tasks(args.tasks)
        completed = _load_completed(args.completed)
        result = analyze(tasks, completed)
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return EXIT_OK if not result["has_cycle"] else exit_code("policy_blocked")


if __name__ == "__main__":
    sys.exit(main())
