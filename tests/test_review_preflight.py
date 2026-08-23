# -*- coding: utf-8 -*-
"""Phase 11 Deterministic Review Preflight tests (plan sections 73-97, 122).

Coverage:
- severity mapping: a tool ``warning`` is never CRP HIGH
- parsers: gitleaks / osv-scanner / semgrep / ast-grep / pmd / checkstyle
- attribution: changed line vs outside diff vs historical (unchanged file)
- dedup: same path+range+category+message merges ``sources=[...]``
- machine blocking: new secret, known vulnerable dependency, verification
  (build/test) failure, project-configured analyzer hard failure
- negative coverage: clean / skipped / failed / unsupported + FOCUS ON
- context packer: P0-P3 tiers with tier/files/estimated_chars/omitted/reason

Unit tests exercise pure functions directly; CLI tests use a temporary git
repository and fake analyzer CLIs injected via PATH (never the real machine's
installed analyzers). The real repository is never touched.
"""

import json
import os
import shutil
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
import review_preflight  # noqa: E402


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
    repo = Path(tempfile.mkdtemp(prefix="crp-review-repo-"))
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


def _facts(**overrides):
    facts = {
        "repo_root": "/repo",
        "base": "HEAD~1",
        "head": "WORKTREE",
        "changed_files": [],
        "untracked_files": [],
        "diff_ranges": {},
        "dependency_manifest_changed": False,
        "lockfile_changed": False,
        "security_candidate": {"state": "not_detected", "evidence": []},
    }
    facts.update(overrides)
    return facts


def _proto(**overrides):
    proto = {
        "source": "semgrep",
        "rule_id": "rules.x",
        "path": "src/App.java",
        "start_line": 5,
        "end_line": 5,
        "severity_raw": "WARNING",
        "category": "correctness",
        "message": "issue",
        "confidence": "medium",
    }
    proto.update(overrides)
    return proto


def _write_fake_cli(bin_dir: Path, name: str, stdout_text: str | None = None, exit_code: int = 0):
    """Create a fake analyzer CLI inside ``bin_dir`` (Windows .cmd, else sh).

    The fake CLI prints ``stdout_text`` to stdout and exits with ``exit_code``.
    When ``exit_code`` is non-zero it prints ``<name> failed`` to stderr.
    """

    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        if exit_code != 0:
            script = "@echo off\r\necho {name} failed 1>&2\r\nexit /b {code}\r\n".format(
                name=name, code=exit_code
            )
            (bin_dir / (name + ".cmd")).write_text(script, encoding="ascii")
        else:
            out_name = name + ".out"
            (bin_dir / out_name).write_text(
                "" if stdout_text is None else stdout_text, encoding="utf-8"
            )
            script = '@echo off\r\ntype "%~dp0{out}"\r\nexit /b 0\r\n'.format(out=out_name)
            (bin_dir / (name + ".cmd")).write_text(script, encoding="ascii")
    else:
        target = bin_dir / name
        if exit_code != 0:
            target.write_text(
                "#!/bin/sh\necho '{name} failed' >&2\nexit {code}\n".format(
                    name=name, code=exit_code
                ),
                encoding="utf-8",
            )
        else:
            target.write_text(
                "#!/bin/sh\ncat <<'EOF'\n{}\nEOF\n".format("" if stdout_text is None else stdout_text),
                encoding="utf-8",
            )
        target.chmod(0o755)


