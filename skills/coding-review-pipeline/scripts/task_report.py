"""Programmatic phase-report write/read for coder subagents.

Coders append a compact phase report after finishing a coding stage; the main
session reads the latest report instead of inferring status from large diffs.

Storage: one JSON array per run under the ledger runs directory at
<runs_dir>/<run_id>/phase-report.json. Each entry carries task_id, phase
(READ/IMPLEMENT/VERIFY/REPORT), status (completed/blocked/in_progress), a
bounded summary, and optional list fields.

CLI:
    write   read a JSON object on stdin, validate required fields, append
    read    print the latest report for a task
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import crp_common
from crp_common import CrpError, emit_error, exit_code, reconfigure_stdio
from run_ledger import runs_dir

REQUIRED_FIELDS = ("task_id", "phase", "status", "summary")
OPTIONAL_LIST_FIELDS = ("files_changed", "verification", "judgment_calls", "gaps")
KNOWN_PHASES = ("READ", "IMPLEMENT", "VERIFY", "REPORT")
KNOWN_STATUSES = ("completed", "blocked", "in_progress")
MAX_SUMMARY_CHARS = 2000


def report_path(run_id: str, start=None, codex_home=None) -> Path:
    return Path(runs_dir(start=start, codex_home=codex_home)) / run_id / "phase-report.json"


def load_reports(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError("invalid_input", "existing phase report is not valid JSON", detail=str(error)) from error
    if not isinstance(data, list):
        raise CrpError("invalid_input", "phase report file must be a JSON array")
    return data


def validate_entry(entry: dict) -> None:
    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CrpError("invalid_input", f"missing or invalid required field: {field}")
    if entry["phase"] not in KNOWN_PHASES:
        raise CrpError("invalid_input", f"phase must be one of {KNOWN_PHASES}")
    if entry["status"] not in KNOWN_STATUSES:
        raise CrpError("invalid_input", f"status must be one of {KNOWN_STATUSES}")
    if len(entry["summary"]) > MAX_SUMMARY_CHARS:
        raise CrpError("invalid_input", f"summary exceeds {MAX_SUMMARY_CHARS} chars")
    for list_field in OPTIONAL_LIST_FIELDS:
        value = entry.get(list_field, [])
        if not isinstance(value, list):
            raise CrpError("invalid_input", "{0} must be a list when present".format(list_field))


def write_entry(run_id: str, entry: dict, start=None, codex_home=None) -> dict:
    validate_entry(entry)
    path = report_path(run_id)
    entries = load_reports(path)
    clean = dict((k, v) for k, v in entry.items() if k in REQUIRED_FIELDS or k in OPTIONAL_LIST_FIELDS)
    clean["timestamp"] = crp_common.utc_timestamp()
    entries.append(clean)
    path.parent.mkdir(parents=True, exist_ok=True)
    crp_common.atomic_json_write(str(path), entries)
    return {"ok": True, "written": clean}


def read_latest(run_id: str, task_id: str | None, start=None, codex_home=None) -> dict:
    entries = load_reports(report_path(run_id))
    candidates = [e for e in entries if task_id is None or e.get("task_id") == task_id]
    if not candidates:
        return {"ok": False, "reason": "no_report"}
    latest = candidates[-1]
    return {"ok": True, "report": latest, "entries_for_task": len(candidates)}


def main(argv: list[str] | None = None) -> int:
    reconfigure_stdio()
    parser = argparse.ArgumentParser(prog="task_report.py")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="append a phase report from stdin JSON")
    w.add_argument("--run-id", required=True)
    r = sub.add_parser("read", help="read latest phase report")
    r.add_argument("--run-id", required=True)
    r.add_argument("--task-id", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            raw = sys.stdin.buffer.read().decode("utf-8")
            entry = json.loads(raw)
            if not isinstance(entry, dict):
                raise CrpError("invalid_input", "stdin must be a JSON object")
            result = write_entry(args.run_id, entry)
        else:
            result = read_latest(args.run_id, args.task_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")


if __name__ == "__main__":
    sys.exit(main())
