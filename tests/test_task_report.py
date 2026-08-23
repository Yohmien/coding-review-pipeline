"""Tests for programmatic phase-report write/read."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
SCRIPT = SCRIPTS / "task_report.py"


class TaskReportCliTest(unittest.TestCase):
    def setUp(self):
        self.codex_home = Path(tempfile.mkdtemp(prefix="crp-report-home-"))
        self.addCleanup(lambda: _rmtree(self.codex_home))

    def _run(self, *args, stdin_text=None):
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.codex_home)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=env,
        )

    def test_write_then_read_latest(self):
        entry = {
            "task_id": "T1",
            "phase": "IMPLEMENT",
            "status": "completed",
            "summary": "added handler and tests",
            "files_changed": ["src/A.java"],
        }
        proc = self._run("write", "--run-id", "run1", stdin_text=json.dumps(entry))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        read = self._run("read", "--run-id", "run1", "--task-id", "T1")
        self.assertEqual(read.returncode, 0, read.stderr)
        out = json.loads(read.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["report"]["summary"], "added handler and tests")

    def test_latest_wins(self):
        for status in ("in_progress", "completed"):
            entry = {"task_id": "T2", "phase": "VERIFY", "status": status, "summary": "s-" + status}
            proc = self._run("write", "--run-id", "run1", stdin_text=json.dumps(entry))
            self.assertEqual(proc.returncode, 0, proc.stderr)
        read = self._run("read", "--run-id", "run1", "--task-id", "T2")
        out = json.loads(read.stdout)
        self.assertEqual(out["report"]["status"], "completed")

    def test_missing_required_field_rejected(self):
        bad = {"task_id": "T3", "phase": "IMPLEMENT"}
        proc = self._run("write", "--run-id", "run1", stdin_text=json.dumps(bad))
        self.assertEqual(proc.returncode, 2)

    def test_read_without_reports_returns_no_report(self):
        read = self._run("read", "--run-id", "empty-run")
        self.assertEqual(read.returncode, 0)
        out = json.loads(read.stdout)
        self.assertFalse(out["ok"])


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
