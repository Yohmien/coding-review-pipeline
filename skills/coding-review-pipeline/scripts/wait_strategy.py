"""Programmatic wait/execution strategy for tests and subagent dispatch.

Two deterministic decisions, no natural-language judgment:

1. Test execution mode: given the number of test files (and optional total
   test count), return one of

       foreground_wait    small suites: run inline and watch output directly
       background_file    medium suites: start a background process, poll the
                          result file
       background_poll    large suites: background process with long-poll and
                          periodic tail of the result file

2. Subagent wait budget: given task count and total files in scope, return a
   single wait_agent timeout in milliseconds plus a polling policy. The point
   is to avoid frequent status probing: the caller issues ONE long wait per
   dispatch round.

CLI reads one JSON object on stdin:

    {"test_files": 12, "test_count": 340}
    {"tasks": 3, "files": 24, "risk": "HIGH"}

risk is one of NORMAL | ELEVATED | HIGH (default NORMAL). Output is a JSON
object; exit codes follow crp_common conventions.
"""

from __future__ import annotations

import json
import sys

import crp_common
from crp_common import CrpError, emit_error, exit_code, reconfigure_stdio

RISKS = ("NORMAL", "ELEVATED", "HIGH")

# Thresholds are deliberately simple and monotonic; they encode the observed
# cost structure from real runs rather than per-case tuning.
SMALL_SUITE_FILES = 8
LARGE_SUITE_FILES = 40
SMALL_SUITE_COUNT = 120
LARGE_SUITE_COUNT = 600

BASE_WAIT_MS = 600_000
PER_TASK_MS = 300_000
PER_FILE_MS = 15_000
RISK_MULTIPLIER = {"NORMAL": 1.0, "ELEVATED": 1.25, "HIGH": 1.5}
MAX_WAIT_MS = 1_800_000
MIN_WAIT_MS = 600_000


def decide_test_mode(test_files: int, test_count: int | None = None) -> dict:
    if not isinstance(test_files, int) or isinstance(test_files, bool) or test_files < 0:
        raise CrpError("invalid_input", "test_files must be a non-negative integer")
    if test_count is not None and (not isinstance(test_count, int) or isinstance(test_count, bool) or test_count < 0):
        raise CrpError("invalid_input", "test_count must be a non-negative integer when present")
    if test_files <= SMALL_SUITE_FILES or (
        test_count is not None and test_count < SMALL_SUITE_COUNT
    ):
        mode = "foreground_wait"
        reason = "small suite: run inline, read stdout directly"
    elif test_files >= LARGE_SUITE_FILES or (
        test_count is not None and test_count > LARGE_SUITE_COUNT
    ):
        mode = "background_poll"
        reason = "large suite: background process, long-poll result file"
    else:
        mode = "background_file"
        reason = "medium suite: background process, read result file on completion"
    return {"mode": mode, "reason": reason}


def decide_wait_ms(tasks: int, files: int, risk: str = "NORMAL") -> dict:
    if not isinstance(tasks, int) or isinstance(tasks, bool) or tasks < 1:
        raise CrpError("invalid_input", "tasks must be a positive integer")
    if not isinstance(files, int) or isinstance(files, bool) or files < 0:
        raise CrpError("invalid_input", "files must be a non-negative integer")
    if risk not in RISKS:
        raise CrpError("invalid_input", f"risk must be one of {RISKS}")
    wait = BASE_WAIT_MS + (tasks - 1) * PER_TASK_MS + files * PER_FILE_MS
    wait = int(wait * RISK_MULTIPLIER[risk])
    wait = max(MIN_WAIT_MS, min(MAX_WAIT_MS, wait))
    return {
        "wait_agent_ms": wait,
        "polling": "single_long_wait",
        "note": "issue one long wait per dispatch round; no intermediate probing",
    }


def main(argv=None):
    reconfigure_stdio()
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise CrpError("invalid_input", "input must be a JSON object")
        if "test_files" in data:
            result = decide_test_mode(data["test_files"], data.get("test_count"))
        elif "tasks" in data:
            result = decide_wait_ms(data["tasks"], data.get("files", 0), data.get("risk", "NORMAL"))
        else:
            raise CrpError("invalid_input", 'need either {"test_files": N} or {"tasks": N}')
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")


if __name__ == "__main__":
    sys.exit(main())
