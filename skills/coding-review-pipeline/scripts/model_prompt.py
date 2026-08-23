"""Programmatic model-selection prompt builder (stage 3).

Reads a live spawn_agent schema snapshot (models + efforts) from JSON, plus
the four recommended fields, and prints the fixed-format selection request.
"""

from __future__ import annotations

import argparse
import json
import sys

import crp_common
from crp_common import CrpError, emit_error, exit_code, reconfigure_stdio

FIELDS = ("coding_model", "coding_effort", "review_model", "review_effort")


def build_prompt(models, recommend):
    if not isinstance(models, list) or not models:
        raise CrpError("invalid_input", "models must be a non-empty list")
    for field in FIELDS:
        if not isinstance(recommend.get(field), str) or not recommend[field].strip():
            raise CrpError("invalid_input", f"recommend.{field} is required")
    lines = [
        "请确认以下四项模型选择（回复编号、按推荐，或直接改写任一项）：",
        "",
    ]
    for idx, field in enumerate(FIELDS, 1):
        lines.append(f"{idx}. {field} = {recommend[field]}")
    lines.append("")
    lines.append("可选范围（live schema 完整枚举）：")
    for m in models:
        mid = m.get("id") if isinstance(m, dict) else None
        efforts = m.get("efforts") if isinstance(m, dict) else None
        if not mid or not efforts:
            raise CrpError("invalid_input", "each model entry needs id and efforts lists")
        lines.append(f"- {mid} | effort: {chr(44).join(str(e) for e in efforts)}")
    lines.append("")
    lines.append("回复示例：按推荐 / 把 1 改成 X / coding_model 用 Y high")
    return chr(10).join(lines)


def main(argv=None):
    reconfigure_stdio()
    parser = argparse.ArgumentParser(prog="model_prompt.py")
    parser.add_argument("--schema", required=True, help="schema snapshot JSON: [{id,efforts}] or {models:[...]}")
    parser.add_argument("--recommend", required=True, help="four recommended fields JSON file")
    args = parser.parse_args(argv)
    try:
        schema = json.loads(open(args.schema, encoding='utf-8').read())
        rec_text = open(args.recommend, encoding='utf-8').read()
        recommend = json.loads(rec_text)
        models = schema.get("models") if isinstance(schema, dict) else schema
        print(build_prompt(models, recommend))
        return 0
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")


if __name__ == "__main__":
    sys.exit(main())
