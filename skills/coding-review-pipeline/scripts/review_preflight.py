#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic Review Preflight (plan sections 73-97).

This module is a WRAP/ADAPTER, not an agent and not a scanner reimplementation.
It detects analyzers (reuse-before-install, never auto-installing), runs only
allowed analyzers via their CLIs, normalizes their output into one finding
schema, diff-filters and attributes findings, deduplicates across sources,
builds negative coverage (machine coverage), and packs a review-context budget
(P0-P3). OCR is optional rule enrichment: missing OCR is reported SKIPPED and
processing continues; it never STOPs review.

Machine-readable UTF-8 JSON on stdout; structured errors on stderr. Exit codes
follow ``crp_common``: 0 ok / 2 invalid_input / 3 policy_blocked / 1
internal_error. Reuses ``crp_common`` for hashing, JSON, atomic write, git, and
timestamps; it does not reimplement scanning, atomic write, hashing, or git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import crp_common
from crp_common import (
    CrpError,
    EXIT_OK,
    emit_error,
    exit_code,
    reconfigure_stdio,
)


SEVERITY_RANK = {
    "BLOCKER": 5,
    "HIGH": 4,
    "MEDIUM": 3,
    "LOW": 2,
    "INFO": 1,
}

# Per-analyzer severity mapping. A tool's ``warning``/``info`` must never be
# silently promoted to CRP HIGH; each analyzer declares its own semantics.
SEVERITY_MAPS: dict[str, dict[str, str]] = {
    "gitleaks": {"default": "HIGH"},
    "osv-scanner": {
        "CRITICAL": "BLOCKER",
        "HIGH": "HIGH",
        "MODERATE": "MEDIUM",
        "MEDIUM": "MEDIUM",
        "LOW": "LOW",
        "default": "HIGH",
    },
    "semgrep": {
        "ERROR": "HIGH",
        "WARNING": "MEDIUM",
        "INFO": "INFO",
        "default": "MEDIUM",
    },
    "ast-grep": {
        "error": "HIGH",
        "warning": "MEDIUM",
        "info": "INFO",
        "hint": "LOW",
        "default": "MEDIUM",
    },
    "pmd": {
        "1": "HIGH",
        "2": "MEDIUM",
        "3": "MEDIUM",
        "4": "LOW",
        "5": "INFO",
        "default": "MEDIUM",
    },
    "checkstyle": {
        "error": "HIGH",
        "warning": "MEDIUM",
        "info": "INFO",
        "ignore": "INFO",
        "default": "MEDIUM",
    },
    # Synthetic machine findings produced by this adapter itself.
    "verification": {"default": "HIGH"},
    "project-analyzer": {"default": "HIGH"},
    "heuristic": {"default": "MEDIUM"},
}


def map_severity(source: str, severity_raw: object) -> str:
    """Map a raw analyzer severity to a CRP severity (never over-promote)."""

    mapping = SEVERITY_MAPS.get(source, {})
    if severity_raw is not None:
        key = str(severity_raw)
        if key in mapping:
            return mapping[key]
        upper = key.upper()
        if upper in mapping:
            return mapping[upper]
    return mapping.get("default", "MEDIUM")


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_gitleaks(data: object) -> list[dict[str, object]]:
    """Normalize native Gitleaks JSON (a list of findings)."""

    protos: list[dict[str, object]] = []
    if not isinstance(data, list):
        return protos
    for item in data:
        if not isinstance(item, dict):
            continue
        start = _to_int(item.get("StartLine"), 0)
        protos.append(
            {
                "source": "gitleaks",
                "rule_id": item.get("RuleID"),
                "path": item.get("File", ""),
                "start_line": start,
                "end_line": _to_int(item.get("EndLine"), start),
                "severity_raw": None,
                "category": "security",
                "message": item.get("Description", ""),
                "confidence": "high",
            }
        )
    return protos


def _osv_severity(vuln: dict) -> str | None:
    sev = vuln.get("severity")
    if isinstance(sev, str) and sev:
        return sev
    dbs = vuln.get("database_specific") or {}
    if isinstance(dbs.get("severity"), str):
        return dbs["severity"]
    if isinstance(sev, list):
        for entry in sev:
            if isinstance(entry, dict) and isinstance(entry.get("score"), str):
                for level in ("CRITICAL", "HIGH", "MODERATE", "MEDIUM", "LOW"):
                    if level in entry["score"].upper():
                        return level
    return None


