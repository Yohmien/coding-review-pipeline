#!/usr/bin/env python3
"""Validate skill directories.

Checks that each directory contains a SKILL.md with valid frontmatter whose
`name` matches the directory name and that has a `description`. Accepts either
skill directories or parent directories containing skills.
"""

import pathlib
import sys


SKILL_MD = "SKILL.md"


def find_skill_dirs(base: pathlib.Path) -> list[pathlib.Path]:
    if (base / SKILL_MD).exists():
        return [base]
    return sorted(p for p in base.iterdir() if (p / SKILL_MD).exists())


def parse_frontmatter(text: str) -> dict[str, str]:
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().strip("\"'")
            value = value.strip().strip("\"'")
            if key and key not in data:
                data[key] = value
    return data


def validate(skill_dir: pathlib.Path) -> tuple[bool, str]:
    md = skill_dir / SKILL_MD
    if not md.exists():
        return False, f"{skill_dir}: SKILL.md not found"
    text = md.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    name = meta.get("name", "")
    if not name:
        return False, f"{skill_dir}: missing 'name' in frontmatter"
    if name != skill_dir.name:
        return False, f"{skill_dir}: frontmatter name '{name}' != directory name '{skill_dir.name}'"
    if not meta.get("description"):
        return False, f"{skill_dir}: missing 'description' in frontmatter"
    return True, f"{skill_dir}: Skill is valid!"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: validate.py <skill-or-parent-dir> [...]")
        return 2
    ok = True
    for arg in sys.argv[1:]:
        base = pathlib.Path(arg)
        if not base.exists():
            print(f"{base}: does not exist")
            ok = False
            continue
        for skill_dir in find_skill_dirs(base):
            valid, msg = validate(skill_dir)
            print(msg)
            ok = ok and valid
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
