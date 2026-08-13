"""Shared deterministic utilities for coding-review-pipeline scripts.

Only truly shared plumbing lives here: UTF-8 JSON read/write, atomic JSON
replace, stable JSON serialization, SHA-256 fingerprints, a git command
wrapper, repo root resolution, path normalization, timestamps, and structured
errors. Routing business logic is intentionally not allowed in this module.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INVALID = 2
EXIT_POLICY = 3

_EXIT_BY_CODE = {
    "internal_error": EXIT_INTERNAL,
    "invalid_input": EXIT_INVALID,
    "policy_blocked": EXIT_POLICY,
    "not_git_repo": EXIT_POLICY,
    "missing_reference": EXIT_POLICY,
    "snapshot_changed": EXIT_POLICY,
}


class CrpError(Exception):
    """Structured error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        if code not in _EXIT_BY_CODE:
            raise ValueError(f"unknown error code: {code}")
        self.code = code
        self.message = message
        self.details = details

    def to_json(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.message, **self.details},
        }


def exit_code(code: str) -> int:
    """Stable nonzero exit code for a structured error code."""

    return _EXIT_BY_CODE[code]


def emit_error(error: CrpError) -> None:
    """Print a structured error JSON document to stderr."""

    print(json.dumps(error.to_json(), ensure_ascii=False, sort_keys=True), file=sys.stderr)


def reconfigure_stdio() -> None:
    """Force UTF-8 on stdout/stderr regardless of the Windows locale."""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def stable_json(data: object) -> str:
    """Deterministic JSON serialization for hashing and writing."""

    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def json_read(path: str | Path) -> object:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def json_write(path: str | Path, data: object) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(stable_json(data))


def atomic_json_write(path: str | Path, data: object) -> None:
    """Write JSON to a sibling temp file, then atomically replace the target."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(stable_json(data))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_json(data: object) -> str:
    return sha256_text(stable_json(data))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run git with UTF-8 decoding; raises CrpError for missing/timeout git."""

    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise CrpError("internal_error", "git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CrpError("internal_error", "git command timed out", args=args) from exc


def run_git_bytes(args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run git with raw bytes (binary-safe); raises CrpError for missing/timeout."""

    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise CrpError("internal_error", "git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise CrpError("internal_error", "git command timed out", args=args) from exc


def repo_root(start: str | Path | None = None) -> Path:
    """Resolve the repository root containing ``start`` (default: cwd)."""

    start_path = Path(start).resolve() if start is not None else Path.cwd().resolve()
    proc = run_git(["rev-parse", "--show-toplevel"], cwd=str(start_path))
    if proc.returncode != 0:
        raise CrpError(
            "not_git_repo",
            "not inside a git repository",
            cwd=str(start_path),
            git_error=proc.stderr.strip(),
        )
    return Path(proc.stdout.strip()).resolve()


def normalize_repo_path(path: str | Path, root: str | Path) -> str:
    """Normalize a path to POSIX form relative to the repo root."""

    root_path = Path(root)
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(root_path)
        except ValueError:
            return str(p)
    return p.as_posix()


def utc_timestamp() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")