def parse_osv(data: object) -> list[dict[str, object]]:
    """Normalize native OSV-Scanner JSON into dependency vulnerability findings."""

    protos: list[dict[str, object]] = []
    if not isinstance(data, dict):
        return protos
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        source = result.get("source") or {}
        path = source.get("path", "") if isinstance(source, dict) else ""
        for pkg in result.get("packages") or []:
            if not isinstance(pkg, dict):
                continue
            pkg_info = pkg.get("package") or {}
            name = pkg_info.get("name", "") if isinstance(pkg_info, dict) else ""
            version = pkg_info.get("version", "") if isinstance(pkg_info, dict) else ""
            for vuln in pkg.get("vulnerabilities") or []:
                if not isinstance(vuln, dict):
                    continue
                message = " ".join(
                    part
                    for part in (
                        f"{name} {version}".strip(),
                        vuln.get("summary") or vuln.get("id") or "",
                    )
                    if part
                )
                protos.append(
                    {
                        "source": "osv-scanner",
                        "rule_id": vuln.get("id"),
                        "path": path,
                        "start_line": 0,
                        "end_line": 0,
                        "severity_raw": _osv_severity(vuln),
                        "category": "dependency",
                        "message": message,
                        "confidence": "high",
                    }
                )
    return protos


def parse_semgrep(data: object) -> list[dict[str, object]]:
    """Normalize native Semgrep JSON results."""

    protos: list[dict[str, object]] = []
    if not isinstance(data, dict):
        return protos
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        extra = result.get("extra") or {}
        metadata = extra.get("metadata") or {}
        category = metadata.get("category") if isinstance(metadata, dict) else None
        start = (result.get("start") or {}).get("line")
        end = (result.get("end") or {}).get("line")
        protos.append(
            {
                "source": "semgrep",
                "rule_id": result.get("check_id"),
                "path": result.get("path", ""),
                "start_line": _to_int(start, 0),
                "end_line": _to_int(end, 0),
                "severity_raw": extra.get("severity") if isinstance(extra, dict) else None,
                "category": category or "correctness",
                "rule_family": category,
                "message": extra.get("message", "") if isinstance(extra, dict) else "",
                "confidence": "medium",
            }
        )
    return protos


def parse_astgrep(data: object) -> list[dict[str, object]]:
    """Normalize native ast-grep JSON results."""

    protos: list[dict[str, object]] = []
    if not isinstance(data, list):
        return protos
    for item in data:
        if not isinstance(item, dict):
            continue
        rng = item.get("range") or {}
        start = (rng.get("start") or {}).get("line") if isinstance(rng, dict) else None
        end = (rng.get("end") or {}).get("line") if isinstance(rng, dict) else None
        protos.append(
            {
                "source": "ast-grep",
                "rule_id": item.get("ruleId"),
                "path": item.get("file", ""),
                "start_line": _to_int(start, 0),
                "end_line": _to_int(end, 0),
                "severity_raw": item.get("severity"),
                "category": "maintainability",
                "message": item.get("message", ""),
                "confidence": "medium",
            }
        )
    return protos


def parse_pmd(data: object) -> list[dict[str, object]]:
    """Normalize native PMD JSON output."""

    protos: list[dict[str, object]] = []
    if not isinstance(data, dict):
        return protos
    for file_entry in data.get("files") or []:
        if not isinstance(file_entry, dict):
            continue
        name = file_entry.get("filename", "")
        for violation in file_entry.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            start = _to_int(violation.get("beginline"), 0)
            priority = violation.get("priority")
            protos.append(
                {
                    "source": "pmd",
                    "rule_id": violation.get("rule"),
                    "path": name,
                    "start_line": start,
                    "end_line": _to_int(violation.get("endline"), start),
                    "severity_raw": str(priority) if priority is not None else None,
                    "category": "correctness",
                    "rule_family": violation.get("ruleset"),
                    "message": violation.get("description", ""),
                    "confidence": "medium",
                }
            )
    return protos


