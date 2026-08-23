"""Tests for the programmatic wait/execution strategy (P-wait)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
SCRIPT = SCRIPTS / "wait_strategy.py"


def run_cli(payload: dict) -> tuple[int, dict | None, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, proc.stderr


class TestExecutionMode(unittest.TestCase):
    def test_small_suite_foreground(self):
        code, out, err = run_cli({"test_files": 5})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["mode"], "foreground_wait")

    def test_medium_suite_background_file(self):
        code, out, err = run_cli({"test_files": 20})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["mode"], "background_file")

    def test_large_suite_background_poll(self):
        code, out, err = run_cli({"test_files": 50})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["mode"], "background_poll")

    def test_count_overrides_files_boundary(self):
        code, out, err = run_cli({"test_files": 20, "test_count": 700})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["mode"], "background_poll")


class TestWaitBudget(unittest.TestCase):
    def test_single_task_normal_floor(self):
        code, out, err = run_cli({"tasks": 1, "files": 2})
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(out["wait_agent_ms"], 600_000)
        self.assertEqual(out["polling"], "single_long_wait")

    def test_more_tasks_longer_wait(self):
        _, small, _ = run_cli({"tasks": 1, "files": 5})
        _, big, _ = run_cli({"tasks": 4, "files": 40, "risk": "HIGH"})
        self.assertGreater(big["wait_agent_ms"], small["wait_agent_ms"])

    def test_cap_at_max(self):
        code, out, err = run_cli({"tasks": 12, "files": 200, "risk": "HIGH"})
        self.assertEqual(code, 0, err)
        self.assertLessEqual(out["wait_agent_ms"], 1_800_000)

    def test_invalid_risk_rejected(self):
        code, out, err = run_cli({"tasks": 1, "risk": "WHENEVER"})
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
