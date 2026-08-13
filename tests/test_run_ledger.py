# -*- coding: utf-8 -*-
"""Phase 9-10 Persistent Run Ledger + Verification Router tests.

Coverage (execution plan section 120):
init, atomic update, UTF-8, Chinese paths, task owner, coder reuse,
review round, diff fingerprint, verification freshness, ship anti-redispatch,
fix-first recovery, running agent recovery, corrupt JSON, multiple runs,
plan mismatch, NON_GIT. Plus the Verification Router tier decision
(execution plan section 70).

All repository-backed tests use a temporary git repository; the real
repository is never touched. NON_GIT tests use a temporary directory and a
temporary CODEX_HOME (never TEMP).
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "coding-review-pipeline" / "scripts"

sys.path.insert(0, str(SCRIPTS))
import crp_common  # noqa: E402
import run_ledger  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def make_repo(files=None):
    """Create a temporary git repo with one committed baseline commit."""
    repo = Path(tempfile.mkdtemp(prefix="crp-ledger-repo-"))
    for rel, content in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
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
    )
    return repo


def _plan(**overrides):
    plan = {
        "objective": "fix login bug",
        "tasks": ["T1"],
        "dependencies": [],
        "interfaces": ["auth"],
        "constraints": ["no new deps"],
        "acceptance": ["login works"],
        "decisions": [],
    }
    plan.update(overrides)
    return plan


def _task(**overrides):
    task = {
        "task_id": "T1",
        "deliverable": "fix",
        "write_set": ["src/A.java"],
        "read_only": [],
        "dependencies": [],
        "state": "in_progress",
        "owner_coder": "coder-1",
        "reuse_policy": "sticky",
        "actual_changed_files": ["src/A.java"],
        "verification": [],
        "review_round": 0,
        "latest_verdict": "",
        "verdict_diff_fingerprint": "",
        "pending_fix": False,
        "pending_audit": False,
    }
    task.update(overrides)
    return task


def _agent(**overrides):
    agent = {
        "agent_id": "coder-1",
        "role": "coder",
        "task_id": "T1",
        "decision_id": "",
        "reuse_policy": "sticky",
        "lifecycle_state": "ACTIVE",
        "close_eligible": False,
        "last_observed_runtime_state": "running",
        "pending_fix": False,
        "pending_review": False,
    }
    agent.update(overrides)
    return agent


class TestLedgerInit(unittest.TestCase):
    def test_new_ledger_has_required_structure(self):
        ledger = run_ledger.new_ledger("r1", "/repo/root", plan=_plan())
        expected_keys = {
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
        }
        self.assertEqual(ledger["schema_version"], 1)
        self.assertEqual(set(ledger), expected_keys)
        self.assertEqual(ledger["run_id"], "r1")
        self.assertEqual(ledger["repo_root"], "/repo/root")
        self.assertEqual(ledger["plan"], _plan())
        self.assertIsInstance(ledger["tasks"], dict)
        self.assertIsInstance(ledger["agents"], dict)
        self.assertIsInstance(ledger["events"], list)

    def test_new_ledger_stores_plan_fingerprint(self):
        plan = _plan()
        ledger = run_ledger.new_ledger("r1", "/repo", plan=plan)
        self.assertEqual(
            ledger["baseline"]["plan_fingerprint"],
            run_ledger.plan_fingerprint(plan),
        )

    def test_new_ledger_rejects_bad_run_id(self):
        for run_id in ("", "  ", "a/b", "a\\b", "..", "."):
            with self.subTest(run_id=run_id):
                with self.assertRaises(crp_common.CrpError):
                    run_ledger.new_ledger(run_id, "/repo")


class TestRunIdValidation(unittest.TestCase):
    def test_run_id_over_64_chars_rejected(self):
        for run_id in ("x" * 65, "x" * 128):
            with self.subTest(run_id=run_id):
                with self.assertRaises(crp_common.CrpError) as context:
                    run_ledger.new_ledger(run_id, "/repo")
                self.assertEqual(context.exception.code, "invalid_input")

    def test_run_id_exactly_64_chars_allowed(self):
        run_id = "r" * 64
        ledger = run_ledger.new_ledger(run_id, "/repo")
        self.assertEqual(ledger["run_id"], run_id)

    def test_windows_reserved_names_rejected(self):
        reserved = (
            "CON",
            "con",
            "PRN",
            "prn.txt",
            "AUX.log",
            "NUL",
            "nul.json",
            "COM1",
            "com3.tar.gz",
            "COM9",
            "LPT1",
            "lpt9.cmd",
        )
        for run_id in reserved:
            with self.subTest(run_id=run_id):
                with self.assertRaises(crp_common.CrpError) as context:
                    run_ledger.new_ledger(run_id, "/repo")
                self.assertEqual(context.exception.code, "invalid_input")

    def test_reserved_name_lookalikes_allowed(self):
        for run_id in ("CONSOLE", "COM10", "LPT0", "config", "console.txt"):
            with self.subTest(run_id=run_id):
                ledger = run_ledger.new_ledger(run_id, "/repo")
                self.assertEqual(ledger["run_id"], run_id)


class TestPlanTasksValidation(unittest.TestCase):
    def test_valid_forms_return_id_set(self):
        valid = (
            ["T1"],
            ["T1", "T2"],
            [{"TASK_ID": "T1"}],
            [{"task_id": "T1"}],
            [{"id": "T1"}],
            [{"TASK_ID": "", "task_id": "T1"}],
            {"T1": {}},
            {"T1": {"x": 1}, "T2": []},
        )
        for tasks in valid:
            with self.subTest(tasks=tasks):
                ids = run_ledger.validate_plan_tasks({"tasks": tasks}, require_tasks=True)
                self.assertIsInstance(ids, set)
                self.assertTrue(ids)

    def test_require_false_allows_missing_tasks(self):
        self.assertIsNone(
            run_ledger.validate_plan_tasks({"objective": "x"}, require_tasks=False)
        )

    def test_require_true_rejects_missing_tasks(self):
        with self.assertRaises(crp_common.CrpError) as context:
            run_ledger.validate_plan_tasks({"objective": "x"}, require_tasks=True)
        self.assertEqual(context.exception.code, "invalid_input")

    def test_bad_shapes_rejected_in_both_modes(self):
        bad = (
            "not-a-plan",
            {"tasks": None},
            {"tasks": []},
            {"tasks": {}},
            {"tasks": "T1"},
            {"tasks": ["T1", 123]},
            {"tasks": [{"foo": "T1"}]},
            {"tasks": [{"TASK_ID": 123}]},
            {"tasks": {"T1": {}, 1: {}}},
            {"tasks": {"": {}}},
        )
        for plan in bad:
            for require_tasks in (True, False):
                with self.subTest(plan=plan, require_tasks=require_tasks):
                    with self.assertRaises(crp_common.CrpError) as context:
                        run_ledger.validate_plan_tasks(plan, require_tasks=require_tasks)
                    self.assertEqual(context.exception.code, "invalid_input")


class TestLedgerPersistence(unittest.TestCase):
    def test_init_and_reload_round_trip(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger("r1", str(repo), plan=_plan())
        path = run_ledger.write_ledger(ledger, "r1", start=repo)
        self.assertTrue(path.is_file())
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["run_id"], "r1")
        self.assertEqual(reloaded["plan"], _plan())

    def test_atomic_update_merges_and_rewrites(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        updated = run_ledger.update_ledger(
            "r1",
            {"models": {"coder": "deepseek-coder"}, "tasks": {"T1": _task()}},
            start=repo,
        )
        self.assertEqual(updated["models"], {"coder": "deepseek-coder"})
        self.assertEqual(updated["tasks"]["T1"]["task_id"], "T1")
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["models"]["coder"], "deepseek-coder")
        self.assertEqual(reloaded["tasks"]["T1"]["owner_coder"], "coder-1")
        self.assertEqual(reloaded["plan"], _plan())
        self.assertIn("plan_fingerprint", reloaded["baseline"])

    def test_atomic_write_failure_keeps_old_ledger_valid(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger("r1", str(repo), plan=_plan())
        run_ledger.write_ledger(ledger, "r1", start=repo)
        path = run_ledger.ledger_path("r1", start=repo)
        original_bytes = path.read_bytes()

        with mock.patch.object(crp_common.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                run_ledger.update_ledger("r1", {"models": {"coder": "x"}}, start=repo)

        self.assertEqual(path.read_bytes(), original_bytes)
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["run_id"], "r1")
        self.assertEqual(reloaded["models"], {})

    def test_utf8_round_trip(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger(
            "r1",
            str(repo),
            plan=_plan(objective="修复登录：支持中文用户与 🚀 emoji"),
        )
        ledger["tasks"]["T1"] = _task(deliverable="中文交付物")
        run_ledger.write_ledger(ledger, "r1", start=repo)
        raw = run_ledger.ledger_path("r1", start=repo).read_bytes()
        text = raw.decode("utf-8")
        self.assertIn("中文", text)
        reloaded = json.loads(text)
        self.assertEqual(reloaded["plan"]["objective"], "修复登录：支持中文用户与 🚀 emoji")
        self.assertEqual(reloaded["tasks"]["T1"]["deliverable"], "中文交付物")

    def test_chinese_paths_round_trip(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger("r1", str(repo), plan=_plan())
        ledger["tasks"]["T1"] = _task(
            write_set=["src/主服务.java"],
            read_only=["docs/只读说明.md"],
            actual_changed_files=["src/主服务.java"],
        )
        run_ledger.write_ledger(ledger, "r1", start=repo)
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["tasks"]["T1"]["write_set"], ["src/主服务.java"])
        self.assertEqual(reloaded["tasks"]["T1"]["read_only"], ["docs/只读说明.md"])
        self.assertEqual(reloaded["tasks"]["T1"]["actual_changed_files"], ["src/主服务.java"])

    def test_corrupt_json_is_structured_error(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        path = run_ledger.ledger_path("r1", start=repo)
        path.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(crp_common.CrpError) as context:
            run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(context.exception.code, "invalid_input")
        self.assertIn("ledger", context.exception.message)

    def test_ledger_never_pollutes_git_status(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        status = _git(repo, "status", "--porcelain=v1")
        self.assertEqual(status.stdout.strip(), "")
        self.assertIn(".git", str(run_ledger.ledger_path("r1", start=repo)).replace("\\", "/"))

    def test_colon_run_id_rejected_and_creates_no_file(self):
        repo = make_repo({"README.md": "r\n"})
        with self.assertRaises(crp_common.CrpError) as context:
            run_ledger.ledger_path("C:foo", start=repo)
        self.assertEqual(context.exception.code, "invalid_input")
        runs = run_ledger.runs_dir(start=repo)
        self.assertFalse(runs.exists())
        self.assertFalse((runs / "C:foo").exists())

    def test_update_rejects_malformed_structured_fields(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        cases = [
            ("tasks", "not-a-dict"),
            ("agents", 3),
            ("decisions", ["x"]),
            ("integration", None),
            ("events", {"a": 1}),
        ]
        for field, bad in cases:
            with self.subTest(field=field):
                with self.assertRaises(crp_common.CrpError) as context:
                    run_ledger.update_ledger("r1", {field: bad}, start=repo)
                self.assertEqual(context.exception.code, "invalid_input")
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["tasks"], {})
        self.assertEqual(reloaded["agents"], {})
        self.assertEqual(reloaded["decisions"], {})
        self.assertEqual(reloaded["integration"], {})
        self.assertEqual(reloaded["events"], [])


class TestTaskAndAgentFields(unittest.TestCase):
    def test_task_owner_and_reuse_policy_recorded(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger("r1", str(repo), plan=_plan())
        ledger["tasks"]["T1"] = _task(owner_coder="coder-9", reuse_policy="sticky")
        run_ledger.write_ledger(ledger, "r1", start=repo)
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["tasks"]["T1"]["owner_coder"], "coder-9")
        self.assertEqual(reloaded["tasks"]["T1"]["reuse_policy"], "sticky")

    def test_review_round_and_verdict_tracked(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        run_ledger.update_ledger(
            "r1",
            {"tasks": {"T1": _task(review_round=1, latest_verdict="fix-first")}},
            start=repo,
        )
        run_ledger.update_ledger(
            "r1",
            {"tasks": {"T1": _task(review_round=2, latest_verdict="ship")}},
            start=repo,
        )
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["tasks"]["T1"]["review_round"], 2)
        self.assertEqual(reloaded["tasks"]["T1"]["latest_verdict"], "ship")


class TestDiffFingerprint(unittest.TestCase):
    def test_differs_on_file_set(self):
        a = run_ledger.diff_fingerprint(
            {"changed_files": ["a.py"], "untracked_files": [], "diff_ranges": {}}
        )
        b = run_ledger.diff_fingerprint(
            {"changed_files": ["b.py"], "untracked_files": [], "diff_ranges": {}}
        )
        self.assertNotEqual(a, b)

    def test_deterministic(self):
        facts = {
            "changed_files": ["a.py"],
            "untracked_files": ["c.txt"],
            "diff_ranges": {"a.py": [{"start": 1, "end": 3}]},
        }
        self.assertEqual(run_ledger.diff_fingerprint(facts), run_ledger.diff_fingerprint(dict(facts)))

    def test_sensitive_to_ranges(self):
        a = run_ledger.diff_fingerprint(
            {"changed_files": ["a.py"], "untracked_files": [], "diff_ranges": {"a.py": [{"start": 1, "end": 1}]}}
        )
        b = run_ledger.diff_fingerprint(
            {"changed_files": ["a.py"], "untracked_files": [], "diff_ranges": {"a.py": [{"start": 1, "end": 9}]}}
        )
        self.assertNotEqual(a, b)

    def test_ignores_volatile_fields(self):
        a = run_ledger.diff_fingerprint(
            {"changed_files": ["a.py"], "untracked_files": [], "diff_ranges": {}, "generated_at": "t1"}
        )
        b = run_ledger.diff_fingerprint(
            {"changed_files": ["a.py"], "untracked_files": [], "diff_ranges": {}, "generated_at": "t2"}
        )
        self.assertEqual(a, b)


class TestVerificationFreshness(unittest.TestCase):
    def test_valid_verification_requires_exit_code(self):
        self.assertTrue(
            run_ledger.valid_verification({"command": "pytest", "exit_code": 0, "failure_count": 0})
        )
        self.assertFalse(run_ledger.valid_verification({"command": "pytest", "failure_count": 0}))
        self.assertFalse(run_ledger.valid_verification({"command": "pytest", "exit_code": None}))
        self.assertFalse(run_ledger.valid_verification({"command": "pytest", "exit_code": "0"}))

    def test_verification_freshness_by_diff_fingerprint(self):
        rec = {
            "command": "pytest",
            "exit_code": 0,
            "failure_count": 0,
            "diff_fingerprint": "fp1",
        }
        self.assertTrue(run_ledger.is_verification_fresh(rec, "fp1"))
        self.assertFalse(run_ledger.is_verification_fresh(rec, "fp2"))
        self.assertFalse(
            run_ledger.is_verification_fresh({"command": "pytest", "exit_code": 0}, "fp1")
        )


class TestVerificationTier(unittest.TestCase):
    def _facts(self, **overrides):
        facts = {
            "changed_files": ["src/A.java"],
            "untracked_files": [],
            "modules": ["app"],
            "tests_changed": False,
            "dependency_manifest_changed": False,
            "lockfile_changed": False,
            "migration_changed": False,
            "changed_file_classes": {},
            "transaction_candidate": {"state": "not_detected", "evidence": []},
            "public_api_candidate": {"state": "not_detected", "evidence": []},
            "security_candidate": {"state": "not_detected", "evidence": []},
            "concurrency_candidate": {"state": "not_detected", "evidence": []},
            "external_side_effect_candidate": {"state": "not_detected", "evidence": []},
        }
        facts.update(overrides)
        return facts

    def test_full_on_high_risk_candidate(self):
        result = run_ledger.verification_tier(
            self._facts(transaction_candidate={"state": "candidate", "evidence": []})
        )
        self.assertEqual(result["tier"], "FULL")

    def test_full_on_explicit_task_risk(self):
        result = run_ledger.verification_tier(self._facts(), task_facts={"risk": "HIGH"})
        self.assertEqual(result["tier"], "FULL")

    def test_integration_on_migration(self):
        result = run_ledger.verification_tier(self._facts(migration_changed=True))
        self.assertEqual(result["tier"], "INTEGRATION")

    def test_integration_on_dependency_change(self):
        result = run_ledger.verification_tier(self._facts(dependency_manifest_changed=True))
        self.assertEqual(result["tier"], "INTEGRATION")

    def test_integration_on_contract_class(self):
        result = run_ledger.verification_tier(
            self._facts(changed_file_classes={"contract/interface candidate": ["api/user.proto"]})
        )
        self.assertEqual(result["tier"], "INTEGRATION")

    def test_integration_on_multiple_modules(self):
        result = run_ledger.verification_tier(self._facts(modules=["app", "lib"]))
        self.assertEqual(result["tier"], "INTEGRATION")

    def test_module_on_single_module(self):
        result = run_ledger.verification_tier(self._facts(modules=["app"]))
        self.assertEqual(result["tier"], "MODULE")

    def test_targeted_on_tests_changed(self):
        result = run_ledger.verification_tier(self._facts(modules=[], tests_changed=True))
        self.assertEqual(result["tier"], "TARGETED")

    def test_targeted_on_single_file_no_module(self):
        result = run_ledger.verification_tier(
            self._facts(modules=[], changed_files=["src/A.java"])
        )
        self.assertEqual(result["tier"], "TARGETED")

    def test_none_when_unprovable(self):
        self.assertIsNone(run_ledger.verification_tier({}))
        self.assertIsNone(
            run_ledger.verification_tier(
                self._facts(modules=[], tests_changed=False, changed_files=["a.py", "b.py"])
            )
        )

    def test_result_carries_reasons(self):
        result = run_ledger.verification_tier(self._facts(migration_changed=True))
        self.assertIsInstance(result["reasons"], list)
        self.assertTrue(result["reasons"])


class TestResume(unittest.TestCase):
    def _write_run(self, repo, run_id, tasks=None, agents=None, plan=None):
        ledger = run_ledger.new_ledger(
            run_id, str(repo), plan=plan or _plan(),
            baseline={"base": "HEAD", "stage": "execute"},
        )
        ledger["tasks"] = {task["task_id"]: task for task in (tasks or [])}
        ledger["agents"] = {agent["agent_id"]: agent for agent in (agents or [])}
        run_ledger.write_ledger(ledger, run_id, start=repo)
        return ledger

    def test_coder_reuse_fix_first_routes_to_owner(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(
            repo,
            "r1",
            tasks=[_task(latest_verdict="fix-first", pending_fix=True, owner_coder="coder-1")],
        )
        result = run_ledger.resume_state("r1", start=repo)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tasks"]["T1"]["resume_action"], "resume_same")
        self.assertEqual(result["tasks"]["T1"]["owner_coder"], "coder-1")

    def test_ship_anti_redispatch(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(
            repo,
            "r1",
            tasks=[_task(latest_verdict="ship", pending_fix=False, state="shipped")],
        )
        result = run_ledger.resume_state("r1", start=repo)
        self.assertEqual(result["tasks"]["T1"]["resume_action"], "no_redispatch")

    def test_fix_first_without_owner_is_blocked(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(
            repo,
            "r1",
            tasks=[_task(latest_verdict="fix-first", pending_fix=True, owner_coder="")],
        )
        result = run_ledger.resume_state("r1", start=repo)
        self.assertEqual(result["tasks"]["T1"]["resume_action"], "blocked_no_owner")

    def test_running_agent_recovery_queries_first(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(
            repo,
            "r1",
            tasks=[_task(state="in_progress", latest_verdict="")],
            agents=[_agent(agent_id="coder-1", task_id="T1", lifecycle_state="RUNNING")],
        )
        result = run_ledger.resume_state("r1", start=repo)
        self.assertEqual(len(result["running_agents"]), 1)
        self.assertEqual(result["running_agents"][0]["agent_id"], "coder-1")
        self.assertEqual(result["tasks"]["T1"]["resume_action"], "query_first")

    def test_malformed_agent_state_fields_fail_closed_query_first(self):
        repo = make_repo({"README.md": "r\n"})
        bad_values = (["RUNNING"], {"state": "RUNNING"}, 7, None)
        for field in ("lifecycle_state", "last_observed_runtime_state"):
            for bad in bad_values:
                with self.subTest(field=field, value=bad):
                    overrides = {field: bad}
                    if field == "lifecycle_state":
                        overrides["last_observed_runtime_state"] = "exited"
                    else:
                        overrides["lifecycle_state"] = "DONE"
                    self._write_run(
                        repo,
                        "r1",
                        tasks=[_task(state="in_progress", latest_verdict="")],
                        agents=[
                            _agent(
                                agent_id="coder-1",
                                task_id="T1",
                                **overrides,
                            )
                        ],
                    )
                    result = run_ledger.resume_state("r1", start=repo)
                    self.assertTrue(result["ok"])
                    self.assertEqual(len(result["running_agents"]), 1)
                    self.assertEqual(
                        result["tasks"]["T1"]["resume_action"], "query_first"
                    )

    def test_non_dict_agent_fail_closed_structured(self):
        repo = make_repo({"README.md": "r\n"})
        for bad in ("RUNNING", ["ACTIVE"], None):
            with self.subTest(value=bad):
                ledger = run_ledger.new_ledger(
                    "r1",
                    str(repo),
                    plan=_plan(),
                    baseline={"base": "HEAD", "stage": "execute"},
                )
                ledger["tasks"] = {"T1": _task(state="in_progress", latest_verdict="")}
                ledger["agents"] = {"coder-1": bad}
                run_ledger.write_ledger(ledger, "r1", start=repo)
                result = run_ledger.resume_state("r1", start=repo)
                self.assertTrue(result["ok"])
                self.assertEqual(len(result["running_agents"]), 1)
                self.assertEqual(result["running_agents"][0]["agent_id"], "coder-1")

    def test_terminal_agent_states_not_running(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(
            repo,
            "r1",
            tasks=[_task(state="in_progress", latest_verdict="")],
            agents=[
                _agent(
                    agent_id="coder-1",
                    task_id="T1",
                    lifecycle_state="DONE",
                    last_observed_runtime_state="exited",
                )
            ],
        )
        result = run_ledger.resume_state("r1", start=repo)
        self.assertEqual(result["running_agents"], [])
        self.assertEqual(result["tasks"]["T1"]["resume_action"], "continue")

    def test_running_agent_unhashable_task_id_stays_structured(self):
        repo = make_repo({"README.md": "r\n"})
        for bad in (["T1"], {"task": "T1"}):
            with self.subTest(value=bad):
                self._write_run(
                    repo,
                    "r1",
                    tasks=[_task(state="in_progress", latest_verdict="")],
                    agents=[
                        _agent(
                            agent_id="coder-1",
                            task_id=bad,
                            lifecycle_state="RUNNING",
                        )
                    ],
                )
                result = run_ledger.resume_state("r1", start=repo)
                self.assertTrue(result["ok"])
                self.assertEqual(len(result["running_agents"]), 1)
                self.assertEqual(result["running_agents"][0]["agent_id"], "coder-1")
                self.assertEqual(result["running_agents"][0]["task_id"], bad)

    def test_non_dict_task_records_structured_fail_closed(self):
        repo = make_repo({"README.md": "r\n"})
        for bad in ("RUNNING", ["x"], None):
            with self.subTest(value=bad):
                ledger = run_ledger.new_ledger(
                    "r1",
                    str(repo),
                    plan=_plan(),
                    baseline={"base": "HEAD", "stage": "execute"},
                )
                ledger["tasks"] = {
                    "T1": _task(state="in_progress", latest_verdict=""),
                    "T2": bad,
                }
                run_ledger.write_ledger(ledger, "r1", start=repo)
                result = run_ledger.resume_state("r1", start=repo)
                self.assertTrue(result["ok"])
                entry = result["tasks"]["T2"]
                for key in (
                    "state",
                    "latest_verdict",
                    "pending_fix",
                    "owner_coder",
                    "resume_action",
                ):
                    self.assertIn(key, entry)
                self.assertEqual(entry["resume_action"], "query_first")
                self.assertEqual(result["tasks"]["T1"]["resume_action"], "continue")

    def test_continue_for_ordinary_unfinished_task(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(
            repo,
            "r1",
            tasks=[_task(state="in_progress", latest_verdict="")],
        )
        result = run_ledger.resume_state("r1", start=repo)
        self.assertEqual(result["tasks"]["T1"]["resume_action"], "continue")

    def test_plan_mismatch_requires_reconfirmation(self):
        repo = make_repo({"README.md": "r\n"})
        self._write_run(repo, "r1", tasks=[_task()], plan=_plan(objective="original"))
        result = run_ledger.resume_state(
            "r1", start=repo, current_plan=_plan(objective="changed")
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["plan_reconfirmation_required"])

    def test_plan_match_continues(self):
        repo = make_repo({"README.md": "r\n"})
        plan = _plan(objective="same")
        self._write_run(repo, "r1", tasks=[_task()], plan=plan)
        result = run_ledger.resume_state("r1", start=repo, current_plan=plan)
        self.assertTrue(result["ok"])
        self.assertFalse(result["plan_reconfirmation_required"])


class TestMultipleRuns(unittest.TestCase):
    def test_list_runs_reports_all_without_guessing(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger(
                "r1", str(repo), plan=_plan(objective="first"), baseline={"base": "HEAD", "stage": "execute"}
            ),
            "r1",
            start=repo,
        )
        run_ledger.write_ledger(
            run_ledger.new_ledger(
                "r2", str(repo), plan=_plan(objective="second"), baseline={"base": "HEAD~1", "stage": "review"}
            ),
            "r2",
            start=repo,
        )
        runs = run_ledger.list_runs(start=repo)
        self.assertEqual([item["run_id"] for item in runs], ["r1", "r2"])
        self.assertEqual(runs[0]["plan_summary"], "first")
        self.assertEqual(runs[1]["plan_summary"], "second")
        self.assertEqual(runs[0]["base"], "HEAD")
        self.assertEqual(runs[1]["stage"], "review")
        self.assertTrue(runs[0]["created_at"])
        self.assertNotEqual(runs[0]["plan_summary"], runs[1]["plan_summary"])

    def test_list_runs_marks_corrupt_entry_without_failing(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan(objective="good")), "r1", start=repo
        )
        corrupt_dir = run_ledger.runs_dir(start=repo) / "r2"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_dir / "ledger.json").write_text("{ not json", encoding="utf-8")

        runs = run_ledger.list_runs(start=repo)
        by_id = {item["run_id"]: item for item in runs}
        self.assertEqual(by_id["r1"]["plan_summary"], "good")
        self.assertTrue(by_id["r2"]["corrupt"])
        self.assertIn("error", by_id["r2"])
        self.assertEqual(by_id["r2"]["error_code"], "invalid_input")

    def test_list_runs_marks_schema_invalid_ledger_corrupt(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan(objective="good")), "r1", start=repo
        )
        runs = run_ledger.runs_dir(start=repo)

        bad_schema_dir = runs / "r2"
        bad_schema_dir.mkdir(parents=True, exist_ok=True)
        bad_schema = run_ledger.new_ledger("r2", str(repo), plan=_plan(objective="x"))
        bad_schema["schema_version"] = 999
        (bad_schema_dir / "ledger.json").write_text(json.dumps(bad_schema), encoding="utf-8")

        missing_keys_dir = runs / "r3"
        missing_keys_dir.mkdir(parents=True, exist_ok=True)
        (missing_keys_dir / "ledger.json").write_text(
            json.dumps({"run_id": "r3"}), encoding="utf-8"
        )

        runs = run_ledger.list_runs(start=repo)
        by_id = {item["run_id"]: item for item in runs}
        self.assertEqual(by_id["r1"]["plan_summary"], "good")
        for run_id in ("r2", "r3"):
            self.assertTrue(by_id[run_id]["corrupt"])
            self.assertIn("error", by_id[run_id])
            self.assertEqual(by_id[run_id]["error_code"], "invalid_input")


class TestNonGit(unittest.TestCase):
    def test_non_git_ledger_path_under_codex_home_not_temp(self):
        workspace = Path(tempfile.mkdtemp(prefix="crp-ledger-ws-"))
        codex_home = Path(tempfile.mkdtemp(prefix="crp-ledger-home-"))
        try:
            wid = run_ledger.workspace_id(workspace)
            path = run_ledger.ledger_path("r1", start=workspace, codex_home=codex_home)
            path_str = str(path).replace("\\", "/")
            self.assertIn(
                str(codex_home).replace("\\", "/") + "/state/coding-review-pipeline/" + wid + "/runs/",
                path_str,
            )
            self.assertEqual(path.name, "ledger.json")
            self.assertTrue(path_str.endswith("/runs/r1/ledger.json"))

            ledger = run_ledger.new_ledger("r1", str(workspace), plan=_plan())
            run_ledger.write_ledger(ledger, "r1", start=workspace, codex_home=codex_home)
            reloaded = run_ledger.load_ledger("r1", start=workspace, codex_home=codex_home)
            self.assertEqual(reloaded["run_id"], "r1")
            self.assertEqual(reloaded["plan"], _plan())
        finally:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(codex_home, ignore_errors=True)

    def test_non_git_workspace_id_is_stable(self):
        workspace = Path(tempfile.mkdtemp(prefix="crp-ledger-ws-"))
        try:
            first = run_ledger.workspace_id(workspace)
            second = run_ledger.workspace_id(workspace)
            self.assertEqual(first, second)
            self.assertTrue(first)
        finally:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)


class TestNoInternalErrorMatrix(unittest.TestCase):
    """Any parseable ledger under any command must never exit 1 (internal_error)."""

    def _run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return run_ledger.main(argv)

    def test_nested_bad_shapes_never_exit_1(self):
        repo = make_repo({"README.md": "r\n"})
        run_id = "r1"
        changes_file = repo / "changes.json"
        changes_file.write_text(
            json.dumps({"models": {"coder": "x"}}), encoding="utf-8"
        )
        ledger_file = run_ledger.runs_dir(start=repo) / run_id / "ledger.json"
        mutations = {
            "repo_root": ({"a": 1}, [1, 2], "x", 7, None),
            "baseline": ({"no_fp": True}, [1, 2], "x", 7, None),
            "models": ({"a": 1}, [1, 2], "x", 7, None),
            "plan": ({"tasks": "T1"}, [1, 2], "x", 7, None),
            "events": ({}, "x", 7, None, ["e"]),
            "tasks": ({"T1": "bad"}, [1, 2], "x", 7, None),
            "agents": ({"a1": ["ACTIVE"]}, [1, 2], "x", 7, None),
            "decisions": ({"D1": "open"}, [1, 2], "x", 7, None),
            "integration": ({"i": ["x"]}, [1, 2], "x", 7, None),
        }
        commands = (
            ["load", "--run-id", run_id, "--repo", str(repo)],
            ["resume", "--run-id", run_id, "--repo", str(repo)],
            ["list", "--repo", str(repo)],
            [
                "update",
                "--run-id",
                run_id,
                "--changes",
                str(changes_file),
                "--repo",
                str(repo),
            ],
        )
        for key, shapes in mutations.items():
            for shape in shapes:
                with self.subTest(key=key, shape=shape):
                    ledger = run_ledger.new_ledger(run_id, str(repo), plan=_plan())
                    ledger[key] = shape
                    ledger_file.parent.mkdir(parents=True, exist_ok=True)
                    ledger_file.write_text(json.dumps(ledger), encoding="utf-8")
                    for command in commands:
                        code = self._run_cli(list(command))
                        self.assertNotEqual(code, 1)


class TestCli(unittest.TestCase):
    def _run(self, repo, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "run_ledger.py"), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_cli_init_round_trip(self):
        repo = make_repo({"README.md": "r\n"})
        plan_file = repo / "plan.json"
        plan_file.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
        proc = self._run(
            repo,
            "init",
            "--run-id",
            "r1",
            "--repo",
            str(repo),
            "--plan",
            str(plan_file),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["run_id"], "r1")
        self.assertEqual(out["schema_version"], 1)
        self.assertTrue(run_ledger.ledger_path("r1", start=repo).is_file())

    def test_cli_bad_run_id_exits_2(self):
        repo = make_repo({"README.md": "r\n"})
        proc = self._run(repo, "init", "--run-id", "a/b", "--repo", str(repo))
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_reserved_and_overlong_run_ids_exit_2(self):
        repo = make_repo({"README.md": "r\n"})
        for run_id in ("CON.txt", "com3", "LPT9", "x" * 65):
            with self.subTest(run_id=run_id):
                proc = self._run(repo, "init", "--run-id", run_id, "--repo", str(repo))
                self.assertEqual(proc.returncode, 2)
                error = json.loads(proc.stderr)
                self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_resume_malformed_agent_stays_structured(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger(
            "r1",
            str(repo),
            plan=_plan(),
            baseline={"base": "HEAD", "stage": "execute"},
        )
        ledger["tasks"] = {"T1": _task(state="in_progress", latest_verdict="")}
        ledger["agents"] = {
            "coder-1": _agent(
                agent_id="coder-1",
                task_id="T1",
                lifecycle_state=["RUNNING"],
            )
        }
        run_ledger.write_ledger(ledger, "r1", start=repo)
        proc = self._run(repo, "resume", "--run-id", "r1", "--repo", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["tasks"]["T1"]["resume_action"], "query_first")
        self.assertNotIn("internal_error", proc.stdout + proc.stderr)

    def test_cli_resume_unhashable_task_id_stays_structured(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger(
            "r1",
            str(repo),
            plan=_plan(),
            baseline={"base": "HEAD", "stage": "execute"},
        )
        ledger["tasks"] = {"T1": _task(state="in_progress", latest_verdict="")}
        ledger["agents"] = {
            "coder-1": _agent(
                agent_id="coder-1",
                task_id=["T1"],
                lifecycle_state="RUNNING",
            )
        }
        run_ledger.write_ledger(ledger, "r1", start=repo)
        proc = self._run(repo, "resume", "--run-id", "r1", "--repo", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["running_agents"]), 1)
        self.assertEqual(out["running_agents"][0]["task_id"], ["T1"])
        self.assertNotIn("internal_error", proc.stdout + proc.stderr)

    def test_cli_resume_non_dict_task_stays_structured(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger(
            "r1",
            str(repo),
            plan=_plan(),
            baseline={"base": "HEAD", "stage": "execute"},
        )
        ledger["tasks"] = {
            "T1": _task(state="in_progress", latest_verdict=""),
            "T2": "RUNNING",
        }
        run_ledger.write_ledger(ledger, "r1", start=repo)
        proc = self._run(repo, "resume", "--run-id", "r1", "--repo", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertIn("T2", out["tasks"])
        self.assertEqual(out["tasks"]["T2"]["resume_action"], "query_first")
        self.assertNotIn("internal_error", proc.stdout + proc.stderr)

    def test_cli_update_baseline_list_exits_2_no_write(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        path = run_ledger.ledger_path("r1", start=repo)
        original = path.read_bytes()
        changes_file = repo / "changes.json"
        changes_file.write_text(json.dumps({"baseline": [1, 2]}), encoding="utf-8")
        proc = self._run(
            repo,
            "update",
            "--run-id",
            "r1",
            "--changes",
            str(changes_file),
            "--repo",
            str(repo),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")
        self.assertEqual(path.read_bytes(), original)
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertEqual(reloaded["run_id"], "r1")
        self.assertIsInstance(reloaded["baseline"], dict)

    def test_cli_update_malformed_agents_value_exits_2_no_write(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        path = run_ledger.ledger_path("r1", start=repo)
        original = path.read_bytes()
        changes_file = repo / "changes.json"
        changes_file.write_text(
            json.dumps({"agents": {"coder-x": ["ACTIVE"]}}), encoding="utf-8"
        )
        proc = self._run(
            repo,
            "update",
            "--run-id",
            "r1",
            "--changes",
            str(changes_file),
            "--repo",
            str(repo),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")
        self.assertEqual(path.read_bytes(), original)

    def test_cli_load_baseline_non_dict_exits_2(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        path = run_ledger.ledger_path("r1", start=repo)
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["baseline"] = [1, 2]
        path.write_text(json.dumps(ledger), encoding="utf-8")
        proc = self._run(repo, "load", "--run-id", "r1", "--repo", str(repo))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_resume_baseline_non_dict_exits_3(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        path = run_ledger.ledger_path("r1", start=repo)
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["baseline"] = [1, 2]
        path.write_text(json.dumps(ledger), encoding="utf-8")
        plan_file = repo / "plan.json"
        plan_file.write_text(json.dumps(_plan()), encoding="utf-8")
        proc = self._run(
            repo,
            "resume",
            "--run-id",
            "r1",
            "--repo",
            str(repo),
            "--plan",
            str(plan_file),
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertFalse(out["ok"])
        self.assertTrue(out["plan_reconfirmation_required"])

    def test_cli_list_baseline_non_dict_marks_corrupt_exit_0(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan(objective="good")),
            "r1",
            start=repo,
        )
        runs = run_ledger.runs_dir(start=repo)
        bad_dir = runs / "r2"
        bad_dir.mkdir(parents=True, exist_ok=True)
        bad = run_ledger.new_ledger("r2", str(repo), plan=_plan(objective="bad"))
        bad["baseline"] = [1, 2]
        (bad_dir / "ledger.json").write_text(json.dumps(bad), encoding="utf-8")
        proc = self._run(repo, "list", "--repo", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        by_id = {item["run_id"]: item for item in out["runs"]}
        self.assertEqual(by_id["r1"]["plan_summary"], "good")
        self.assertTrue(by_id["r2"]["corrupt"])
        self.assertEqual(by_id["r2"]["error_code"], "invalid_input")

    def test_cli_list_undecodable_ledger_marks_corrupt_exit_0(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan(objective="good")),
            "r1",
            start=repo,
        )
        runs = run_ledger.runs_dir(start=repo)
        bad_dir = runs / "r2"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "ledger.json").write_bytes(b'{"run_id": "r2", "bad": "\xff\xfe"}')
        proc = self._run(repo, "list", "--repo", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        by_id = {item["run_id"]: item for item in out["runs"]}
        self.assertEqual(by_id["r1"]["plan_summary"], "good")
        self.assertTrue(by_id["r2"]["corrupt"])
        self.assertEqual(by_id["r2"]["error_code"], "invalid_input")

    def test_cli_load_undecodable_ledger_exits_2(self):
        repo = make_repo({"README.md": "r\n"})
        run_ledger.write_ledger(
            run_ledger.new_ledger("r1", str(repo), plan=_plan()), "r1", start=repo
        )
        path = run_ledger.ledger_path("r1", start=repo)
        path.write_bytes(b'{"schema_version": 1, "bad": "\xff"}')
        proc = self._run(repo, "load", "--run-id", "r1", "--repo", str(repo))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_cli_update_clean_entry_into_dirty_section_exits_2_no_write(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger("r1", str(repo), plan=_plan())
        ledger["agents"] = {"a1": "bad"}
        run_ledger.write_ledger(ledger, "r1", start=repo)
        path = run_ledger.ledger_path("r1", start=repo)
        original = path.read_bytes()
        changes_file = repo / "changes.json"
        changes_file.write_text(
            json.dumps(
                {"agents": {"a2": _agent(agent_id="a2", task_id="T1", lifecycle_state="DONE")}}
            ),
            encoding="utf-8",
        )
        proc = self._run(
            repo,
            "update",
            "--run-id",
            "r1",
            "--changes",
            str(changes_file),
            "--repo",
            str(repo),
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")
        self.assertEqual(path.read_bytes(), original)

    def test_cli_update_repairs_malformed_entry_exits_0(self):
        repo = make_repo({"README.md": "r\n"})
        ledger = run_ledger.new_ledger("r1", str(repo), plan=_plan())
        ledger["agents"] = {"a1": "bad"}
        run_ledger.write_ledger(ledger, "r1", start=repo)
        changes_file = repo / "changes.json"
        changes_file.write_text(
            json.dumps(
                {"agents": {"a1": _agent(agent_id="a1", task_id="T1", lifecycle_state="DONE")}}
            ),
            encoding="utf-8",
        )
        proc = self._run(
            repo,
            "update",
            "--run-id",
            "r1",
            "--changes",
            str(changes_file),
            "--repo",
            str(repo),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reloaded = run_ledger.load_ledger("r1", start=repo)
        self.assertIsInstance(reloaded["agents"]["a1"], dict)


if __name__ == "__main__":
    unittest.main()