def parse_checkstyle(text: str) -> list[dict[str, object]]:
    """Normalize native Checkstyle XML output."""

    protos: list[dict[str, object]] = []
    if not text or not text.strip():
        return protos
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return protos
    for file_el in root.iter("file"):
        name = file_el.get("name", "")
        for error_el in file_el.iter("error"):
            line = _to_int(error_el.get("line"), 0)
            source = error_el.get("source") or ""
            protos.append(
                {
                    "source": "checkstyle",
                    "rule_id": source.rsplit(".", 1)[-1],
                    "path": name,
                    "start_line": line,
                    "end_line": line,
                    "severity_raw": error_el.get("severity"),
                    "category": "style",
                    "message": error_el.get("message", ""),
                    "confidence": "medium",
                }
            )
    return protos


PARSERS = {
    "gitleaks_json": parse_gitleaks,
    "osv_json": parse_osv,
    "semgrep_json": parse_semgrep,
    "astgrep_json": parse_astgrep,
    "pmd_json": parse_pmd,
}


def _normalize_message(message: object) -> str:
    return " ".join(str(message or "").split())


def _mask_secret(value: object) -> str:
    """Truncate a secret-like value into a length-preserving mask."""

    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def _fingerprint(path: object, start: int, end: int, category: object, rule_family: object, message: object) -> str:
    material = "|".join(
        (
            str(path),
            str(start),
            str(end),
            str(category),
            str(rule_family or category),
            _normalize_message(message),
        )
    )
    return crp_common.sha256_text(material)


def _line_in_ranges(line: int, ranges: object) -> bool:
    for item in ranges or []:
        if not isinstance(item, dict):
            continue
        if item.get("full_file") is True:
            return True
        start = item.get("start")
        end = item.get("end")
        if isinstance(start, int) and isinstance(end, int) and start <= line <= end:
            return True
    return False


def attribute(proto: dict[str, object], facts: dict[str, object]) -> tuple[bool, bool]:
    """Return (in_diff, attributable) for one proto-finding.

    A finding is attributable only when it is in a changed/untracked file AND
    (for line findings) the line falls inside the diff ranges. Repository
    history findings (unchanged files or unchanged lines) do not block.
    """

    path = str(proto.get("path") or "")
    changed = set(facts.get("changed_files") or []) | set(facts.get("untracked_files") or [])
    if path not in changed:
        return False, False
    start = _to_int(proto.get("start_line"), 0)
    if start <= 0:
        # Line-less finding (e.g. a dependency vulnerability): attribute by file.
        return True, True
    ranges = (facts.get("diff_ranges") or {}).get(path) or []
    in_diff = _line_in_ranges(start, ranges)
    return in_diff, in_diff


def finalize_finding(proto: dict[str, object], facts: dict[str, object]) -> dict[str, object]:
    """Normalize a proto-finding into the unified finding schema."""

    if "attributable" in proto:
        attributable = bool(proto.get("attributable"))
        in_diff = bool(proto.get("in_diff", attributable))
    else:
        in_diff, attributable = attribute(proto, facts)
    path = str(proto.get("path") or "")
    start = _to_int(proto.get("start_line"), 0)
    end = _to_int(proto.get("end_line"), 0)
    category = proto.get("category") or "unknown"
    message = str(proto.get("message") or "")
    fingerprint = _fingerprint(
        path, start, end, category, proto.get("rule_family"), message
    )
    return {
        "id": "F" + fingerprint[:12],
        "source": proto.get("source"),
        "rule_id": proto.get("rule_id"),
        "path": path,
        "start_line": start,
        "end_line": end,
        "severity_raw": proto.get("severity_raw"),
        "severity": map_severity(str(proto.get("source") or ""), proto.get("severity_raw")),
        "category": category,
        "message": message,
        "confidence": proto.get("confidence") or "medium",
        "in_diff": in_diff,
        "attributable": attributable,
        "fingerprint": fingerprint,
    }


def _finding_sort_key(finding: dict[str, object]) -> tuple[object, ...]:
    severity = str(finding.get("severity") or "INFO")
    rank = SEVERITY_RANK.get(severity, 0)
    return (
        -rank,
        str(finding.get("path") or ""),
        _to_int(finding.get("start_line"), 0),
        str(finding.get("source") or ""),
    )


