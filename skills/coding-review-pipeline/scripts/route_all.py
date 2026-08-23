"""Combined routing entry: change facts -> route context -> task graph in one call.

P7: merges the three routing scripts' outputs into a single compact JSON so
the orchestration loop parses one payload instead of three overlapping ones.
Shared fields (write sets, risk candidates, file classification) are emitted
once. Falls back to individual scripts when any stage fails; exit codes follow
crp_common conventions.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import crp_common
from crp_common import CrpError, EXIT_OK, emit_error, exit_code

SCRIPTS = Path(__file__).resolve().parent


def _run_module(name: str, args: list[str]) -> dict:
    """Import a sibling script module and invoke its main() with captured stdout."""

    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    if spec is None or spec.loader is None:
        raise CrpError("internal_error", f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = module.main(args)
    if code not in (0, None):
        raise CrpError("policy_blocked", f"{name} exited {code}", output=buffer.getvalue())
    try:
        return json.loads(buffer.getvalue())
    except json.JSONDecodeError as error:
        raise CrpError("internal_error", f"{name} produced non-JSON output", detail=str(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="route_all.py",
        description="Run change_facts + route_context + task_graph once and emit a merged compact JSON.",
    )
    parser.add_argument("--facts-args", default="", help="space-separated args forwarded to change_facts.py")
    parser.add_argument("--route-args", default="", help="space-separated args forwarded to route_context.py")
    parser.add_argument("--graph-args", default="", help="space-separated args forwarded to task_graph.py")
    parser.add_argument(
        "--facts-out",
        default=None,
        help="optional path to write the change_facts result before routing continues",
    )
    parser.add_argument("--out", default=None, help="optional path to also write the merged JSON")
    args = parser.parse_args(argv)
    try:
        facts = _run_module("change_facts", args.facts_args.split())
        if args.facts_out:
            crp_common.atomic_json_write(args.facts_out, facts)
        route = _run_module("route_context", args.route_args.split())
        graph = _run_module("task_graph", args.graph_args.split())
        merged = {
            "change_facts": facts,
            "routing": {
                key: value for key, value in route.items() if key != "change_facts"
            },
            "task_graph": graph,
        }
        if args.out:
            crp_common.atomic_json_write(args.out, merged)
        print(json.dumps(merged, ensure_ascii=False, sort_keys=True))
        return EXIT_OK
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")


if __name__ == "__main__":
    sys.exit(main())
