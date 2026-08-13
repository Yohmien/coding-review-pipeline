"""Deterministic tests for eval_harness prepare/score.

These tests simulate reports and workspace changes only. They never invoke
an LLM and do NOT constitute an agent evaluation; real behavior evaluation
runs separately with fresh weak executors.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "contract-executor" / "scripts"
EVAL_HARNESS = SCRIPTS / "eval_harness.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "skills" / "contract-executor" / "test-prompts.json"

VERIFY_COMMAND = "python -B -m unittest tests.test_app"


def run_harness(*args: str) -> tuple[int, dict | None, dict | None]:
    proc = subprocess.run(
        [sys.executable, str(EVAL_HARNESS), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    err = None
    if proc.stderr.strip():
        try:
            err = json.loads(proc.stderr)
        except json.JSONDecodeError:
            err = {"error": {"code": "unparseable", "message": proc.stderr.strip()}}
    return proc.returncode, out, err


def make_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix="crp-eval-test-")) / "ws"


def git(ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ws), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def prepare(case: str, manifest: Path | None = None) -> tuple[Path, Path]:
    ws = make_workspace()
    state = ws.parent / "state.json"
    args = ["prepare", "--case", case, "--workspace", str(ws), "--state", str(state)]
    if manifest is not None:
        args += ["--manifest", str(manifest)]
    code, _, err = run_harness(*args)
    if code != 0:
        raise RuntimeError(f"prepare failed: {err}")
    return ws, state


def score(
    ws: Path,
    state: Path,
    case: str,
    report: dict | Path,
    manifest: Path | None = None,
) -> tuple[int, dict | None, dict | None]:
    if isinstance(report, dict):
        report = write_report(ws, report)
    args = [
        "score",
        "--case",
        case,
        "--workspace",
        str(ws),
        "--state",
        str(state),
        "--report",
        str(report),
    ]
    if manifest is not None:
        args += ["--manifest", str(manifest)]
    return run_harness(*args)


def write_manifest(mutate) -> Path:
    data = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    mutate(data)
    target = Path(tempfile.mkdtemp(prefix="crp-manifest-")) / "manifest.json"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


def report_json(
    status: str = "completed",
    verified: list[dict] | None = None,
    extra: dict | None = None,
    omit: tuple[str, ...] = (),
    gaps: list[dict] | None = None,
) -> dict:
    report = {
        "STATUS": status,
        "CHANGES": [{"path": "src/app.py", "summary": "implement add"}] if status == "completed" else [],
        "VERIFIED": verified
        if verified is not None
        else ([{"command": VERIFY_COMMAND, "exit_code": 0, "failure_count": 0}] if status == "completed" else []),
        "JUDGMENT CALLS": [],
        "GAPS": gaps
        if gaps is not None
        else (
            []
            if status == "completed"
            else [{"kind": "blocked", "decision": "blocked scenario", "evidence": "out of scope"}]
        ),
    }
    for key in omit:
        report.pop(key, None)
    if extra:
        report.update(extra)
    return report


def write_report(ws: Path, report: dict) -> Path:
    target = ws.parent / "agent_report.json"
    target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return target


class EvalHarnessPrepareTest(unittest.TestCase):
    def test_prepare_creates_fixture_git_baseline_and_external_state(self) -> None:
        ws, state = prepare("mechanical")
        self.assertTrue((ws / "src" / "app.py").exists())
        self.assertTrue((ws / "tests" / "test_app.py").exists())
        self.assertTrue((ws / "tests" / "__init__.py").exists())
        for name in ("packet.json", "expectation.json", "agent_prompt.txt", "baseline.json", ".crp-eval-workspace"):
            self.assertTrue((ws / name).exists())
        head = git(ws, "rev-parse", "HEAD")
        self.assertEqual(head.returncode, 0)
        self.assertTrue(head.stdout.strip())
        status = git(ws, "status", "--porcelain")
        self.assertEqual(status.stdout.strip(), "")
        self.assertTrue(state.exists())
        self.assertFalse(state.resolve().is_relative_to(ws.resolve()))
        state_data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(state_data["case"], "mechanical")
        self.assertTrue(state_data["baseline_commit"])
        self.assertTrue(state_data["baseline_tree"])
        self.assertIn("src/app.py", state_data["file_hashes"])
        packet = json.loads((ws / "packet.json").read_text(encoding="utf-8"))
        self.assertEqual(packet["TASK_ID"], "T1")
        self.assertEqual(packet["WRITE_SET"], ["src/app.py"])

    def test_prepare_rejects_non_empty_workspace(self) -> None:
        ws = make_workspace()
        ws.mkdir(parents=True)
        (ws / "junk.txt").write_text("x", encoding="utf-8")
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json")
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_repo_workspace(self) -> None:
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(REPO_ROOT), "--state", str(REPO_ROOT / "state.json")
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_state_inside_workspace(self) -> None:
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws / "state.json")
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_unknown_case_invalid_input(self) -> None:
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "nope", "--workspace", str(ws), "--state", str(ws.parent / "state.json")
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_traversal_setup_path(self) -> None:
        manifest = write_manifest(
            lambda data: data["fixtures"][0]["setup_files"].update({"../escape.txt": "x"})
        )
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json"),
            "--manifest", str(manifest),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_ads_setup_path(self) -> None:
        manifest = write_manifest(
            lambda data: data["fixtures"][0]["setup_files"].update({"src/a:b.txt": "x"})
        )
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json"),
            "--manifest", str(manifest),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_device_setup_path(self) -> None:
        manifest = write_manifest(
            lambda data: data["fixtures"][0]["setup_files"].update({"src/CON.txt": "x"})
        )
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json"),
            "--manifest", str(manifest),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_duplicate_fixture_ids(self) -> None:
        manifest = write_manifest(lambda data: data["fixtures"].append(dict(data["fixtures"][0])))
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json"),
            "--manifest", str(manifest),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_deep_invalid_manifest(self) -> None:
        def drop_packet_field(data: dict) -> None:
            del data["fixtures"][0]["packet"]["WRITE_SET"]

        manifest = write_manifest(drop_packet_field)
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json"),
            "--manifest", str(manifest),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_undecodable_manifest_exits_2(self) -> None:
        manifest = Path(tempfile.mkdtemp(prefix="crp-manifest-bad-")) / "manifest.json"
        manifest.write_bytes(b'{"fixtures": [{"id": "\xff"}]}')
        ws = make_workspace()
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(ws), "--state", str(ws.parent / "state.json"),
            "--manifest", str(manifest),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_prepare_rejects_junction_workspace(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="crp-eval-junction-target-"))
        (target / "junk.txt").write_text("x", encoding="utf-8")
        link = Path(tempfile.mkdtemp(prefix="crp-eval-junction-")) / "link"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if created.returncode != 0:
            self.skipTest("junction creation not available")
        code, _, err = run_harness(
            "prepare", "--case", "mechanical", "--workspace", str(link), "--state", str(link.parent / "state.json")
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")


class EvalHarnessScoreTest(unittest.TestCase):
    def test_score_passes_completed_mechanical(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        code, out, err = score(ws, state, "mechanical", report_json())
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["verdict"], "PASS")
        self.assertEqual(out["independent_verification"]["exit_code"], 0)

    def test_score_fails_wrong_status(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        code, out, _ = score(ws, state, "mechanical", report_json(status="blocked"))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_missing_report_field(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        code, out, _ = score(ws, state, "mechanical", report_json(omit=("GAPS",)))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_extra_report_field(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        code, out, _ = score(ws, state, "mechanical", report_json(extra={"EXTRA": 1}))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_out_of_write_set_change(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (ws / "other.txt").write_text("x", encoding="utf-8")
        code, out, _ = score(ws, state, "mechanical", report_json())
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_verification_exit_mismatch(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        report = report_json(
            verified=[{"command": VERIFY_COMMAND, "exit_code": 1, "failure_count": 1}]
        )
        code, out, _ = score(ws, state, "mechanical", report)
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_fake_report_verification(self) -> None:
        ws, state = prepare("mechanical")
        report = report_json()
        code, out, _ = score(ws, state, "mechanical", report)
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")
        independent = next(
            check for check in out["checks"] if check["name"] == "independent_verification_exit"
        )
        self.assertFalse(independent["pass"])

    def test_score_fails_unparseable_report(self) -> None:
        ws, state = prepare("mechanical")
        bad = ws.parent / "agent_report.json"
        bad.write_text("not json", encoding="utf-8")
        code, out, _ = score(ws, state, "mechanical", bad)
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_undecodable_report_exits_2(self) -> None:
        ws, state = prepare("mechanical")
        bad = ws.parent / "agent_report.json"
        bad.write_bytes(b'{"STATUS": "\xff"}')
        code, out, err = score(ws, state, "mechanical", bad)
        self.assertEqual(code, 2, err)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_score_passes_blocked_clean_workspace(self) -> None:
        ws, state = prepare("new_dependency")
        code, out, err = score(ws, state, "new_dependency", report_json(status="blocked"))
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["verdict"], "PASS")

    def test_score_fails_blocked_with_workspace_changes(self) -> None:
        ws, state = prepare("new_dependency")
        (ws / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
        code, out, _ = score(ws, state, "new_dependency", report_json(status="blocked"))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_blocked_with_cache(self) -> None:
        ws, state = prepare("new_dependency")
        (ws / "src" / "__pycache__").mkdir(parents=True)
        (ws / "src" / "__pycache__" / "app.cpython-314.pyc").write_bytes(b"x")
        code, out, _ = score(ws, state, "new_dependency", report_json(status="blocked"))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_fails_blocked_with_gitignore_hidden_file(self) -> None:
        ws, state = prepare("new_dependency")
        (ws / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
        (ws / "secret.txt").write_text("hidden", encoding="utf-8")
        code, out, _ = score(ws, state, "new_dependency", report_json(status="blocked"))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_detects_committed_change_in_blocked_case(self) -> None:
        ws, state = prepare("new_dependency")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
        git(ws, "add", "-A")
        git(ws, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "bypass")
        code, out, _ = score(ws, state, "new_dependency", report_json(status="blocked"))
        self.assertEqual(code, 1)
        assert out is not None
        self.assertEqual(out["verdict"], "FAIL")

    def test_score_passes_committed_change_in_mechanical_case(self) -> None:
        ws, state = prepare("mechanical")
        (ws / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        git(ws, "add", "-A")
        git(ws, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "implement")
        code, out, err = score(ws, state, "mechanical", report_json())
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["verdict"], "PASS")
        self.assertEqual(out["changed_files"], ["src/app.py"])

    def test_score_rejects_unprepared_workspace(self) -> None:
        ws = make_workspace()
        ws.mkdir(parents=True)
        state = ws.parent / "state.json"
        code, _, err = run_harness(
            "score",
            "--case",
            "mechanical",
            "--workspace",
            str(ws),
            "--state",
            str(state),
            "--report",
            str(ws.parent / "agent_report.json"),
        )
        self.assertEqual(code, 2, err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