def deduplicate(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge findings that share a fingerprint.

    Sources are combined and the merged severity takes the highest rank across
    sources; all other fields are kept from the first-seen finding.
    """

    merged: dict[str, dict[str, object]] = {}
    for finding in findings:
        key = str(finding.get("fingerprint") or "")
        if key not in merged:
            merged[key] = dict(finding)
            merged[key]["sources"] = [str(finding.get("source") or "")]
            continue
        current = merged[key]
        current_rank = SEVERITY_RANK.get(str(current.get("severity") or "INFO"), 0)
        incoming_rank = SEVERITY_RANK.get(str(finding.get("severity") or "INFO"), 0)
        if incoming_rank > current_rank:
            current["severity"] = finding["severity"]
        sources = current.setdefault("sources", [])
        if finding.get("source") not in sources:
            sources.append(str(finding.get("source") or ""))
    result = list(merged.values())
    for finding in result:
        finding["sources"] = sorted(set(finding.get("sources") or []))
    return sorted(result, key=_finding_sort_key)


def security_candidate_findings(facts: dict[str, object]) -> list[dict[str, object]]:
    """Turn change-facts ``security_candidate`` evidence into secret-like candidates."""

    candidate = facts.get("security_candidate") or {}
    evidence = candidate.get("evidence") if isinstance(candidate, dict) else None
    protos: list[dict[str, object]] = []
    for entry in evidence or []:
        if not isinstance(entry, dict):
            continue
        line = _to_int(entry.get("line"), 0)
        protos.append(
            {
                "source": "heuristic",
                "rule_id": "security_candidate",
                "path": entry.get("file", ""),
                "start_line": line,
                "end_line": line,
                "severity_raw": None,
                "category": "security",
                "message": (
                    f"secret-like pattern (rule=security_candidate, "
                    f"{entry.get('file', '')}:{line}, masked={_mask_secret(entry.get('match'))})"
                ),
                "confidence": "low",
            }
        )
    return protos


def verification_failure_findings(records: object) -> list[dict[str, object]]:
    """Turn verification records with a non-zero exit_code into build/test blockers."""

    protos: list[dict[str, object]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        code = record.get("exit_code")
        if not (isinstance(code, int) and not isinstance(code, bool) and code != 0):
            continue
        command = str(record.get("command") or "")
        category = "test" if _is_test_command(command) else "build"
        protos.append(
            {
                "source": "verification",
                "rule_id": "verification_failure",
                "path": "",
                "start_line": 0,
                "end_line": 0,
                "severity_raw": str(code),
                "category": category,
                "message": f"verification failed: {command} (exit {code})",
                "confidence": "high",
                "attributable": True,
                "in_diff": True,
            }
        )
    return protos


def _is_test_command(command: str) -> bool:
    """Classify a verification command as a test command by whole-word match.

    ``pytest``/``unittest`` are matched directly; ``test`` must appear as a word
    (``mvn test`` / ``gradle test`` / ``go test``). Substrings like ``inspect``,
    ``respect``, or ``spectral`` must not be classified as test.
    """

    lower = command.lower()
    if "pytest" in lower or "unittest" in lower:
        return True
    return bool(re.search(r"\btest\b", lower))


def _analyzer_hard_failure_proto(name: str, reason: str) -> dict[str, object]:
    return {
        "source": "project-analyzer",
        "rule_id": "analyzer_hard_failure",
        "path": "",
        "start_line": 0,
        "end_line": 0,
        "severity_raw": None,
        "category": "build",
        "message": f"project-configured analyzer {name} failed: {reason}",
        "confidence": "high",
        "attributable": True,
        "in_diff": True,
    }


FOCUS_ON = (
    "behavior correctness",
    "state transitions",
    "data consistency",
    "transaction semantics",
    "concurrency",
    "compatibility",
    "cross-file contract",
    "failure path",
    "test adequacy",
)

DO_NOT_SPEND_BUDGET_ON = (
    "formatting",
    "unused imports",
    "analyzer-covered mechanical patterns",
)


def build_machine_coverage(analyzer_results: list[dict[str, object]]) -> dict[str, object]:
    """Negative coverage: clean/skipped/failed/unsupported plus FOCUS ON."""

    clean = [
        str(result["name"])
        for result in analyzer_results
        if result.get("state") == "ran" and _to_int(result.get("findings_count"), 0) == 0
    ]
    skipped = [
        {"name": str(result["name"]), "reason": result.get("reason")}
        for result in analyzer_results
        if result.get("state") == "skipped"
    ]
    failed = [
        {"name": str(result["name"]), "reason": result.get("reason")}
        for result in analyzer_results
        if result.get("state") == "failed"
    ]
    unsupported = [
        {"name": str(result["name"]), "reason": result.get("reason")}
        for result in analyzer_results
        if result.get("state") == "unsupported"
    ]
    return {
        "clean": clean,
        "skipped": skipped,
        "failed": failed,
        "unsupported": unsupported,
        "focus_on": list(FOCUS_ON),
        "do_not_spend_budget_on": list(DO_NOT_SPEND_BUDGET_ON),
    }


def _char_count(root: Path, rel: object) -> int:
    target = Path(str(rel))
    if not target.is_absolute():
        target = root / target
    try:
        if target.is_file():
            return len(target.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return 0
    return 0


def _pack_tier(root: Path, tier: str, files: list[str], budget: int, reason: str) -> dict[str, object]:
    included: list[str] = []
    omitted: list[str] = []
    total = 0
    for rel in files:
        size = _char_count(root, rel)
        if total + size <= budget:
            included.append(rel)
            total += size
        else:
            omitted.append(rel)
    return {
        "tier": tier,
        "files": included,
        "estimated_chars": total,
        "omitted": omitted,
        "reason": reason,
    }


def pack_review_context(
    repo_root: object,
    facts: dict[str, object],
    task_facts_path: object = None,
    verification_path: object = None,
    findings: list[dict[str, object]] | None = None,
    facts_path: object = None,
) -> dict[str, object]:
    """Pack a P0-P3 review context with an observable char budget."""

    root = Path(str(repo_root))
    changed = list(facts.get("changed_files") or []) + list(facts.get("untracked_files") or [])
    changed = sorted(set(str(path) for path in changed))

    p0_files = [str(path) for path in (facts_path, task_facts_path, verification_path) if path]
    machine_count = len(findings or [])
    p0 = _pack_tier(
        root,
        "P0",
        p0_files,
        40_000,
        f"task contract / current diff / change facts / verification evidence "
        f"({machine_count} machine finding(s))",
    )
    p1 = _pack_tier(
        root,
        "P1",
        changed,
        100_000,
        "changed files (function/class context)",
    )
    p2 = _pack_tier(
        root,
        "P2",
        [],
        40_000,
        "direct callers/callees/interfaces: only if risk requires a symbol index (none available)",
    )
    p3 = _pack_tier(
        root,
        "P3",
        [],
        40_000,
        "broader repository search: only on reviewer evidence request",
    )
    tiers = [p0, p1, p2, p3]
    return {
        "tiers": tiers,
        "total_estimated_chars": sum(_to_int(tier["estimated_chars"], 0) for tier in tiers),
    }


def detect_ocr(which_fn=shutil.which) -> dict[str, object]:
    """Detect the optional ``ocr`` CLI; missing is SKIPPED, never a STOP."""

    path = which_fn("ocr")
    if not path:
        return {"state": "skipped", "reason": "ocr executable not found"}
    return {"state": "available", "path": path}


# Reuse-before-install analyzer registry (levels 1-3 only; never install).
ANALYZERS: list[dict[str, object]] = [
    {
        "name": "gitleaks",
        "cli": "gitleaks",
        "format": "gitleaks_json",
        "category": "security",
        "applies": "always",
        "run_args": ["detect", "--no-git", "--report-format", "json", "--exit-code", "0", "{files}"],
        "project_markers": [".gitleaks.toml", "gitleaks.toml"],
    },
    {
        "name": "osv-scanner",
        "cli": "osv-scanner",
        "format": "osv_json",
        "category": "dependency",
        "applies": "dependency",
        "run_args": ["--format", "json", "{files}"],
        "project_markers": ["osv-scanner.toml"],
    },
    {
        "name": "semgrep",
        "cli": "semgrep",
        "format": "semgrep_json",
        "category": "correctness",
        "applies": "always",
        "run_args": ["--json", "{files}"],
        "project_markers": [".semgrep.yml", ".semgrep.yaml", "semgrep.yml", "semgrep.yaml"],
    },
    {
        "name": "ast-grep",
        "cli": "ast-grep",
        "format": "astgrep_json",
        "category": "maintainability",
        "applies": "always",
        "run_args": ["scan", "--json", "{files}"],
        "project_markers": ["sgconfig.yml", "sgconfig.yaml"],
    },
    {
        "name": "pmd",
        "cli": "pmd",
        "format": "pmd_json",
        "category": "correctness",
        "applies": "java",
        "run_args": ["check", "-f", "json", "{files}"],
        "project_markers": ["pmd.xml", "ruleset.xml", "pmd-ruleset.xml"],
    },
    {
        "name": "checkstyle",
        "cli": "checkstyle",
        "format": "checkstyle_xml",
        "category": "style",
        "applies": "java",
        "run_args": ["{files}"],
        "project_markers": ["checkstyle.xml", "checkstyle.yaml", "google_checks.xml", "sun_checks.xml"],
    },
]

UNSUPPORTED_ANALYZER_CLIS = ("reviewdog", "sonar-scanner", "sonarqube-scanner")

# Java analyzers with no standalone CLI to invoke (Maven/Gradle-integrated
# only). They are always reported as unsupported so negative coverage honestly
# shows they were never machine-run; reuse via existing CI/report is not
# implemented here.
BUILD_INTEGRATED_ANALYZERS = ("spotbugs", "archunit", "error-prone", "p3c", "sonar")

# Java analyzers whose CLIs require project-specific required arguments
# (ruleset/config paths) that this adapter never possesses. Detection entries
# and parsers stay registered (existing CI reports can be reused), but the
# run phase is detect-only: always SKIPPED, never auto-run, never a machine
# blocker. Phase 13b documents this contract change.
DETECT_ONLY_ANALYZERS = frozenset({"pmd", "checkstyle"})

_WALK_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".gradle",
    ".venv",
    "venv",
    "target",
    "build",
    "dist",
}

_DEP_MANIFEST_OR_LOCK = {
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "Gemfile",
    "Gemfile.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "composer.json",
    "composer.lock",
}


def _has_project_marker(root: Path, markers: object) -> bool:
    if not markers:
        return False
    marker_set = set(markers)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [name for name in dirnames if name not in _WALK_SKIP_DIRS]
        if marker_set.intersection(filenames):
            return True
    return False


def _applies(cfg: dict[str, object], facts: dict[str, object]) -> bool:
    kind = cfg.get("applies", "always")
    if kind == "dependency":
        return bool(facts.get("dependency_manifest_changed") or facts.get("lockfile_changed"))
    if kind == "java":
        changed = list(facts.get("changed_files") or []) + list(facts.get("untracked_files") or [])
        return any(str(path).lower().endswith(".java") for path in changed)
    return True


def _skip_reason(cfg: dict[str, object]) -> str:
    kind = cfg.get("applies", "always")
    if kind == "dependency":
        return "dependency unchanged"
    if kind == "java":
        return "no Java files changed"
    return "not applicable"


def _run_files(cfg: dict[str, object], facts: dict[str, object]) -> list[str]:
    changed = list(facts.get("changed_files") or []) + list(facts.get("untracked_files") or [])
    kind = cfg.get("applies", "always")
    if kind == "dependency":
        return [str(path) for path in changed if Path(str(path)).name in _DEP_MANIFEST_OR_LOCK]
    if kind == "java":
        return [str(path) for path in changed if str(path).lower().endswith(".java")]
    return [str(path) for path in changed]


def _run_analyzer(
    cfg: dict[str, object],
    cli_path: str,
    repo_root: Path,
    files: list[str],
) -> tuple[str | None, str | None]:
    argv = [cli_path]
    for token in cfg.get("run_args") or []:
        if token == "{repo}":
            argv.append(str(repo_root))
        elif token == "{files}":
            argv.extend(files)
        else:
            argv.append(token)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except FileNotFoundError:
        return None, "executable not found"
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if proc.returncode != 0:
        return None, f"exit code {proc.returncode}"
    return proc.stdout, None


def normalize_output(cfg: dict[str, object], text: str | None) -> tuple[list[dict[str, object]], str | None]:
    """Parse analyzer output; returns ``(findings, error)``.

    A non-zero analyzer exit code is handled by the caller; here a non-parseable
    successful output is reported as an ``error`` string so the analyzer is
    marked ``failed`` without aborting the preflight.
    """

    fmt = cfg.get("format")
    if fmt == "checkstyle_xml":
        return parse_checkstyle(text or ""), None
    if not text or not text.strip():
        return [], None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        return [], f"invalid JSON output: {error}"
    parser = PARSERS.get(str(fmt))
    if parser is None:
        return [], None
    return parser(data), None


def run_preflight(
    repo: object,
    facts: dict[str, object],
    verification: object = None,
    task_facts_path: object = None,
    verification_path: object = None,
    facts_path: object = None,
    which_fn=shutil.which,
) -> dict[str, object]:
    root = Path(str(repo))
    proto_findings: list[dict[str, object]] = []
    proto_findings.extend(security_candidate_findings(facts))
    proto_findings.extend(verification_failure_findings(verification))

    analyzer_results: list[dict[str, object]] = []
    for cfg in ANALYZERS:
        name = str(cfg["name"])
        if name in DETECT_ONLY_ANALYZERS:
            analyzer_results.append(
                {
                    "name": name,
                    "state": "skipped",
                    "reason": "detect_only / missing_required_args",
                    "findings_count": 0,
                }
            )
            continue
        cli_path = which_fn(str(cfg["cli"]))
        project_configured = _has_project_marker(root, cfg.get("project_markers"))
        if cli_path is None:
            reason = (
                "project configured but executable not found"
                if project_configured
                else "executable not found on PATH"
            )
            analyzer_results.append(
                {"name": name, "state": "skipped", "reason": reason, "findings_count": 0}
            )
            continue
        if not _applies(cfg, facts):
            analyzer_results.append(
                {"name": name, "state": "skipped", "reason": _skip_reason(cfg), "findings_count": 0}
            )
            continue
        files = _run_files(cfg, facts)
        if not files:
            analyzer_results.append(
                {"name": name, "state": "skipped", "reason": "no changed files", "findings_count": 0}
            )
            continue
        output, error = _run_analyzer(cfg, cli_path, root, files)
        if error is not None:
            if project_configured:
                proto_findings.append(_analyzer_hard_failure_proto(name, error))
            analyzer_results.append(
                {"name": name, "state": "failed", "reason": error, "findings_count": 0}
            )
            continue
        protos, parse_error = normalize_output(cfg, output)
        if parse_error is not None:
            analyzer_results.append(
                {"name": name, "state": "failed", "reason": parse_error, "findings_count": 0}
            )
            continue
        proto_findings.extend(protos)
        analyzer_results.append(
            {"name": name, "state": "ran", "reason": None, "findings_count": len(protos)}
        )

    for cli in sorted(UNSUPPORTED_ANALYZER_CLIS):
        if which_fn(cli):
            analyzer_results.append(
                {"name": cli, "state": "unsupported", "reason": "no adapter for this analyzer", "findings_count": 0}
            )

    for name in BUILD_INTEGRATED_ANALYZERS:
        analyzer_results.append(
            {
                "name": name,
                "state": "unsupported",
                "reason": "build-integrated analyzer (Maven/Gradle); reuse via CI/report not implemented",
                "findings_count": 0,
            }
        )

    findings = deduplicate([finalize_finding(proto, facts) for proto in proto_findings])
    coverage = build_machine_coverage(analyzer_results)
    context = pack_review_context(
        root,
        facts,
        facts_path=facts_path,
        task_facts_path=task_facts_path,
        verification_path=verification_path,
        findings=findings,
    )
    ocr = detect_ocr(which_fn)
    if ocr["state"] == "available":
        ocr = _enrich_ocr(ocr, root, facts)

    return {
        "repo_root": str(root),
        "analyzers": analyzer_results,
        "findings": findings,
        "machine_coverage": coverage,
        "review_context": context,
        "ocr": ocr,
        "generated_at": crp_common.utc_timestamp(),
    }


def _enrich_ocr(ocr: dict[str, object], root: Path, facts: dict[str, object]) -> dict[str, object]:
    changed = list(facts.get("changed_files") or []) + list(facts.get("untracked_files") or [])
    try:
        proc = subprocess.run(
            [str(ocr["path"]), "delegate", "rule", *[str(path) for path in changed]],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "skipped", "reason": "ocr delegate rule failed"}
    if proc.returncode != 0:
        return {"state": "skipped", "reason": f"ocr delegate rule failed (exit {proc.returncode})"}
    ocr["rule_context"] = proc.stdout[:40_000]
    return ocr


def _load_facts(path: str | None) -> dict[str, object]:
    if path is None:
        return {}
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrpError("invalid_input", "change facts file not found", path=str(target)) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError(
            "invalid_input",
            "change facts file is not valid JSON",
            path=str(target),
            error=str(error),
        ) from error
    if not isinstance(data, dict):
        raise CrpError("invalid_input", "change facts must be a JSON object", path=str(target))
    return data


def _load_optional_json(path: str | None, label: str) -> dict[str, object] | None:
    if path is None:
        return None
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrpError("invalid_input", f"{label} file not found", path=str(target)) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError(
            "invalid_input",
            f"{label} file is not valid JSON",
            path=str(target),
            error=str(error),
        ) from error
    return data


def _load_verification(path: str | None) -> object:
    if path is None:
        return None
    data = _load_optional_json(path, "verification")
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"]
    if isinstance(data, list):
        return data
    raise CrpError("invalid_input", "verification must be a list or {\"records\": [...]}", path=path)


class _CrpArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit_error(CrpError("invalid_input", "invalid arguments", detail=message))
        raise SystemExit(exit_code("invalid_input"))


def main(argv: list[str] | None = None) -> int:
    reconfigure_stdio()
    parser = _CrpArgumentParser(
        prog="review_preflight.py",
        description="Deterministic review preflight: detect/run/normalize/dedup analyzers and pack review context.",
    )
    parser.add_argument("--repo", default=None, help="repository path (default: current directory)")
    parser.add_argument("--facts", default=None, help="change facts JSON file")
    parser.add_argument("--task-facts", default=None, help="task facts JSON file")
    parser.add_argument("--verification", default=None, help="verification records JSON file")
    parser.add_argument("--out", default=None, help="write the result JSON to this path")
    parser.add_argument(
        "--out-index",
        default=None,
        help="write the full package to this path and print a compact index instead of the full JSON",
    )
    args = parser.parse_args(argv)
    try:
        root = crp_common.repo_root(args.repo)
        facts = _load_facts(args.facts)
        # task-facts content is not consumed by the preflight; validate the path
        # exists and is valid JSON, and pass only its path for P0 packing.
        _load_optional_json(args.task_facts, "task facts")
        verification = _load_verification(args.verification)
        result = run_preflight(
            root,
            facts,
            verification=verification,
            task_facts_path=args.task_facts,
            verification_path=args.verification,
            facts_path=args.facts,
        )
        if args.out:
            crp_common.atomic_json_write(args.out, result)
        if args.out_index:
            crp_common.atomic_json_write(args.out_index, result)
            findings = result.get("findings") or []
            severity_counts: dict[str, int] = {}
            for finding in findings:
                severity = str(finding.get("severity", "?"))
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
            machine = result.get("machine_coverage") or {}
            index = {
                "package_path": str(Path(args.out_index).resolve()),
                "finding_total": len(findings),
                "severity_counts": severity_counts,
                "focus_on": result.get("focus_on"),
                "machine_blockers": [
                    b for b in (machine.get("blockers") or [])
                ],
                "analyzer_states": {
                    name: (entry or {}).get("state")
                    for name, entry in (machine.get("analyzers") or {}).items()
                },
            }
            print(json.dumps(index, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return EXIT_OK
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")


if __name__ == "__main__":
    sys.exit(main())
