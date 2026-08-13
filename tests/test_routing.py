"""Tests for the Context Router CLI (plan sections 25-26, 111-113)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
ROUTE_CONTEXT = SCRIPTS / "route_context.py"
CHANGE_FACTS = SCRIPTS / "change_facts.py"


def _write_json(data: dict) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-router-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return Path(name)


def _write_text(text: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-facts-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return Path(name)


def run_router(
    stage: str,
    facts: dict | None = None,
    task: dict | None = None,
    ledger: dict | None = None,
    skill_root: Path | None = None,
):
    args = [sys.executable, str(ROUTE_CONTEXT), "--stage", stage]
    if skill_root is not None:
        args += ["--skill-root", str(skill_root)]
    paths: list[Path] = []
    if facts is not None:
        path = _write_json(facts)
        paths.append(path)
        args += ["--facts", str(path)]
    if task is not None:
        path = _write_json(task)
        paths.append(path)
        args += ["--task-facts", str(path)]
    if ledger is not None:
        path = _write_json(ledger)
        paths.append(path)
        args += ["--ledger", str(path)]
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


def base_facts(**overrides: object) -> dict:
    facts: dict = {
        "repo_root": "/tmp/repo",
        "base": "abc123",
        "head": "WORKTREE",
        "changed_files": [],
        "untracked_files": [],
        "changed_file_classes": {},
        "changed_languages": [],
        "modules": [],
        "tests_changed": False,
        "dependency_manifest_changed": False,
        "lockfile_changed": False,
        "migration_changed": False,
        "generated_file_candidates": [],
        "write_set_overlap": {"state": "unknown", "task_count": 0, "pairs": []},
        "diff_ranges": {},
    }
    for key in (
        "transaction_candidate",
        "public_api_candidate",
        "security_candidate",
        "concurrency_candidate",
        "external_side_effect_candidate",
    ):
        facts[key] = {"state": "not_detected", "evidence": []}
    facts.update(overrides)
    return facts


def _overlap(state: str, task_count: int, pairs: list) -> dict:
    return base_facts(
        write_set_overlap={
            "state": state,
            "task_count": task_count,
            "pairs": pairs,
        }
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def make_repo(base_files: dict[str, str], untracked: dict[str, str] | None = None) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="crp-router-repo-"))
    for rel, content in base_files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if _git(repo, "init", "-q").returncode != 0:
        raise RuntimeError("git init failed")
    if _git(repo, "add", "-A").returncode != 0:
        raise RuntimeError("git add failed")
    if (
        _git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "base",
        ).returncode
        != 0
    ):
        raise RuntimeError("git commit failed")
    for rel, content in (untracked or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


class RoutingTest(unittest.TestCase):
    def test_simple_tool_heavy_no_user_decision(self) -> None:
        facts = base_facts(
            changed_files=["src/main/java/App.java"],
            changed_file_classes={"production source": ["src/main/java/App.java"]},
            changed_languages=["Java"],
        )
        task = {
            "files_read": 12,
            "tool_count": 8,
            "skill_count": 3,
            "test_count": 5,
            "needs_graph_evidence": True,
            "needs_text_search": True,
            "root_cause": "not_established",
            "testable": True,
            "behavior_count": 1,
            "write_sets": [["src/main/java/App.java"]],
        }
        code, out, err = run_router("execute", facts=facts, task=task)
        self.assertEqual(code, 0, err)
        assert out is not None
        reasons = out["reasons"]
        self.assertEqual(reasons["g1_user_decision"], "NONE")
        self.assertIn("files_read", reasons["g1_non_triggers"])
        self.assertIn("tool_count", reasons["g1_non_triggers"])
        self.assertEqual(reasons["g3_task_count"], "single")
        self.assertEqual(reasons["g4_execution"], "single")
        self.assertEqual(reasons["g2_risk"], "NORMAL")
        self.assertFalse(reasons["advisor_candidate"])
        self.assertIn("systematic-debugging", out["skills"])
        self.assertIn("test-driven-development", out["skills"])
        self.assertIn("search-gates", out["skills"])
        self.assertIn("ponytail", out["skills"])
        self.assertIn("codegraph explore", out["tools"])
        self.assertIn("rg", out["tools"])
        self.assertNotIn("grill-with-docs", out["skills"])
        self.assertNotIn("request_user_input", out["tools"])

    def test_high_risk_no_user_decision(self) -> None:
        facts = base_facts(
            migration_changed=True,
            changed_files=["db/migration/V2__fix.sql", "src/main/java/Fix.java"],
            changed_file_classes={
                "migration": ["db/migration/V2__fix.sql"],
                "production source": ["src/main/java/Fix.java"],
            },
            transaction_candidate={"state": "candidate", "evidence": [{"file": "src/main/java/Fix.java", "line": 1, "match": "@Transactional"}]},
        )
        task = {
            "risk": "HIGH",
            "root_cause": "established",
            "known_transaction_bug": True,
            "testable": True,
            "write_sets": [["db/migration/V2__fix.sql"]],
        }
        code, out, err = run_router("execute", facts=facts, task=task)
        self.assertEqual(code, 0, err)
        assert out is not None
        reasons = out["reasons"]
        self.assertEqual(reasons["g2_risk"], "HIGH")
        self.assertEqual(reasons["g2_advisor"], "required")
        self.assertTrue(reasons["advisor_candidate"])
        self.assertEqual(reasons["g1_user_decision"], "NONE")
        self.assertEqual(reasons["grill"], "not_required")
        self.assertNotIn("grill-with-docs", out["skills"])
        self.assertNotIn("request_user_input", out["tools"])
        self.assertIn("references/task-contracts.md", out["references"])
        self.assertIn("test-driven-development", out["skills"])

    def test_candidate_is_not_confirmed_high(self) -> None:
        facts = base_facts(
            changed_files=["src/main/java/Risk.java"],
            changed_file_classes={"production source": ["src/main/java/Risk.java"]},
            transaction_candidate={"state": "candidate", "evidence": [{"file": "src/main/java/Risk.java", "line": 1, "match": "@Transactional"}]},
        )
        code, out, err = run_router("execute", facts=facts, task={"testable": True, "write_sets": [["src/main/java/Risk.java"]]})
        self.assertEqual(code, 0, err)
        assert out is not None
        reasons = out["reasons"]
        self.assertEqual(reasons["g2_risk"], "ELEVATED")
        self.assertNotEqual(reasons["g2_risk"], "HIGH")
        self.assertTrue(reasons["advisor_candidate"])
        self.assertEqual(reasons["g2_advisor"], "not_required")

    def test_confirmed_fact_is_high(self) -> None:
        facts = base_facts(
            changed_files=["src/main/java/Risk.java"],
            changed_file_classes={"production source": ["src/main/java/Risk.java"]},
            transaction_candidate={"state": "confirmed", "evidence": [{"file": "src/main/java/Risk.java", "line": 1, "match": "@Transactional"}]},
        )
        code, out, err = run_router("execute", facts=facts, task={"write_sets": [["src/main/java/Risk.java"]]})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g2_risk"], "HIGH")

    def test_genuine_ambiguity_requires_user_decision(self) -> None:
        facts = base_facts(
            changed_files=["api/user.proto"],
            changed_file_classes={"contract/interface candidate": ["api/user.proto"]},
        )
        task = {
            "genuine_ambiguity": True,
            "ambiguous_decision": "api_behavior",
            "behavior_count": 1,
        }
        code, out, err = run_router("plan", facts=facts, task=task)
        self.assertEqual(code, 0, err)
        assert out is not None
        reasons = out["reasons"]
        self.assertEqual(reasons["g1_user_decision"], "REQUIRES_USER_DECISION")
        self.assertEqual(reasons["grill"], "required")
        self.assertIn("grill-with-docs", out["skills"])
        self.assertIn("request_user_input", out["tools"])

    def test_recovery_gate_loads_recovery_reference(self) -> None:
        facts = base_facts()
        ledger = {"incomplete_ledger": True, "running_agent": False}
        code, out, err = run_router("resume", facts=facts, ledger=ledger)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g5_recovery"], "required")
        self.assertIn("references/recovery-and-failures.md", out["references"])

        code, out, err = run_router("execute", facts=facts, task={})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g5_recovery"], "none")
        self.assertNotIn("references/recovery-and-failures.md", out["references"])

    def test_execution_modes(self) -> None:
        disjoint = base_facts(
            changed_files=["a/One.java", "b/Two.java"],
            changed_file_classes={"production source": ["a/One.java", "b/Two.java"]},
        )
        code, out, err = run_router("decompose", facts=disjoint, task={"write_sets": [["a/One.java"], ["b/Two.java"]]})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "parallel-safe")

        overlapping = base_facts(
            changed_files=["a/One.java", "a/Two.java"],
            changed_file_classes={"production source": ["a/One.java", "a/Two.java"]},
            write_set_overlap={"state": "confirmed", "task_count": 2, "pairs": [{"task_a": "t1", "task_b": "t2", "intersection": ["a/Two.java"]}]},
        )
        code, out, err = run_router(
            "decompose",
            facts=overlapping,
            task={"write_sets": [["a/One.java", "a/Two.java"], ["a/Two.java"]]},
        )
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

        code, out, err = run_router("decompose", facts=disjoint, task={})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

        code, out, err = run_router(
            "decompose",
            facts=disjoint,
            task={"write_sets": [["a/One.java"], ["b/Two.java"]], "dependencies": ["t2 after t1"]},
        )
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

    def test_empty_facts_missing_required_keys(self) -> None:
        code, out, err = run_router("execute", facts={}, task={})
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")
        self.assertIn("missing", err["error"])

    def test_unknown_keys_rejected(self) -> None:
        code, out, err = run_router(
            "execute",
            facts=base_facts(extra_unknown=1),
            task={},
        )
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")
        self.assertIn("extra_unknown", err["error"]["unknown"])

        code, out, err = run_router(
            "execute",
            facts=base_facts(),
            task={"mystery_key": True},
        )
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        code, out, err = run_router(
            "execute",
            facts=base_facts(),
            ledger={"mystery_key": True},
        )
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_undecodable_facts_exits_2(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-router-bad-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b'{"changed_files": ["\xff\xfe"]}')
        path = Path(name)
        try:
            proc = subprocess.run(
                [sys.executable, str(ROUTE_CONTEXT), "--stage", "plan", "--facts", str(path)],
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

    def test_nested_schema_errors_are_invalid_input(self) -> None:
        bad_evidence = base_facts(
            security_candidate={"state": "candidate", "evidence": [{"file": 1}]}
        )
        code, out, err = run_router("execute", facts=bad_evidence, task={})
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_write_set_overlap_closed_schema(self) -> None:
        cases = [
            base_facts(
                write_set_overlap={
                    "state": "unknown",
                    "task_count": 0,
                    "pairs": [],
                    "extra": 1,
                }
            ),
            base_facts(write_set_overlap={"state": "unknown", "task_count": 0}),
            base_facts(
                write_set_overlap={
                    "state": "unknown",
                    "task_count": 0,
                    "pairs": [{"task_a": "a", "task_b": "b", "intersection": ["x"], "extra": 1}],
                }
            ),
            base_facts(
                write_set_overlap={
                    "state": "unknown",
                    "task_count": 0,
                    "pairs": [{"task_a": "a", "intersection": ["x"]}],
                }
            ),
        ]
        for facts in cases:
            code, out, err = run_router("execute", facts=facts, task={})
            self.assertEqual(code, 2, err)
            self.assertIsNone(out)
            assert err is not None
            self.assertEqual(err["error"]["code"], "invalid_input")

        code, out, err = run_router("execute", facts=base_facts(), task={})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

    def test_write_set_overlap_state_semantics(self) -> None:
        invalid_cases = [
            _overlap("candidate", 0, []),
            _overlap("unknown", 1, []),
            _overlap(
                "unknown",
                0,
                [{"task_a": "a", "task_b": "b", "intersection": ["x"]}],
            ),
            _overlap("not_detected", 0, []),
            _overlap(
                "not_detected",
                2,
                [{"task_a": "a", "task_b": "b", "intersection": ["x"]}],
            ),
            _overlap(
                "confirmed",
                1,
                [{"task_a": "a", "task_b": "b", "intersection": ["x"]}],
            ),
            _overlap("confirmed", 2, []),
            _overlap("unknown", True, []),
            _overlap("unknown", -1, []),
            _overlap(
                "confirmed",
                2,
                [{"task_a": "a", "task_b": "a", "intersection": ["x"]}],
            ),
            _overlap(
                "confirmed",
                2,
                [{"task_a": " ", "task_b": "b", "intersection": ["x"]}],
            ),
            _overlap(
                "confirmed",
                2,
                [{"task_a": "b", "task_b": "a", "intersection": ["x"]}],
            ),
            _overlap(
                "confirmed",
                2,
                [{"task_a": "a", "task_b": "b", "intersection": []}],
            ),
            _overlap(
                "confirmed",
                2,
                [{"task_a": "a", "task_b": "b", "intersection": ["x", " "]}],
            ),
            _overlap(
                "confirmed",
                2,
                [{"task_a": "a", "task_b": "b", "intersection": ["x", "x"]}],
            ),
            _overlap(
                "confirmed",
                3,
                [
                    {"task_a": "a", "task_b": "b", "intersection": ["x"]},
                    {"task_a": "a", "task_b": "b", "intersection": ["y"]},
                ],
            ),
            _overlap(
                "confirmed",
                3,
                [
                    {"task_a": "b", "task_b": "c", "intersection": ["x"]},
                    {"task_a": "a", "task_b": "b", "intersection": ["y"]},
                ],
            ),
            _overlap(
                "confirmed",
                2,
                [
                    {"task_a": "a", "task_b": "b", "intersection": ["x"]},
                    {"task_a": "a", "task_b": "c", "intersection": ["y"]},
                ],
            ),
        ]
        for facts in invalid_cases:
            code, out, err = run_router("execute", facts=facts, task={})
            self.assertEqual(code, 2, err)
            self.assertIsNone(out)
            assert err is not None
            self.assertEqual(err["error"]["code"], "invalid_input")

    def test_write_set_overlap_valid_state_semantics(self) -> None:
        valid_cases = [
            base_facts(),
            _overlap("not_detected", 2, []),
            _overlap(
                "confirmed",
                2,
                [{"task_a": "a", "task_b": "b", "intersection": ["x"]}],
            ),
            _overlap(
                "confirmed",
                3,
                [
                    {"task_a": "a", "task_b": "b", "intersection": ["x"]},
                    {"task_a": "a", "task_b": "c", "intersection": ["y"]},
                ],
            ),
        ]
        for facts in valid_cases:
            code, out, err = run_router("execute", facts=facts, task={})
            self.assertEqual(code, 0, err)

        bad_ranges = base_facts(diff_ranges={"a.java": [{"start": "x"}]})
        code, out, err = run_router("execute", facts=bad_ranges, task={})
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        bad_overlap = base_facts(write_set_overlap={"state": "nope", "task_count": 0, "pairs": []})
        code, out, err = run_router("execute", facts=bad_overlap, task={})
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        code, out, err = run_router(
            "execute",
            facts=base_facts(),
            task={"write_sets": [["a.java", ""]]},
        )
        self.assertEqual(code, 2)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_ledger_nested_type_errors(self) -> None:
        code, out, err = run_router(
            "resume",
            facts=base_facts(),
            ledger={"incomplete_ledger": "yes"},
        )
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_schema_version_allowed(self) -> None:
        code, out, err = run_router(
            "execute",
            facts=base_facts(schema_version=1),
            task={"schema_version": "1"},
        )
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

    def test_advisor_candidate_dependency_only_false(self) -> None:
        facts = base_facts(
            dependency_manifest_changed=True,
            lockfile_changed=True,
            changed_files=["pom.xml", "package-lock.json"],
            changed_file_classes={
                "dependency manifest": ["pom.xml"],
                "lockfile": ["package-lock.json"],
            },
        )
        code, out, err = run_router("execute", facts=facts, task={"write_sets": [["pom.xml"]]})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g2_risk"], "ELEVATED")
        self.assertFalse(out["reasons"]["advisor_candidate"])
        self.assertEqual(out["reasons"]["g2_advisor"], "not_required")

    def test_argparse_usage_error_json(self) -> None:
        proc = subprocess.run([sys.executable, str(ROUTE_CONTEXT)], capture_output=True)
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr.decode("utf-8"))
        self.assertEqual(error["error"]["code"], "invalid_input")
        self.assertIn("invalid arguments", error["error"]["message"])

        proc = subprocess.run(
            [sys.executable, str(ROUTE_CONTEXT), "--stage", "execute", "--bogus"],
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr.decode("utf-8"))
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_missing_reference_is_structured_error(self) -> None:
        empty_root = Path(tempfile.mkdtemp(prefix="crp-skill-root-"))
        code, out, err = run_router("execute", facts=base_facts(), task={}, skill_root=empty_root)
        self.assertEqual(code, 3)
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "missing_reference")
        self.assertIn("references/routing-gates.md", err["error"]["missing"])

    def test_nested_type_errors_are_invalid_input(self) -> None:
        code, out, err = run_router(
            "execute",
            facts=base_facts(),
            task={"write_sets": "oops"},
        )
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        code, out, err = run_router(
            "execute",
            facts=base_facts(changed_files=5),
            task={},
        )
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        code, out, err = run_router(
            "execute",
            facts=base_facts(),
            task={"risk": "LOW"},
        )
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        code, out, err = run_router(
            "execute",
            facts=base_facts(),
            task={"write_sets": [[]]},
        )
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_utf8_bytes_router(self) -> None:
        task = _write_json(
            {
                "genuine_ambiguity": True,
                "ambiguous_decision": "接口行为",
                "behavior_count": 1,
            }
        )
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROUTE_CONTEXT),
                    "--stage",
                    "plan",
                    "--task-facts",
                    str(task),
                ],
                capture_output=True,
            )
        finally:
            task.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("utf-8")  # strict: proves UTF-8 bytes on stdout
        out = json.loads(text)
        self.assertIn("grill-with-docs", out["skills"])

    def test_change_facts_to_route_context_integration(self) -> None:
        repo = make_repo(
            {"README.md": "r\n"},
            untracked={"a.java": "class A {}\n", "b.java": "class B {}\n"},
        )
        overlapping = _write_json(
            {
                "tasks": [
                    {"id": "t1", "files": ["a.java", "b.java"]},
                    {"id": "t2", "files": ["b.java"]},
                ]
            }
        )
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(CHANGE_FACTS),
                    "--repo",
                    str(repo),
                    "--base",
                    "HEAD",
                    "--write-sets",
                    str(overlapping),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        finally:
            overlapping.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        facts = json.loads(proc.stdout)
        self.assertEqual(facts["write_set_overlap"]["state"], "confirmed")
        facts_path = _write_text(proc.stdout)
        try:
            code, out, err = run_router(
                "decompose",
                facts=facts,
                task={"write_sets": [["a.java", "b.java"], ["b.java"]]},
            )
        finally:
            facts_path.unlink(missing_ok=True)
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

        code, out, err = run_router("decompose", facts=facts, task={})
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "serial")

        disjoint = _write_json(
            {"tasks": [{"id": "t1", "files": ["a.java"]}, {"id": "t2", "files": ["b.java"]}]}
        )
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(CHANGE_FACTS),
                    "--repo",
                    str(repo),
                    "--base",
                    "HEAD",
                    "--write-sets",
                    str(disjoint),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        finally:
            disjoint.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        facts2 = json.loads(proc.stdout)
        self.assertEqual(facts2["write_set_overlap"]["state"], "not_detected")
        code, out, err = run_router(
            "decompose",
            facts=facts2,
            task={"write_sets": [["a.java"], ["b.java"]]},
        )
        self.assertEqual(code, 0, err)
        assert out is not None
        self.assertEqual(out["reasons"]["g4_execution"], "parallel-safe")


if __name__ == "__main__":
    unittest.main()
