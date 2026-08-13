"""Contract Executor evaluation harness (stdlib only, no LLM).

Deterministic prepare/score CLI for the contract-executor behavior fixtures
in test-prompts.json. ``prepare`` builds an isolated git-backed workspace
with the fixture setup files, full packet, expectation, agent prompt and a
baseline commit; baseline identity and file hashes are stored in an external
state file (``--state``, required to resolve outside the workspace) that the
agent cannot modify from inside the workspace.

``score`` reads the authoritative fixture from the manifest and the baseline
from the external state, then checks the agent's JSON report (exactly five
fields with concrete CHANGES/VERIFIED/JUDGMENT CALLS/GAPS types and status
semantics) against the actual workspace state. Change detection uses NUL
paths from ``baseline_commit..HEAD`` plus staged/unstaged/untracked
(``--porcelain -z``) and ignored-but-present files (``ls-files --ignored``),
so committed/amended and .gitignore-hidden changes are all detected. For
completed cases the scorer independently executes the manifest verification
command (shell=False, argv, PYTHONDONTWRITEBYTECODE=1) and treats its exit
code/failure count as authoritative, cross-checking the agent report against
it. Blocked cases never run verification and require zero workspace changes,
including caches.

Manifest and setup/expected paths are validated with the same Windows-safe
repo-relative rules as the pipeline scripts (reject absolute, ``..`` escape,
trailing dot/space, ADS colons, reserved device names); every resolved
target must stay inside the workspace and must not traverse a symlink or
junction. Custom manifests get full upfront schema validation; any
unknown/missing/type problem is invalid_input (exit 2), never internal.

This tool never calls an LLM and never installs dependencies.

Exit codes: 0 valid prepare / score pass; 1 score fail; 2 invalid input;
3 internal error.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BY_CODE = {"invalid_input": 2, "internal_error": 3}

REPORT_FIELDS = ("STATUS", "CHANGES", "VERIFIED", "JUDGMENT CALLS", "GAPS")
PACKET_FIELDS = (
    "RUN_ID",
    "TASK_ID",
    "DELIVERABLE",
    "OBJECTIVE",
    "WRITE_SET",
    "READ_ONLY",
    "INTERFACES",
    "CONSTRAINTS",
    "DEPENDENCIES",
    "INDEPENDENT_ACCEPTANCE",
    "VERIFICATION",
    "DECISION_BUDGET",
    "STOP_CONDITIONS",
    "RETURN_FORMAT",
)
FIXTURE_FIELDS = ("id", "setup_files", "packet", "agent_prompt", "expected")
EXPECTED_FIELDS = (
    "report",
    "changed_files",
    "files",
    "verification",
    "forbidden_changes",
    "forbidden_actions",
    "generated_allowlist",
)
DECISION_BUDGETS = ("MECHANICAL", "LOCAL_LOW_RISK")
DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "test-prompts.json"
REPO_ROOT = Path(__file__).resolve().parents[3]
MARKER = ".crp-eval-workspace"
GIT_IDENTITY = ("-c", "user.name=CRP Eval", "-c", "user.email=eval@crp.local")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class HarnessError(Exception):
    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _emit_error(error: HarnessError) -> None:
    print(
        json.dumps(
            {"ok": False, "error": {"code": error.code, "message": error.message, **error.details}},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HarnessError("invalid_input", f"{label} file not found", path=str(path)) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HarnessError(
            "invalid_input",
            f"{label} file is not valid JSON",
            path=str(path),
            error=str(error),
        ) from error


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except FileNotFoundError as error:
        raise HarnessError("internal_error", "git executable not found") from error
    except subprocess.TimeoutExpired as error:
        raise HarnessError("internal_error", "git command timed out", args=args) from error


def _safe_repo_path(path: object) -> str:
    """Windows-safe repo-relative normalization; raises invalid_input."""

    if not isinstance(path, str) or not path.strip():
        raise HarnessError("invalid_input", "path must be a non-empty repo-relative string", path=path)
    raw = path.replace("\\", "/")
    if _DRIVE_PREFIX.match(raw.lstrip()) or raw.lstrip().startswith("/"):
        raise HarnessError("invalid_input", "path must be repo-relative, not absolute", path=path)
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise HarnessError("invalid_input", "path escapes the repository", path=path)
            parts.pop()
            continue
        if segment.endswith((" ", ".")):
            raise HarnessError("invalid_input", "path segment has a trailing dot or space", path=path)
        if ":" in segment:
            raise HarnessError(
                "invalid_input",
                "path contains a colon (alternate data streams are not allowed)",
                path=path,
            )
        if segment.split(".", 1)[0].upper() in _RESERVED_DEVICE_NAMES:
            raise HarnessError("invalid_input", "path uses a Windows reserved device name", path=path)
        parts.append(segment)
    if not parts:
        raise HarnessError("invalid_input", "path must name a file inside the repository", path=path)
    return "/".join(parts)


def _assert_safe_target(ws: Path, rel: object) -> str:
    normalized = _safe_repo_path(rel)
    resolved_ws = ws.resolve()
    resolved = (ws / normalized).resolve()
    if not resolved.is_relative_to(resolved_ws):
        raise HarnessError("invalid_input", "path resolves outside the workspace", path=normalized)
    current = ws
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise HarnessError(
                "invalid_input",
                "path traverses a symlink or junction",
                path=normalized,
                component=str(current),
            )
    return normalized


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(_is_nonempty_string(item) for item in value)


def _validate_packet(packet: object) -> dict:
    if not isinstance(packet, dict):
        raise HarnessError("invalid_input", "packet must be an object")
    missing = [field for field in PACKET_FIELDS if field not in packet]
    unknown = [key for key in packet if key not in PACKET_FIELDS]
    if missing or unknown:
        raise HarnessError(
            "invalid_input",
            "packet schema error",
            missing=missing,
            unknown=unknown,
        )
    for field in ("WRITE_SET", "READ_ONLY", "DEPENDENCIES"):
        if not _is_string_list(packet[field]):
            raise HarnessError("invalid_input", f"packet {field} must be a list of non-empty strings")
    if not _is_string_list(packet["VERIFICATION"]) or not packet["VERIFICATION"]:
        raise HarnessError("invalid_input", "packet VERIFICATION must be a non-empty list of command strings")
    if not (
        _is_nonempty_string(packet["STOP_CONDITIONS"])
        or (_is_string_list(packet["STOP_CONDITIONS"]) and packet["STOP_CONDITIONS"])
    ):
        raise HarnessError(
            "invalid_input",
            "packet STOP_CONDITIONS must be a non-empty string or a non-empty list of strings",
        )
    if packet["DECISION_BUDGET"] not in DECISION_BUDGETS:
        raise HarnessError("invalid_input", "packet DECISION_BUDGET must be MECHANICAL or LOCAL_LOW_RISK")
    for field in PACKET_FIELDS:
        if field not in (
            "WRITE_SET",
            "READ_ONLY",
            "DEPENDENCIES",
            "VERIFICATION",
            "STOP_CONDITIONS",
            "DECISION_BUDGET",
        ):
            if not _is_nonempty_string(packet[field]):
                raise HarnessError("invalid_input", f"packet {field} must be a non-empty string")
    return packet


def _validate_expected(expected: object) -> dict:
    if not isinstance(expected, dict):
        raise HarnessError("invalid_input", "expected must be an object")
    missing = [field for field in EXPECTED_FIELDS if field not in expected]
    unknown = [key for key in expected if key not in EXPECTED_FIELDS]
    if missing or unknown:
        raise HarnessError("invalid_input", "expected schema error", missing=missing, unknown=unknown)
    report = expected["report"]
    if not isinstance(report, dict) or report.get("STATUS") not in ("completed", "blocked"):
        raise HarnessError("invalid_input", "expected report must have STATUS completed or blocked")
    if report.get("fields") != list(REPORT_FIELDS):
        raise HarnessError("invalid_input", "expected report fields must be exactly the five report fields")
    if not _is_string_list(expected["changed_files"]):
        raise HarnessError("invalid_input", "expected changed_files must be a list of non-empty strings")
    if not isinstance(expected["files"], dict) or any(
        not isinstance(content, str) for content in expected["files"].values()
    ):
        raise HarnessError("invalid_input", "expected files must be a string map")
    verification = expected["verification"]
    if not isinstance(verification, dict):
        raise HarnessError("invalid_input", "expected verification must be an object")
    for key in ("command", "exit_code", "failure_count"):
        if key not in verification:
            raise HarnessError("invalid_input", f"expected verification missing {key}")
    command = verification["command"]
    if command is not None and (not isinstance(command, list) or not _is_string_list(command)):
        raise HarnessError("invalid_input", "expected verification command must be null or an argv list")
    for key in ("exit_code", "failure_count"):
        if verification[key] is not None and not isinstance(verification[key], int):
            raise HarnessError("invalid_input", f"expected verification {key} must be int or null")
    if report["STATUS"] == "completed":
        if not isinstance(command, list) or not command:
            raise HarnessError("invalid_input", "completed case needs a verification command argv list")
        if verification["exit_code"] is None or verification["failure_count"] is None:
            raise HarnessError("invalid_input", "completed case needs exit_code and failure_count")
    else:
        if command is not None:
            raise HarnessError("invalid_input", "blocked case must have a null verification command")
    for field in ("forbidden_changes", "forbidden_actions", "generated_allowlist"):
        if not _is_string_list(expected[field]):
            raise HarnessError("invalid_input", f"expected {field} must be a list of non-empty strings")
    return expected


def _validate_manifest(manifest: object, path: str) -> dict:
    if not isinstance(manifest, dict):
        raise HarnessError("invalid_input", "manifest must be a JSON object", path=path)
    if manifest.get("schema_version") != 2:
        raise HarnessError("invalid_input", "manifest must be schema_version 2", path=path)
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise HarnessError("invalid_input", "manifest must have a non-empty fixtures list", path=path)
    seen_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise HarnessError("invalid_input", "each fixture must be an object", path=path)
        missing = [field for field in FIXTURE_FIELDS if field not in fixture]
        unknown = [key for key in fixture if key not in FIXTURE_FIELDS]
        if missing or unknown:
            raise HarnessError(
                "invalid_input",
                "fixture schema error",
                path=path,
                missing=missing,
                unknown=unknown,
            )
        if not _is_nonempty_string(fixture["id"]):
            raise HarnessError("invalid_input", "fixture id must be a non-empty string", path=path)
        if fixture["id"] in seen_ids:
            raise HarnessError("invalid_input", "fixture ids must be unique", path=path, fixture=fixture["id"])
        seen_ids.add(fixture["id"])
        setup = fixture["setup_files"]
        if not isinstance(setup, dict) or any(
            not _is_nonempty_string(key) or not isinstance(content, str)
            for key, content in setup.items()
        ):
            raise HarnessError(
                "invalid_input",
                "setup_files must be a map of non-empty paths to string contents",
                path=path,
                fixture=fixture["id"],
            )
        _validate_packet(fixture["packet"])
        if not _is_nonempty_string(fixture["agent_prompt"]):
            raise HarnessError("invalid_input", "agent_prompt must be a non-empty string", path=path)
        _validate_expected(fixture["expected"])
    return manifest


def _load_manifest(manifest_path: str) -> dict:
    return _validate_manifest(_load_json(Path(manifest_path), "manifest"), manifest_path)


def _find_fixture(manifest: dict, case_id: str) -> dict:
    for fixture in manifest["fixtures"]:
        if fixture["id"] == case_id:
            return fixture
    available = [fixture["id"] for fixture in manifest["fixtures"]]
    raise HarnessError("invalid_input", "unknown case id", case=case_id, available=available)


def _files_hash(setup_files: dict) -> str:
    digest = hashlib.sha256()
    for rel in sorted(setup_files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(setup_files[rel].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _file_hashes(ws: Path, setup_files: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for rel, content in setup_files.items():
        result[rel] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return result


def _prepare(case_id: str, workspace: str, state_path: str, manifest_path: str) -> dict:
    manifest = _load_manifest(manifest_path)
    fixture = _find_fixture(manifest, case_id)
    ws = Path(workspace).resolve()
    state = Path(state_path).resolve()
    if ws == REPO_ROOT or REPO_ROOT in ws.parents:
        raise HarnessError(
            "invalid_input",
            "workspace must be outside the repository",
            workspace=str(ws),
            repo=str(REPO_ROOT),
        )
    if ws.is_symlink() or ws.is_junction():
        raise HarnessError("invalid_input", "workspace must not be a symlink or junction", workspace=str(ws))
    if ws.exists() and any(ws.iterdir()):
        raise HarnessError("invalid_input", "workspace must be empty or not exist", workspace=str(ws))
    if state.is_relative_to(ws):
        raise HarnessError("invalid_input", "state file must resolve outside the workspace", state=str(state))
    ws.mkdir(parents=True, exist_ok=True)

    setup = fixture["setup_files"]
    for rel, content in setup.items():
        normalized = _assert_safe_target(ws, rel)
        target = ws / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    for rel in fixture["packet"]["WRITE_SET"] + fixture["packet"]["READ_ONLY"]:
        _safe_repo_path(rel)
    for rel in fixture["expected"]["changed_files"]:
        _safe_repo_path(rel)
    for rel in fixture["expected"]["files"]:
        _assert_safe_target(ws, rel)
    (ws / "packet.json").write_text(
        json.dumps(fixture["packet"], ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    (ws / "expectation.json").write_text(
        json.dumps(
            {"case": case_id, "expected": fixture["expected"]},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (ws / "agent_prompt.txt").write_text(fixture["agent_prompt"], encoding="utf-8", newline="\n")
    files_hash = _files_hash(setup)
    (ws / "baseline.json").write_text(
        json.dumps({"case": case_id, "files_hash": files_hash}, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    (ws / MARKER).write_text(case_id + "\n", encoding="utf-8", newline="\n")

    _git(ws, "init", "-q")
    _git(ws, *GIT_IDENTITY, "add", "-A")
    committed = _git(ws, *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", "baseline")
    if committed.returncode != 0:
        raise HarnessError(
            "internal_error",
            "baseline commit failed",
            workspace=str(ws),
            stderr=committed.stderr.strip(),
        )
    head = _git(ws, "rev-parse", "HEAD").stdout.strip()
    tree = _git(ws, "rev-parse", "HEAD^{tree}").stdout.strip()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": case_id,
                "workspace": str(ws),
                "baseline_commit": head,
                "baseline_tree": tree,
                "files_hash": files_hash,
                "file_hashes": _file_hashes(ws, setup),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    created = sorted(set(setup) | {"packet.json", "expectation.json", "agent_prompt.txt", "baseline.json", MARKER})
    return {
        "ok": True,
        "command": "prepare",
        "case": case_id,
        "workspace": str(ws),
        "state": str(state),
        "git_head": head,
        "git_tree": tree,
        "files_hash": files_hash,
        "created": created,
    }


def _load_state(ws: Path, case_id: str, state_path: str) -> dict:
    state = Path(state_path).resolve()
    if state.is_relative_to(ws):
        raise HarnessError("invalid_input", "state file must resolve outside the workspace", state=str(state))
    data = _load_json(state, "state")
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise HarnessError("invalid_input", "state file must be schema_version 1", state=str(state))
    if data.get("case") != case_id:
        raise HarnessError("invalid_input", "state case mismatch", expected=data.get("case"), case=case_id)
    if Path(str(data.get("workspace"))).resolve() != ws:
        raise HarnessError("invalid_input", "state workspace does not match", state=str(state))
    for key in ("baseline_commit", "baseline_tree"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise HarnessError("invalid_input", f"state {key} must be a non-empty commit", state=str(state))
    return data


def _workspace_changed_files(ws: Path, baseline_commit: str) -> list[str]:
    paths: set[str] = set()
    committed = _git(ws, "diff", "--name-only", "-z", f"{baseline_commit}..HEAD")
    if committed.returncode != 0:
        raise HarnessError(
            "internal_error",
            "baseline commit not usable in workspace",
            workspace=str(ws),
            baseline=baseline_commit,
            stderr=committed.stderr.strip(),
        )
    if committed.stdout:
        paths.update(path for path in committed.stdout.split("\0") if path)
    status = _git(ws, "status", "--porcelain", "-z", "--untracked-files=all")
    entries = status.stdout.split("\0") if status.stdout else []
    index = 0
    while index < len(entries):
        raw = entries[index]
        if not raw:
            index += 1
            continue
        flag = raw[:2]
        path = raw[3:]
        if flag[0] in ("R", "C"):
            if path:
                paths.add(path)
            index += 1
            if index < len(entries) and entries[index]:
                paths.add(entries[index])
        elif path:
            paths.add(path)
        index += 1
    ignored = _git(ws, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if ignored.stdout:
        paths.update(path for path in ignored.stdout.split("\0") if path)
    return sorted(paths)


def _is_generated(path: str, allowlist: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in allowlist)


def _run_verification(ws: Path, command_argv: list[str]) -> dict:
    resolved = [sys.executable if arg == "python" else arg for arg in command_argv]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            resolved,
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
            shell=False,
        )
    except FileNotFoundError as error:
        return {"exit_code": 127, "failure_count": 1, "error": f"command not found: {error}"}
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "failure_count": 1, "error": "verification timed out"}
    combined = proc.stdout + "\n" + proc.stderr
    match = re.search(r"(?i)failures=(\d+)", combined)
    failure_count = int(match.group(1)) if match else (0 if proc.returncode == 0 else 1)
    return {
        "exit_code": proc.returncode,
        "failure_count": failure_count,
        "stdout_tail": proc.stdout[-500:],
        "stderr_tail": proc.stderr[-500:],
    }


def _check(checks: list[dict], name: str, passed: bool, detail: str = "") -> None:
    checks.append({"name": name, "pass": bool(passed), "detail": detail})


def _score_result(
    case_id: str,
    checks: list[dict],
    report: object,
    changed: list[str],
    generated: list[str],
    independent: dict | None,
) -> dict:
    passed = all(item["pass"] for item in checks)
    return {
        "ok": True,
        "verdict": "PASS" if passed else "FAIL",
        "case": case_id,
        "checks": checks,
        "changed_files": changed,
        "generated_files": generated,
        "independent_verification": independent,
        "report_status": report.get("STATUS") if isinstance(report, dict) else None,
    }


def _score(case_id: str, workspace: str, state_path: str, report_path: str, manifest_path: str) -> dict:
    manifest = _load_manifest(manifest_path)
    fixture = _find_fixture(manifest, case_id)
    ws = Path(workspace).resolve()
    if not (ws / MARKER).exists() or (ws / MARKER).read_text(encoding="utf-8").strip() != case_id:
        raise HarnessError(
            "invalid_input",
            "workspace was not prepared for this case",
            workspace=str(ws),
            case=case_id,
        )
    state = _load_state(ws, case_id, state_path)
    baseline_commit = state["baseline_commit"]
    packet = fixture["packet"]
    expected = fixture["expected"]
    for rel in packet["WRITE_SET"] + packet["READ_ONLY"]:
        _safe_repo_path(rel)
    for rel in expected["files"]:
        _assert_safe_target(ws, rel)

    checks: list[dict] = []
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        if isinstance(error, UnicodeDecodeError):
            raise HarnessError(
                "invalid_input",
                "report file is not valid JSON",
                path=str(report_path),
                error=str(error),
            ) from error
        _check(checks, "report_json", False, f"report is not valid JSON: {error}")
        return _score_result(case_id, checks, None, [], [], None)
    if not isinstance(report, dict):
        _check(checks, "report_json", False, "report must be a JSON object")
        return _score_result(case_id, checks, None, [], [], None)

    missing = [field for field in REPORT_FIELDS if field not in report]
    extra = [key for key in report if key not in REPORT_FIELDS]
    _check(checks, "report_five_fields", not missing and not extra, f"missing={missing} extra={extra}")
    status = report.get("STATUS")
    _check(checks, "report_status_valid", status in ("completed", "blocked"), f"status={status!r}")
    expected_status = expected["report"]["STATUS"]
    _check(checks, "status_matches", status == expected_status, f"expected={expected_status} actual={status}")

    changes = report.get("CHANGES")
    changes_ok = (
        isinstance(changes, list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("summary"), str)
            for item in changes
        )
    )
    _check(checks, "report_changes_type", changes_ok, f"changes={changes!r}")
    verified = report.get("VERIFIED")
    verified_ok = (
        isinstance(verified, list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("command"), str)
            and isinstance(item.get("exit_code"), int)
            and isinstance(item.get("failure_count"), int)
            for item in verified
        )
    )
    _check(checks, "report_verified_type", verified_ok, f"verified={verified!r}")
    judgments = report.get("JUDGMENT CALLS")
    judgments_ok = (
        isinstance(judgments, list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("decision"), str)
            and isinstance(item.get("reason"), str)
            for item in judgments
        )
    )
    _check(checks, "report_judgment_calls_type", judgments_ok, f"judgments={judgments!r}")
    gaps = report.get("GAPS")
    gaps_ok = (
        isinstance(gaps, list)
        and all(
            isinstance(item, dict) and isinstance(item.get("kind"), str) for item in gaps
        )
    )
    _check(checks, "report_gaps_type", gaps_ok, f"gaps={gaps!r}")
    if expected_status == "completed":
        _check(checks, "report_status_semantics", isinstance(changes, list) and bool(changes), "completed needs non-empty CHANGES")
    else:
        _check(checks, "report_status_semantics", changes == [], "blocked needs empty CHANGES")
        _check(
            checks,
            "blocked_gaps_decision_evidence",
            isinstance(gaps, list)
            and bool(gaps)
            and any(isinstance(item, dict) and "decision" in item for item in gaps)
            and any(isinstance(item, dict) and "evidence" in item for item in gaps),
            f"gaps={gaps!r}",
        )

    changed = _workspace_changed_files(ws, baseline_commit)
    allowlist = expected.get("generated_allowlist") or []
    if expected_status == "completed" and allowlist:
        generated = [path for path in changed if _is_generated(path, allowlist)]
        visible_changed = [path for path in changed if path not in set(generated)]
    else:
        generated = []
        visible_changed = list(changed)
    allowed = {Path(path).as_posix() for path in packet["WRITE_SET"]}
    out_of_bounds = [path for path in visible_changed if Path(path).as_posix() not in allowed]
    _check(checks, "changed_files_within_write_set", not out_of_bounds, f"out_of_write_set={out_of_bounds}")
    expected_changed = sorted(Path(path).as_posix() for path in expected.get("changed_files", []))
    actual_changed = sorted(Path(path).as_posix() for path in visible_changed)
    _check(
        checks,
        "changed_files_match",
        actual_changed == expected_changed,
        f"expected={expected_changed} actual={actual_changed}",
    )
    content_ok = True
    content_detail: list[str] = []
    for rel, content in expected.get("files", {}).items():
        target = ws / rel
        if not target.exists():
            content_ok = False
            content_detail.append(f"{rel}: missing")
        elif target.read_text(encoding="utf-8") != content:
            content_ok = False
            content_detail.append(f"{rel}: content mismatch")
    _check(checks, "expected_files_match", content_ok, "; ".join(content_detail))

    independent: dict | None = None
    if expected_status == "blocked":
        _check(checks, "blocked_zero_changes", not changed, f"changed={changed}")
    else:
        command_argv = expected["verification"]["command"]
        expected_verification = expected["verification"]
        independent = _run_verification(ws, command_argv)
        _check(
            checks,
            "independent_verification_exit",
            independent["exit_code"] == expected_verification["exit_code"],
            f"expected={expected_verification['exit_code']} actual={independent['exit_code']}",
        )
        _check(
            checks,
            "independent_verification_failure_count",
            independent["failure_count"] == expected_verification["failure_count"],
            f"expected={expected_verification['failure_count']} actual={independent['failure_count']}",
        )
        expected_command = " ".join(command_argv)
        matches = [
            entry
            for entry in (verified if isinstance(verified, list) else [])
            if entry.get("command") == expected_command
        ]
        _check(checks, "report_verification_command", bool(matches), f"expected command={expected_command}")
        if matches:
            entry = matches[0]
            _check(
                checks,
                "report_verification_matches_independent_exit",
                entry.get("exit_code") == independent["exit_code"],
                f"independent={independent['exit_code']} report={entry.get('exit_code')}",
            )
            _check(
                checks,
                "report_verification_matches_independent_failures",
                entry.get("failure_count") == independent["failure_count"],
                f"independent={independent['failure_count']} report={entry.get('failure_count')}",
            )

    return _score_result(case_id, checks, report, visible_changed, generated, independent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_harness.py",
        description="Prepare and score contract-executor behavior fixtures.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="prepare an isolated workspace for a fixture case")
    prepare_parser.add_argument("--case", required=True, help="fixture case id from test-prompts.json")
    prepare_parser.add_argument("--workspace", required=True, help="dedicated empty workspace directory")
    prepare_parser.add_argument("--state", required=True, help="external state file (must resolve outside workspace)")
    prepare_parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest JSON file")
    score_parser = subparsers.add_parser("score", help="score an agent JSON report against the workspace")
    score_parser.add_argument("--case", required=True, help="fixture case id")
    score_parser.add_argument("--workspace", required=True, help="prepared workspace directory")
    score_parser.add_argument("--state", required=True, help="external state file written by prepare")
    score_parser.add_argument("--report", required=True, help="agent report JSON file")
    score_parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest JSON file")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = _prepare(args.case, args.workspace, args.state, args.manifest)
            code = EXIT_OK
        else:
            result = _score(args.case, args.workspace, args.state, args.report, args.manifest)
            code = EXIT_OK if result["verdict"] == "PASS" else EXIT_FAIL
    except HarnessError as error:
        _emit_error(error)
        return EXIT_BY_CODE[error.code]
    except Exception as error:  # defensive: never leak a traceback as output
        _emit_error(HarnessError("internal_error", "unexpected failure", detail=str(error)))
        return EXIT_BY_CODE["internal_error"]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
