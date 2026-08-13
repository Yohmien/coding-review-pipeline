"""Tests for the Task Graph CLI (plan sections 28-32, 114-115)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
TASK_GRAPH = SCRIPTS / "task_graph.py"

WINDOWS_RESERVED_NAMES = (
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
)


def _write_json(data: object) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-graph-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return Path(name)


def base_task(task_id: str, **overrides: object) -> dict:
    task: dict = {
        "TASK_ID": task_id,
        "DELIVERABLE": f"{task_id} deliverable",
        "WHY_ONE_TASK": "single invariant",
        "INDEPENDENT_ACCEPTANCE": f"{task_id} acceptance",
        "WRITE_SET": [],
        "READ_ONLY": [],
        "PREDECESSORS": [],
        "SUCCESSORS": [],
        "VERIFICATION_UNIT": f"{task_id} verification",
        "PARALLELISM": "SERIAL",
    }
    task.update(overrides)
    return task


def run_graph(tasks: list[dict], completed: list[str] | None = None) -> tuple[int, dict | None, dict | None]:
    paths: list[Path] = []
    task_path = _write_json(tasks)
    paths.append(task_path)
    args = [sys.executable, str(TASK_GRAPH), "--tasks", str(task_path)]
    if completed is not None:
        completed_path = _write_json(completed)
        paths.append(completed_path)
        args += ["--completed", str(completed_path)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    err = None
    if proc.stderr.strip():
        try:
            err = json.loads(proc.stderr)
        except json.JSONDecodeError:
            err = {"error": {"code": "unparseable", "message": proc.stderr.strip()}}
    return proc.returncode, out, err


class TaskGraphCliTest(unittest.TestCase):
    def test_chain_topology_and_ready_queue(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertFalse(out["has_cycle"])
        self.assertTrue(out["can_split"])
        self.assertEqual(out["topological_order"], ["T1", "T2"])
        self.assertEqual(out["ready"], ["T1"])
        self.assertEqual(out["blocked"], [{"task_id": "T2", "missing_predecessors": ["T1"]}])
        self.assertEqual(out["blocked_successors"], {"T1": ["T2"]})

    def test_cycle_detected_policy_block(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"], PREDECESSORS=["T2"]),
            base_task("T2", SUCCESSORS=["T1"], PREDECESSORS=["T1"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 3, err)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out["has_cycle"])
        self.assertEqual(out["topological_order"], [])
        self.assertFalse(out["can_split"])
        self.assertTrue(
            any(sorted(cycle) == ["T1", "T2"] and len(cycle) == 2 for cycle in out["cycles"])
        )

    def test_diamond_topology_and_blocked_successors(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2", "T3"]),
            base_task("T2", PREDECESSORS=["T1"], SUCCESSORS=["T4"]),
            base_task("T3", PREDECESSORS=["T1"], SUCCESSORS=["T4"]),
            base_task("T4", PREDECESSORS=["T2", "T3"]),
        ]
        code, out, err = run_graph(tasks, completed=["T1"])
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["topological_order"], ["T1", "T2", "T3", "T4"])
        self.assertEqual(out["ready"], ["T2", "T3"])
        self.assertEqual(out["blocked"], [{"task_id": "T4", "missing_predecessors": ["T2", "T3"]}])
        self.assertEqual(out["blocked_successors"], {"T2": ["T4"], "T3": ["T4"]})

    def test_independent_disjoint_writes_parallel_safe(self) -> None:
        tasks = [
            base_task("T1", WRITE_SET=["src/a.py"], PARALLELISM="PARALLEL"),
            base_task("T2", WRITE_SET=["src/b.py"], PARALLELISM="PARALLEL"),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["ready"], ["T1", "T2"])
        self.assertEqual(out["dependencies"], [])
        self.assertEqual(out["write_set_overlaps"], [])
        self.assertEqual(out["parallel_safe"], [["T1", "T2"]])

    def test_same_file_parallel_false(self) -> None:
        tasks = [
            base_task("T1", WRITE_SET=["src/shared.py"]),
            base_task("T2", WRITE_SET=["src/shared.py"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["parallel_safe"], [])
        self.assertEqual(
            out["write_set_overlaps"],
            [{"tasks": ["T1", "T2"], "overlap": ["src/shared.py"]}],
        )

    def test_path_normalization_detects_overlap(self) -> None:
        tasks = [
            base_task("T1", WRITE_SET=["./src/a.py"]),
            base_task("T2", WRITE_SET=["src/a.py"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["write_set_overlaps"], [{"tasks": ["T1", "T2"], "overlap": ["src/a.py"]}])
        self.assertEqual(out["parallel_safe"], [])

    def test_ready_queue_respects_completed(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"]),
        ]
        code, out, err = run_graph(tasks, completed=["T1"])
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["ready"], ["T2"])
        self.assertEqual(out["blocked"], [])
        self.assertEqual(out["blocked_successors"], {})

    def test_missing_task_id_invalid_input(self) -> None:
        task = base_task("T1")
        del task["TASK_ID"]
        code, out, err = run_graph([task])
        self.assertEqual(code, 2, err)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_undecodable_tasks_file_exits_2(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-graph-bad-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b'[{"TASK_ID": "\xff\xfe"}]')
        path = Path(name)
        try:
            proc = subprocess.run(
                [sys.executable, str(TASK_GRAPH), "--tasks", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_unknown_predecessor_invalid_input(self) -> None:
        tasks = [
            base_task("T1"),
            base_task("T2", PREDECESSORS=["T9"]),
        ]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_inconsistent_successor_declaration_invalid_input(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2"),
        ]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_transitive_ancestors(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"], SUCCESSORS=["T3"]),
            base_task("T3", PREDECESSORS=["T2"], SUCCESSORS=["T4"]),
            base_task("T4", PREDECESSORS=["T3"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(
            out["transitive_ancestors"],
            {"T1": [], "T2": ["T1"], "T3": ["T1", "T2"], "T4": ["T1", "T2", "T3"]},
        )

    def test_parallel_safe_requires_both_ready(self) -> None:
        tasks = [
            base_task("T1", WRITE_SET=["src/a.py"], SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"], WRITE_SET=["src/b.py"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["ready"], ["T1"])
        self.assertEqual(out["parallel_safe"], [])
        code, out, err = run_graph(tasks, completed=["T1"])
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["ready"], ["T2"])
        self.assertEqual(out["parallel_safe"], [])

    def test_completed_must_be_predecessor_closed(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"]),
        ]
        code, _, err = run_graph(tasks, completed=["T2"])
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_completed_unknown_task_invalid(self) -> None:
        tasks = [base_task("T1")]
        code, _, err = run_graph(tasks, completed=["T9"])
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_cycle_clears_ready_and_parallel_safe(self) -> None:
        tasks = [
            base_task("T1", PREDECESSORS=["T2"], SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"], SUCCESSORS=["T1"]),
            base_task("T3", WRITE_SET=["src/c.py"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertTrue(out["has_cycle"])
        self.assertEqual(out["ready"], [])
        self.assertEqual(out["parallel_safe"], [])

    def test_shared_node_cycle_single_scc(self) -> None:
        tasks = [
            base_task("T1", PREDECESSORS=["T2"], SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1", "T3"], SUCCESSORS=["T1", "T3"]),
            base_task("T3", PREDECESSORS=["T2"], SUCCESSORS=["T2"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["cycles"], [["T1", "T2", "T3"]])

    def test_multiple_sccs_stable_order(self) -> None:
        tasks = [
            base_task("T1", PREDECESSORS=["T2"], SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"], SUCCESSORS=["T1"]),
            base_task("T3", PREDECESSORS=["T4"], SUCCESSORS=["T4"]),
            base_task("T4", PREDECESSORS=["T3"], SUCCESSORS=["T3"]),
        ]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["cycles"], [["T1", "T2"], ["T3", "T4"]])

    def test_self_loop_cycle(self) -> None:
        tasks = [base_task("T1", PREDECESSORS=["T1"], SUCCESSORS=["T1"])]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 3, err)
        assert out is not None
        self.assertEqual(out["cycles"], [["T1"]])

    def test_empty_task_list_invalid(self) -> None:
        code, _, err = run_graph([])
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_single_task_ok_but_not_splittable(self) -> None:
        tasks = [base_task("T1", WRITE_SET=["src/a.py"])]
        code, out, err = run_graph(tasks)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertFalse(out["has_cycle"])
        self.assertFalse(out["can_split"])
        self.assertEqual(out["ready"], ["T1"])
        self.assertEqual(out["parallel_safe"], [])

    def test_absolute_write_set_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=[r"C:\repo\src\a.py"])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_traversal_write_set_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=["../outside.py"])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_case_insensitive_duplicate_write_set_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=[r"src\a.py", "SRC/A.PY"])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_write_read_conflict_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=["src/a.py"], READ_ONLY=["SRC/A.PY"])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_reserved_device_names_with_extension_invalid(self) -> None:
        for name in WINDOWS_RESERVED_NAMES:
            with self.subTest(name=name):
                tasks = [base_task("T1", WRITE_SET=[f"src/{name}.txt"])]
                code, _, err = run_graph(tasks)
                self.assertEqual(code, 2, err)
                assert err is not None
                self.assertEqual(err["error"]["code"], "invalid_input")

    def test_reserved_device_name_aliases_without_extension_invalid(self) -> None:
        for entry in ("con", "CON", "prn", "PRN", "aux", "AUX", "nul", "NUL", "COM1", "com9", "LPT1", "lpt9"):
            with self.subTest(entry=entry):
                tasks = [base_task("T1", WRITE_SET=[entry])]
                code, _, err = run_graph(tasks)
                self.assertEqual(code, 2, err)
                assert err is not None
                self.assertEqual(err["error"]["code"], "invalid_input")

    def test_trailing_dot_segment_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=["src/a."])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_trailing_space_segment_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=["src/a "])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_colon_ads_invalid(self) -> None:
        tasks = [base_task("T1", WRITE_SET=["src/a:b.txt"])]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_duplicate_predecessors_invalid(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1", "T1"]),
        ]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_duplicate_successors_invalid(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2", "T2"]),
            base_task("T2", PREDECESSORS=["T1"]),
        ]
        code, _, err = run_graph(tasks)
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_duplicate_completed_invalid(self) -> None:
        tasks = [
            base_task("T1", SUCCESSORS=["T2"]),
            base_task("T2", PREDECESSORS=["T1"]),
        ]
        code, _, err = run_graph(tasks, completed=["T1", "T1"])
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