def _write_arg_recording_cli(bin_dir: Path, name: str, args_file: Path, stdout_text: str = ""):
    """Fake CLI that appends its argv to ``args_file`` then emits ``stdout_text``."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        out_name = name + ".out"
        (bin_dir / out_name).write_text(stdout_text, encoding="utf-8")
        script = (
            "@echo off\r\n"
            'echo %* >> "{args}"\r\n'
            'type "%~dp0{out}"\r\n'
            "exit /b 0\r\n"
        ).format(args=str(args_file), out=out_name)
        (bin_dir / (name + ".cmd")).write_text(script, encoding="ascii")
    else:
        target = bin_dir / name
        target.write_text(
            '#!/bin/sh\necho "$@" >> "{args}"\ncat <<\'EOF\'\n{out}\nEOF\n'.format(
                args=str(args_file), out=stdout_text
            ),
            encoding="utf-8",
        )
        target.chmod(0o755)


def _sanitized_path(bin_dir: Path) -> str:
    """PATH with only the fake CLI dir plus the directory holding git."""
    parts = [str(bin_dir)]
    git = shutil.which("git")
    if git:
        parts.append(str(Path(git).parent))
    return os.pathsep.join(parts)


class TestSeverityMapping(unittest.TestCase):
    def test_tool_warning_is_not_crp_high(self):
        self.assertEqual(review_preflight.map_severity("semgrep", "WARNING"), "MEDIUM")
        self.assertEqual(review_preflight.map_severity("semgrep", "ERROR"), "HIGH")
        self.assertEqual(review_preflight.map_severity("semgrep", "INFO"), "INFO")
        self.assertEqual(review_preflight.map_severity("semgrep", None), "MEDIUM")

    def test_checkstyle_warning_is_medium(self):
        self.assertEqual(review_preflight.map_severity("checkstyle", "warning"), "MEDIUM")
        self.assertEqual(review_preflight.map_severity("checkstyle", "error"), "HIGH")

    def test_pmd_priority_mapping(self):
        self.assertEqual(review_preflight.map_severity("pmd", "1"), "HIGH")
        self.assertEqual(review_preflight.map_severity("pmd", "2"), "MEDIUM")
        self.assertEqual(review_preflight.map_severity("pmd", "5"), "INFO")

    def test_secret_and_vulnerability_defaults(self):
        self.assertEqual(review_preflight.map_severity("gitleaks", None), "HIGH")
        self.assertEqual(review_preflight.map_severity("osv-scanner", "CRITICAL"), "BLOCKER")
        self.assertEqual(review_preflight.map_severity("osv-scanner", "LOW"), "LOW")
        self.assertEqual(review_preflight.map_severity("osv-scanner", None), "HIGH")

    def test_heuristic_is_candidate_not_blocker(self):
        self.assertEqual(review_preflight.map_severity("heuristic", None), "MEDIUM")


class TestParsers(unittest.TestCase):
    def test_gitleaks_secret(self):
        data = [
            {
                "RuleID": "generic-api-key",
                "Description": "Found API key",
                "StartLine": 5,
                "EndLine": 5,
                "File": "src/App.java",
                "Secret": "AKIAEXAMPLE",
            }
        ]
        findings = review_preflight.parse_gitleaks(data)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["source"], "gitleaks")
        self.assertEqual(finding["rule_id"], "generic-api-key")
        self.assertEqual(finding["path"], "src/App.java")
        self.assertEqual(finding["start_line"], 5)
        self.assertEqual(finding["category"], "security")
        self.assertEqual(finding["confidence"], "high")

    def test_osv_vulnerability(self):
        data = {
            "results": [
                {
                    "source": {"path": "pom.xml"},
                    "packages": [
                        {
                            "package": {"name": "org.example:lib", "version": "1.0.0"},
                            "vulnerabilities": [
                                {
                                    "id": "GHSA-1234",
                                    "aliases": ["CVE-2024-0001"],
                                    "summary": "Remote code execution",
                                    "severity": "CRITICAL",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        findings = review_preflight.parse_osv(data)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["source"], "osv-scanner")
        self.assertEqual(finding["rule_id"], "GHSA-1234")
        self.assertEqual(finding["path"], "pom.xml")
        self.assertEqual(finding["start_line"], 0)
        self.assertEqual(finding["severity_raw"], "CRITICAL")
        self.assertEqual(finding["category"], "dependency")
        self.assertEqual(finding["confidence"], "high")

    def test_semgrep_warning(self):
        data = {
            "results": [
                {
                    "check_id": "rules.null-deref",
                    "path": "src/App.java",
                    "start": {"line": 10},
                    "end": {"line": 10},
                    "extra": {
                        "severity": "WARNING",
                        "message": "Possible null dereference",
                        "metadata": {"category": "correctness"},
                    },
                }
            ]
        }
        findings = review_preflight.parse_semgrep(data)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["source"], "semgrep")
        self.assertEqual(finding["rule_id"], "rules.null-deref")
        self.assertEqual(finding["start_line"], 10)
        self.assertEqual(finding["severity_raw"], "WARNING")
        self.assertEqual(finding["message"], "Possible null dereference")

    def test_pmd_java(self):
        data = {
            "files": [
                {
                    "filename": "src/App.java",
                    "violations": [
                        {
                            "beginline": 10,
                            "endline": 10,
                            "rule": "NullDereference",
                            "ruleset": "Best Practices",
                            "priority": 1,
                            "description": "Possible null pointer dereference",
                        }
                    ],
                }
            ]
        }
        findings = review_preflight.parse_pmd(data)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["source"], "pmd")
        self.assertEqual(finding["rule_id"], "NullDereference")
        self.assertEqual(finding["category"], "correctness")
        self.assertEqual(finding["severity_raw"], "1")

    def test_checkstyle_xml(self):
        text = (
            '<?xml version="1.0"?>\n'
            '<checkstyle version="10.0">\n'
            '<file name="src/App.java">\n'
            '<error line="7" severity="error" '
            'message="Missing a Javadoc comment." '
            'source="com.puppycrawl.tools.checkstyle.checks.javadoc.MissingJavadocMethod"/>\n'
            "</file>\n"
            "</checkstyle>\n"
        )
        findings = review_preflight.parse_checkstyle(text)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["source"], "checkstyle")
        self.assertEqual(finding["rule_id"], "MissingJavadocMethod")
        self.assertEqual(finding["path"], "src/App.java")
        self.assertEqual(finding["start_line"], 7)
        self.assertEqual(finding["severity_raw"], "error")
        self.assertEqual(finding["category"], "style")

    def test_astgrep(self):
        data = [
            {
                "ruleId": "no-unused-var",
                "file": "src/app.py",
                "range": {"start": {"line": 3}, "end": {"line": 3}},
                "message": "unused variable",
                "severity": "warning",
            }
        ]
        findings = review_preflight.parse_astgrep(data)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["source"], "ast-grep")
        self.assertEqual(finding["severity_raw"], "warning")


class TestAttribution(unittest.TestCase):
    def _facts(self):
        return _facts(
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 5, "end": 5}]},
        )

    def test_changed_line_is_attributable(self):
        in_diff, attributable = review_preflight.attribute(_proto(start_line=5), self._facts())
        self.assertTrue(in_diff)
        self.assertTrue(attributable)

    def test_outside_diff_is_not_attributable(self):
        in_diff, attributable = review_preflight.attribute(_proto(start_line=20), self._facts())
        self.assertFalse(in_diff)
        self.assertFalse(attributable)

    def test_historical_unchanged_file_is_not_attributable(self):
        in_diff, attributable = review_preflight.attribute(
            _proto(path="src/Other.java", start_line=5), self._facts()
        )
        self.assertFalse(in_diff)
        self.assertFalse(attributable)

    def test_untracked_full_file_is_attributable(self):
        facts = _facts(
            untracked_files=["src/New.java"],
            diff_ranges={"src/New.java": [{"start": 1, "end": 10, "full_file": True}]},
        )
        in_diff, attributable = review_preflight.attribute(
            _proto(path="src/New.java", start_line=3), facts
        )
        self.assertTrue(in_diff)
        self.assertTrue(attributable)

    def test_lineless_dependency_finding_attributes_by_file(self):
        facts = _facts(
            changed_files=["pom.xml"],
            dependency_manifest_changed=True,
            diff_ranges={"pom.xml": [{"start": 1, "end": 3}]},
        )
        in_diff, attributable = review_preflight.attribute(
            _proto(path="pom.xml", start_line=0, end_line=0, source="osv-scanner"), facts
        )
        self.assertTrue(in_diff)
        self.assertTrue(attributable)


class TestDedup(unittest.TestCase):
    def _finalized(self, source, message="Possible null dereference", path="src/App.java", line=10):
        proto = _proto(
            source=source,
            rule_id="NullDereference",
            path=path,
            start_line=line,
            end_line=line,
            category="correctness",
            message=message,
        )
        return review_preflight.finalize_finding(proto, _facts())

    def test_merges_sources_for_same_issue(self):
        a = self._finalized("semgrep")
        b = self._finalized("pmd")
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        merged = review_preflight.deduplicate([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0]["sources"]), ["pmd", "semgrep"])

    def test_different_location_does_not_merge(self):
        a = self._finalized("semgrep", line=10)
        b = self._finalized("pmd", line=11)
        merged = review_preflight.deduplicate([a, b])
        self.assertEqual(len(merged), 2)

    def test_merged_severity_takes_highest_rank(self):
        a = review_preflight.finalize_finding(
            _proto(
                source="semgrep",
                severity_raw="WARNING",
                rule_id="NullDereference",
                category="correctness",
                message="Possible null dereference",
                path="src/App.java",
                start_line=10,
                end_line=10,
            ),
            _facts(),
        )
        b = review_preflight.finalize_finding(
            _proto(
                source="pmd",
                severity_raw="1",
                rule_id="NullDereference",
                category="correctness",
                message="Possible null dereference",
                path="src/App.java",
                start_line=10,
                end_line=10,
            ),
            _facts(),
        )
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        self.assertEqual(a["severity"], "MEDIUM")
        self.assertEqual(b["severity"], "HIGH")
        merged = review_preflight.deduplicate([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["severity"], "HIGH")
        self.assertEqual(sorted(merged[0]["sources"]), ["pmd", "semgrep"])


class TestMachineBlocking(unittest.TestCase):
    def test_security_candidate_is_candidate_not_blocker(self):
        facts = _facts(
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 9, "end": 9}]},
            security_candidate={
                "state": "candidate",
                "evidence": [{"file": "src/App.java", "line": 9, "match": "password"}],
            },
        )
        findings = review_preflight.security_candidate_findings(facts)
        self.assertEqual(len(findings), 1)
        finding = review_preflight.finalize_finding(findings[0], facts)
        self.assertEqual(finding["severity"], "MEDIUM")
        self.assertEqual(finding["confidence"], "low")
        self.assertTrue(finding["attributable"])

    def test_verification_failure_is_blocker(self):
        findings = review_preflight.verification_failure_findings(
            [{"command": "pytest", "exit_code": 1, "failure_count": 2}]
        )
        self.assertEqual(len(findings), 1)
        finding = review_preflight.finalize_finding(findings[0], _facts())
        self.assertEqual(finding["severity"], "HIGH")
        self.assertEqual(finding["category"], "test")
        self.assertEqual(finding["confidence"], "high")

    def test_verification_success_is_not_blocker(self):
        findings = review_preflight.verification_failure_findings(
            [{"command": "pytest", "exit_code": 0, "failure_count": 0}]
        )
        self.assertEqual(findings, [])

    def test_verification_inspect_not_test(self):
        for command in ("contest", "protest"):
            with self.subTest(command=command):
                findings = review_preflight.verification_failure_findings(
                    [{"command": command, "exit_code": 1}]
                )
                self.assertEqual(len(findings), 1)
                finding = review_preflight.finalize_finding(findings[0], _facts())
                self.assertEqual(finding["category"], "build")

    def test_verification_word_boundary_matches_test(self):
        for command in ("mvn test", "gradle test", "go test"):
            with self.subTest(command=command):
                findings = review_preflight.verification_failure_findings(
                    [{"command": command, "exit_code": 1}]
                )
                finding = review_preflight.finalize_finding(findings[0], _facts())
                self.assertEqual(finding["category"], "test")

    def test_secret_like_message_is_redacted(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        facts = _facts(
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 9, "end": 9}]},
            security_candidate={
                "state": "candidate",
                "evidence": [{"file": "src/App.java", "line": 9, "match": secret}],
            },
        )
        protos = review_preflight.security_candidate_findings(facts)
        finding = review_preflight.finalize_finding(protos[0], facts)
        self.assertNotIn(secret, finding["message"])
        self.assertIn("masked=", finding["message"])
        self.assertIn("rule=security_candidate", finding["message"])


class TestNegativeCoverage(unittest.TestCase):
    def test_clean_skipped_failed_and_focus(self):
        results = [
            {"name": "pmd", "state": "ran", "reason": None, "findings_count": 0},
            {"name": "osv-scanner", "state": "skipped", "reason": "dependency unchanged", "findings_count": 0},
            {"name": "semgrep", "state": "failed", "reason": "exit code 2", "findings_count": 0},
            {"name": "reviewdog", "state": "unsupported", "reason": "no adapter", "findings_count": 0},
        ]
        coverage = review_preflight.build_machine_coverage(results)
        self.assertIn("pmd", coverage["clean"])
        self.assertEqual(coverage["skipped"][0]["name"], "osv-scanner")
        self.assertEqual(coverage["failed"][0]["name"], "semgrep")
        self.assertEqual(coverage["unsupported"][0]["name"], "reviewdog")
        self.assertIn("behavior correctness", coverage["focus_on"])
        self.assertTrue(coverage["do_not_spend_budget_on"])

    def test_build_integrated_java_analyzers_listed(self):
        result = review_preflight.run_preflight("/repo", _facts(), which_fn=lambda name: None)
        unsupported = {
            item["name"]: item for item in result["machine_coverage"]["unsupported"]
        }
        for name in ("spotbugs", "archunit", "error-prone", "p3c", "sonar"):
            self.assertIn(name, unsupported)
            self.assertIn("build-integrated", unsupported[name]["reason"])


class TestDetectOnlyAnalyzers(unittest.TestCase):
    def test_pmd_and_checkstyle_skipped_detect_only_and_never_run(self):
        facts = _facts(
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 1, "end": 1}]},
        )

        def which(name):
            if name in ("pmd", "checkstyle"):
                return "/fake/bin/" + name
            return None

        with mock.patch.object(review_preflight.subprocess, "run") as run_mock:
            result = review_preflight.run_preflight("/repo", facts, which_fn=which)
        run_mock.assert_not_called()
        analyzers = {item["name"]: item for item in result["analyzers"]}
        for name in ("pmd", "checkstyle"):
            self.assertEqual(analyzers[name]["state"], "skipped")
            self.assertIn("detect_only", analyzers[name]["reason"])
            self.assertIn("missing_required_args", analyzers[name]["reason"])
        self.assertFalse(
            [finding for finding in result["findings"] if finding["source"] in ("pmd", "checkstyle")]
        )
        self.assertFalse(
            [
                finding
                for finding in result["findings"]
                if finding["rule_id"] == "analyzer_hard_failure"
            ]
        )


class TestContextPacker(unittest.TestCase):
    def test_p0_to_p3_tiers_with_budget(self):
        with tempfile.TemporaryDirectory(prefix="crp-ctx-repo-") as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir(parents=True, exist_ok=True)
            (repo / "src" / "App.java").write_text("x" * 1000, encoding="utf-8")
            facts_file = repo / "facts.json"
            facts_file.write_text(json.dumps(_facts(changed_files=["src/App.java"])), encoding="utf-8")
            task_file = repo / "task.json"
            task_file.write_text(json.dumps({"risk": "HIGH"}), encoding="utf-8")
            verification_file = repo / "verification.json"
            verification_file.write_text(json.dumps([]), encoding="utf-8")
            facts = _facts(
                repo_root=str(repo),
                changed_files=["src/App.java"],
                diff_ranges={"src/App.java": [{"start": 1, "end": 1}]},
            )
            context = review_preflight.pack_review_context(
                str(repo),
                facts,
                task_facts_path=str(task_file),
                verification_path=str(verification_file),
                findings=[],
                facts_path=str(facts_file),
            )
            tiers = {tier["tier"]: tier for tier in context["tiers"]}
            self.assertEqual(set(tiers), {"P0", "P1", "P2", "P3"})
            for tier in ("P0", "P1", "P2", "P3"):
                for key in ("tier", "files", "estimated_chars", "omitted", "reason"):
                    self.assertIn(key, tiers[tier], tier)
            self.assertIn("src/App.java", tiers["P1"]["files"])
            self.assertGreaterEqual(tiers["P1"]["estimated_chars"], 1000)
            self.assertIsInstance(tiers["P0"]["files"], list)
            self.assertIn(str(facts_file), tiers["P0"]["files"])
            self.assertIsInstance(context["total_estimated_chars"], int)


class TestCli(unittest.TestCase):
    def _run(self, repo, bin_dir, *args):
        env = dict(os.environ)
        env["PATH"] = _sanitized_path(bin_dir)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "review_preflight.py"), "--repo", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env=env,
        )

    def _repo(self, files):
        repo = make_repo(files)
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        return repo

    def _tmpdir(self, prefix):
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def test_end_to_end_fixtures(self):
        repo = self._repo({"src/App.java": "public class App {}\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        _write_fake_cli(
            bin_dir,
            "gitleaks",
            json.dumps(
                [
                    {
                        "RuleID": "generic-api-key",
                        "Description": "Found API key",
                        "StartLine": 5,
                        "EndLine": 5,
                        "File": "src/App.java",
                        "Secret": "AKIAEXAMPLE",
                    }
                ]
            ),
        )
        _write_fake_cli(
            bin_dir,
            "semgrep",
            json.dumps(
                {
                    "results": [
                        {
                            "check_id": "rules.null-deref",
                            "path": "src/App.java",
                            "start": {"line": 20},
                            "end": {"line": 20},
                            "extra": {
                                "severity": "WARNING",
                                "message": "Possible null dereference",
                                "metadata": {"category": "correctness"},
                            },
                        }
                    ]
                }
            ),
        )
        _write_fake_cli(
            bin_dir,
            "pmd",
            json.dumps(
                {
                    "files": [
                        {
                            "filename": "src/App.java",
                            "violations": [
                                {
                                    "beginline": 5,
                                    "endline": 5,
                                    "rule": "NullDereference",
                                    "ruleset": "Best Practices",
                                    "priority": 1,
                                    "description": "Possible null pointer dereference",
                                }
                            ],
                        }
                    ]
                }
            ),
        )
        _write_fake_cli(bin_dir, "ast-grep", "[]")
        _write_fake_cli(bin_dir, "osv-scanner", '{"results": []}')
        _write_fake_cli(bin_dir, "reviewdog", "whatever")
        _write_fake_cli(bin_dir, "checkstyle", exit_code=2)

        facts = _facts(
            repo_root=str(repo),
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 5, "end": 5}]},
        )
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")

        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)

        findings = out["findings"]
        by_source = {finding["source"]: finding for finding in findings}
        self.assertIn("gitleaks", by_source)
        self.assertTrue(by_source["gitleaks"]["attributable"])
        self.assertEqual(by_source["gitleaks"]["severity"], "HIGH")
        self.assertIn("semgrep", by_source)
        self.assertFalse(by_source["semgrep"]["attributable"])
        self.assertEqual(by_source["semgrep"]["severity"], "MEDIUM")

        coverage = out["machine_coverage"]
        self.assertIn("ast-grep", coverage["clean"])
        self.assertTrue(any(item["name"] == "osv-scanner" for item in coverage["skipped"]))
        self.assertTrue(any(item["name"] == "reviewdog" for item in coverage["unsupported"]))
        analyzers = {item["name"]: item for item in out["analyzers"]}
        for name in ("pmd", "checkstyle"):
            self.assertEqual(analyzers[name]["state"], "skipped")
            self.assertIn("detect_only", analyzers[name]["reason"])
        self.assertNotIn("pmd", by_source)
        self.assertNotIn("checkstyle", by_source)

        tiers = [tier["tier"] for tier in out["review_context"]["tiers"]]
        self.assertEqual(tiers, ["P0", "P1", "P2", "P3"])
    def test_out_index_prints_compact_summary_and_writes_package(self):
        repo = self._repo({"src/App.java": "public class App {}\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        _write_fake_cli(bin_dir, "gitleaks", "[]")
        _write_fake_cli(bin_dir, "semgrep", '{"results": []}')
        _write_fake_cli(bin_dir, "ast-grep", "[]")
        _write_fake_cli(bin_dir, "osv-scanner", '{"results": []}')
        _write_fake_cli(bin_dir, "reviewdog", "whatever")

        facts = _facts(repo_root=str(repo), changed_files=["src/App.java"], diff_ranges={})
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")

        package_path = repo.parent / ("pkg-" + next(tempfile._get_candidate_names()) + ".json")
        self.addCleanup(lambda: package_path.unlink(missing_ok=True))
        proc = self._run(
            repo,
            bin_dir,
            "--facts",
            str(facts_file),
            "--out-index",
            str(package_path),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        index = json.loads(proc.stdout)
        self.assertEqual(index["package_path"], str(package_path.resolve()))
        self.assertIn("finding_total", index)
        self.assertIn("severity_counts", index)
        full = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertIn("findings", full)
        # compact stdout must be much smaller than the full package text
        self.assertLess(len(proc.stdout), 2000)

    def test_detect_only_analyzer_never_runs_cli_even_when_present(self):
        repo = self._repo(
            {"src/App.java": "public class App {}\n", "pmd.xml": "<ruleset/>\n"}
        )
        bin_dir = self._tmpdir("crp-review-bin-")
        args_file = bin_dir / "pmd.args"
        _write_arg_recording_cli(bin_dir, "pmd", args_file, "{}")
        facts = _facts(
            repo_root=str(repo),
            changed_files=["src/App.java"],
            diff_ranges={},
        )
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        analyzers = {item["name"]: item for item in out["analyzers"]}
        self.assertEqual(analyzers["pmd"]["state"], "skipped")
        self.assertIn("detect_only", analyzers["pmd"]["reason"])
        self.assertFalse(args_file.exists())
        self.assertFalse(
            [finding for finding in out["findings"] if finding["source"] == "pmd"]
        )

    def test_dedup_merges_two_analyzers(self):
        repo = self._repo({"src/App.java": "public class App {}\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        semgrep = json.dumps(
            {
                "results": [
                    {
                        "check_id": "maintainability.unused",
                        "path": "src/App.java",
                        "start": {"line": 5},
                        "end": {"line": 5},
                        "extra": {
                            "severity": "ERROR",
                            "message": "Unused variable",
                            "metadata": {"category": "maintainability"},
                        },
                    }
                ]
            }
        )
        astgrep = json.dumps(
            [
                {
                    "ruleId": "no-unused-var",
                    "file": "src/App.java",
                    "range": {"start": {"line": 5}, "end": {"line": 5}},
                    "message": "Unused variable",
                    "severity": "error",
                }
            ]
        )
        _write_fake_cli(bin_dir, "semgrep", semgrep)
        _write_fake_cli(bin_dir, "ast-grep", astgrep)
        facts = _facts(
            repo_root=str(repo),
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 5, "end": 5}]},
        )
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        findings = out["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(sorted(findings[0]["sources"]), ["ast-grep", "semgrep"])

    def test_project_configured_analyzer_hard_failure_is_blocker(self):
        repo = self._repo(
            {
                "src/App.java": "public class App {}\n",
                ".gitleaks.toml": 'title = "test"\n',
            }
        )
        bin_dir = self._tmpdir("crp-review-bin-")
        _write_fake_cli(bin_dir, "gitleaks", exit_code=2)
        facts = _facts(
            repo_root=str(repo),
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 1, "end": 1}]},
        )
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        blocker = [
            finding
            for finding in out["findings"]
            if finding["category"] == "build" and finding["severity"] == "HIGH"
        ]
        self.assertTrue(blocker)
        self.assertEqual(blocker[0]["rule_id"], "analyzer_hard_failure")
        self.assertIn("gitleaks", blocker[0]["message"])

    def test_invalid_facts_exits_2(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        facts_file = repo / "facts.json"
        facts_file.write_text("{ not json", encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_undecodable_facts_exits_2(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        facts_file = repo / "facts.json"
        facts_file.write_bytes(b'{"changed_files": ["\xff"]}')
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_preflight_succeeds_without_optional_analyzers(self):
        repo = self._repo({"src/App.java": "public class App {}\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        facts = _facts(repo_root=str(repo), changed_files=["src/App.java"], diff_ranges={})
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["findings"], [])

    def test_gitleaks_receives_changed_files_not_full_repo(self):
        repo = self._repo({"src/App.java": "public class App {}\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        args_file = bin_dir / "gitleaks.args"
        _write_arg_recording_cli(bin_dir, "gitleaks", args_file, "[]")
        facts = _facts(
            repo_root=str(repo),
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 1, "end": 1}]},
        )
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        recorded = args_file.read_text(encoding="utf-8") if args_file.exists() else ""
        self.assertIn("src/App.java", recorded)
        self.assertNotIn("--source", recorded)

    def test_gitleaks_skipped_when_no_changed_files(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        args_file = bin_dir / "gitleaks.args"
        _write_arg_recording_cli(bin_dir, "gitleaks", args_file, "[]")
        facts = _facts(repo_root=str(repo), changed_files=[], untracked_files=[])
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        gitleaks = next(a for a in out["analyzers"] if a["name"] == "gitleaks")
        self.assertEqual(gitleaks["state"], "skipped")
        self.assertEqual(gitleaks["reason"], "no changed files")
        self.assertFalse(args_file.exists())

    def test_analyzer_invalid_json_is_failed_not_fatal(self):
        repo = self._repo({"src/App.java": "public class App {}\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        _write_fake_cli(bin_dir, "semgrep", "{ not json")
        facts = _facts(
            repo_root=str(repo),
            changed_files=["src/App.java"],
            diff_ranges={"src/App.java": [{"start": 1, "end": 1}]},
        )
        facts_file = repo / "facts.json"
        facts_file.write_text(json.dumps(facts), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--facts", str(facts_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        semgrep = next(a for a in out["analyzers"] if a["name"] == "semgrep")
        self.assertEqual(semgrep["state"], "failed")
        self.assertIn("JSON", semgrep["reason"])

    def test_task_facts_missing_exits_2(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        proc = self._run(repo, bin_dir, "--task-facts", str(repo / "missing.json"))
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_task_facts_invalid_json_exits_2(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        task = repo / "task.json"
        task.write_text("{ not json", encoding="utf-8")
        proc = self._run(repo, bin_dir, "--task-facts", str(task))
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_task_facts_undecodable_exits_2(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        task = repo / "task.json"
        task.write_bytes(b'{"risk": "\xff"}')
        proc = self._run(repo, bin_dir, "--task-facts", str(task))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = json.loads(proc.stderr)
        self.assertEqual(error["error"]["code"], "invalid_input")

    def test_task_facts_valid_is_accepted(self):
        repo = self._repo({"README.md": "r\n"})
        bin_dir = self._tmpdir("crp-review-bin-")
        task = repo / "task.json"
        task.write_text(json.dumps({"risk": "HIGH"}), encoding="utf-8")
        proc = self._run(repo, bin_dir, "--task-facts", str(task))
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
