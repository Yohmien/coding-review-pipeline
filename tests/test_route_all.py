"""Integration tests for the route_all combined routing entry (P7)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
ROUTE_ALL = SCRIPTS / "route_all.py"


def _make_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="crp-route-all-"))
    src = repo / "src"
    src.mkdir()
    (src / "A.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
        capture_output=True,
    )
    return repo


class RouteAllCliTest(unittest.TestCase):
    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(ROUTE_ALL), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--facts-args", proc.stdout)

    def test_end_to_end_merged_output(self):
        repo = _make_repo()
        self.addCleanup(_cleanup, repo)
        (repo / "src" / "B.py").write_text("y = 2\n", encoding="utf-8")
        out_path = repo / "merged.json"
        facts_path = repo / "facts.json"
        tasks_path = repo / "tasks.json"
        tasks_path.write_text(
            json.dumps(
                [
                    {
                        "TASK_ID": "T1",
                        "DELIVERABLE": "add module B",
                        "WHY_ONE_TASK": "single file",
                        "INDEPENDENT_ACCEPTANCE": "import works",
                        "WRITE_SET": ["src/B.py"],
                        "READ_ONLY": [],
                        "PREDECESSORS": [],
                        "SUCCESSORS": [],
                        "VERIFICATION_UNIT": "python -c import B",
                        "PARALLELISM": "SERIAL",
                    }
                ]
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(ROUTE_ALL),
                "--facts-args",
                f"--repo {repo}",
                "--route-args",
                f"--stage explore --facts {facts_path}",
                "--graph-args",
                f"--tasks {tasks_path}",
                "--facts-out",
                str(facts_path),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f"stderr={proc.stderr[:800]} stdout={proc.stdout[:400]}")
        merged = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertIn("change_facts", merged)
        self.assertIn("routing", merged)
        self.assertIn("task_graph", merged)


def _cleanup(repo: Path) -> None:
    import shutil

    shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
