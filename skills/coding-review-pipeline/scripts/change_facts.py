"""Change Facts Engine CLI (plan sections 20-24).

Collects deterministic facts about a repository change set so the main agent
consumes facts instead of rediscovering them. Outputs machine-readable JSON on
stdout; success exits 0, failures exit with a stable nonzero code and a
structured error on stderr.

Semantics contract (revision 1, fix round 2):
- WORKTREE mode (default) covers staged + unstaged tracked changes and
  untracked files. A deterministic worktree fingerprint (status -z bytes,
  binary-safe diff bytes, untracked path/content hashes) is computed before
  and after collection; drift triggers up to 2 retries without sleep, then a
  structured ``snapshot_changed`` error. Candidate reads use the current
  attempt's read cache.
- Explicit ``--head`` mode is commit-only: it ignores the working tree, peels
  ``<ref>^{commit}`` (tree/blob inputs are invalid_input), and scans candidate
  content from head blobs.
- Diff hunks are parsed per file from ``git diff -U0 -- <path>``; paths are
  never inferred from diff headers. Git path lists use ``-z`` (NUL-safe).
- ``write_set_overlap`` is computed only from explicit task write sets via
  set(A) & set(B); without explicit write sets its state is ``unknown``.
- Candidate scanning is limited to production source, contract, and migration
  classes, real added new-side lines, non-binary files, and skips obvious
  comment and pure-string/pattern-table lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import crp_common
from crp_common import (
    CrpError,
    EXIT_OK,
    emit_error,
    exit_code,
    reconfigure_stdio,
    repo_root,
    run_git,
    run_git_bytes,
    sha256_file,
    stable_json,
    utc_timestamp,
)


CLASS_TEST = "test"
CLASS_MIGRATION = "migration"
CLASS_DEPENDENCY_MANIFEST = "dependency manifest"
CLASS_LOCKFILE = "lockfile"
CLASS_BUILD_CONFIG = "build config"
CLASS_GENERATED = "generated"
CLASS_DOCUMENTATION = "documentation"
CLASS_RESOURCE = "resource"
CLASS_CONTRACT = "contract/interface candidate"
CLASS_PRODUCTION = "production source"
CLASS_UNKNOWN = "UNKNOWN"

SCANNABLE_CLASSES = {CLASS_PRODUCTION, CLASS_CONTRACT, CLASS_MIGRATION}

MODULE_MARKERS = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pyproject.toml",
)

LOCKFILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "composer.lock",
    "poetry.lock",
    "Gemfile.lock",
    "go.sum",
    "Cargo.lock",
}

DEPENDENCY_MANIFESTS = {
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "go.mod",
    "Cargo.toml",
    "composer.json",
    "Gemfile",
}

RESOURCE_EXTS = {
    ".properties",
    ".yml",
    ".yaml",
    ".json",
    ".xml",
    ".sql",
    ".csv",
    ".tsv",
    ".html",
    ".css",
    ".scss",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".ttf",
    ".woff",
    ".woff2",
}

BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".jar",
    ".class",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".gz",
    ".tar",
    ".7z",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".bin",
    ".dat",
    ".db",
    ".sqlite",
}

PRODUCTION_EXTS = {
    ".java",
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".kt",
    ".kts",
    ".rb",
    ".php",
    ".scala",
    ".swift",
    ".vue",
    ".sh",
    ".bash",
    ".ps1",
}

LANGUAGES = {
    ".java": "Java",
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cc": "C++",
    ".cs": "C#",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sql": "SQL",
    ".proto": "Protocol Buffers",
    ".xml": "XML",
    ".json": "JSON",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".rst": "Markdown",
    ".sh": "Shell",
    ".bash": "Shell",
    ".ps1": "PowerShell",
    ".gradle": "Gradle",
    ".scala": "Scala",
    ".swift": "Swift",
    ".vue": "Vue",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".svg": "SVG",
}

CANDIDATE_PATTERNS: dict[str, tuple[str, ...]] = {
    "transaction_candidate": (
        "@Transactional",
        "beginTransaction",
        "transactionManager",
        "setAutoCommit",
        "Propagation.",
        "isolation =",
    ),
    "public_api_candidate": (
        "public class ",
        "public interface ",
        "public record ",
        "public enum ",
        "@RestController",
        "@RequestMapping",
        "@GetMapping",
        "@PostMapping",
        "@PutMapping",
        "@DeleteMapping",
        "@PatchMapping",
    ),
    "security_candidate": (
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "api_token",
        "id_token",
        "csrf_token",
        "Authorization",
        "@PreAuthorize",
        "@Secured",
        "authentication",
        "csrf",
        "jwt",
        "api_key",
    ),
    "concurrency_candidate": (
        "synchronized",
        "ReentrantLock",
        "ConcurrentHashMap",
        "Atomic",
        "@Async",
        "CompletableFuture",
        "ExecutorService",
        "Semaphore",
        "CountDownLatch",
    ),
    "external_side_effect_candidate": (
        "RestTemplate",
        "WebClient",
        "OkHttp",
        "requests.",
        "urlopen",
        "subprocess",
        "ProcessBuilder",
        "Runtime.getRuntime",
        "@FeignClient",
        "HttpURLConnection",
        "KafkaTemplate",
        "JdbcTemplate",
        "smtplib",
    ),
}

_DEFAULT_BASE_CANDIDATES = ("origin/main", "origin/master", "main", "master", "HEAD~1")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
MAX_CANDIDATE_BYTES = 1_000_000
_COMMENT_PREFIXES = ("//", "/*", "*", "#", "--")


class _ReadCache:
    """Per-attempt read cache so hashing and candidate scans share bytes."""

    def __init__(self) -> None:
        self._raw: dict[str, bytes | None] = {}
        self._text: dict[str, str | None] = {}

    def raw_for(self, path: str, root: Path) -> bytes | None:
        if path not in self._raw:
            full = root / path
            try:
                if full.is_file() and full.stat().st_size <= MAX_CANDIDATE_BYTES:
                    self._raw[path] = full.read_bytes()
                else:
                    self._raw[path] = None
            except OSError:
                self._raw[path] = None
        return self._raw[path]

    def text_for(self, path: str, root: Path) -> str | None:
        if path not in self._text:
            raw = self.raw_for(path, root)
            self._text[path] = None if raw is None else raw.decode("utf-8", errors="replace")
        return self._text[path]


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    if any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1]):
        return True
    name = Path(path).name
    stem = Path(path).stem
    return bool(
        re.search(r"(Test|Tests|IT|Spec)$", stem)
        or stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    )


def _is_migration_path(path: str) -> bool:
    name = Path(path).name
    lower = path.lower()
    if re.search(r"(?:^|/)migrations?(?:/|$)", path):
        return True
    if re.search(r"(?:^|/)db/migration(?:/|$)", lower):
        return True
    if name.lower().startswith("changelog") and Path(path).suffix.lower() == ".xml":
        return True
    if re.match(r"(?:V|U)\d+.*__.*\.sql$", name, re.IGNORECASE):
        return True
    return bool(re.match(r"R__.*\.sql$", name, re.IGNORECASE))


def _is_contract_path(path: str) -> bool:
    name = Path(path).name.lower()
    if path.endswith(".proto"):
        return True
    if name.startswith(("openapi", "swagger")) and name.endswith((".yaml", ".yml", ".json")):
        return True
    return bool(path.endswith((".graphql", ".gql", ".thrift")))


def _is_generated_path(path: str) -> bool:
    segments = Path(path).parts
    generated_dirs = {
        "generated",
        "generated-sources",
        "generated-test-sources",
        "node_modules",
        "__pycache__",
        ".gradle",
        "dist",
        "build",
        "out",
        "target",
    }
    if any(segment in generated_dirs for segment in segments[:-1]):
        return True
    return bool(re.search(r"(generated|_pb2|\.min\.)", Path(path).name, re.IGNORECASE))


def _is_build_config(path: str) -> bool:
    name = Path(path).name.lower()
    if name in {
        "dockerfile",
        "jenkinsfile",
        "makefile",
        ".gitignore",
        ".dockerignore",
        ".editorconfig",
        "mvnw",
        "gradlew",
        ".gitlab-ci.yml",
    }:
        return True
    if name.startswith("docker-compose"):
        return True
    if ".github/workflows/" in path.lower():
        return True
    return bool(Path(path).suffix.lower() == ".gradle")


def _is_documentation(path: str) -> bool:
    name = Path(path).name.lower()
    if name in {"readme", "license", "changelog"} or name.startswith(
        ("readme.", "license.", "changelog.")
    ):
        return True
    return Path(path).suffix.lower() in {".md", ".rst", ".txt", ".adoc", ".doc", ".docx", ".pdf"}


def classify_path(path: str) -> str:
    """Classify a repo-relative path into a deterministic file class."""

    if _is_test_path(path):
        return CLASS_TEST
    if _is_migration_path(path):
        return CLASS_MIGRATION
    name = Path(path).name
    if name in LOCKFILES:
        return CLASS_LOCKFILE
    if name in DEPENDENCY_MANIFESTS:
        return CLASS_DEPENDENCY_MANIFEST
    if _is_contract_path(path):
        return CLASS_CONTRACT
    if _is_generated_path(path):
        return CLASS_GENERATED
    if _is_build_config(path):
        return CLASS_BUILD_CONFIG
    if _is_documentation(path):
        return CLASS_DOCUMENTATION
    if name.startswith(".env"):
        return CLASS_RESOURCE
    ext = Path(path).suffix.lower()
    if ext in RESOURCE_EXTS:
        return CLASS_RESOURCE
    if ext in PRODUCTION_EXTS:
        return CLASS_PRODUCTION
    return CLASS_UNKNOWN


def _is_binary_path(path: str) -> bool:
    return Path(path).suffix.lower() in BINARY_EXTS


def _has_control_bytes(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    sample = raw[:8192]
    controls = sum(1 for byte in sample if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D))
    return bool(sample) and controls > len(sample) * 0.3


def _is_binary_content(raw: bytes) -> bool:
    return _has_control_bytes(raw)


def _resolve_commit_ref(root: Path, ref: str) -> str:
    """Peel ``<ref>^{commit}``; trees/blobs are invalid_input."""

    proc = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=str(root))
    if proc.returncode != 0:
        raise CrpError(
            "invalid_input",
            "ref does not resolve to a commit",
            ref=ref,
            git_error=proc.stderr.strip(),
        )
    return proc.stdout.strip()


def _resolve_base(root: Path, base: str | None) -> str:
    if base is not None:
        return _resolve_commit_ref(root, base)
    for candidate in _DEFAULT_BASE_CANDIDATES:
        proc = run_git(
            ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            cwd=str(root),
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    raise CrpError(
        "invalid_input",
        "cannot resolve a default base ref",
        candidates=list(_DEFAULT_BASE_CANDIDATES),
    )


def _parse_status_tokens(status_bytes: bytes) -> tuple[list[str], dict[str, str]]:
    """Parse porcelain -z bytes into (untracked paths, rename pairs old->new)."""

    untracked: list[str] = []
    renames: dict[str, str] = {}
    tokens = status_bytes.decode("utf-8", errors="replace").split("\0")
    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]
        if token == "":
            index += 1
            continue
        if token.startswith("??"):
            untracked.append(token[3:])
        elif "R" in token[:2] and index + 1 < count:
            renames[tokens[index + 1]] = token[3:]
            index += 1
        index += 1
    return sorted(set(untracked)), renames


def _status_facts(root: Path) -> tuple[list[str], dict[str, str]]:
    proc = run_git_bytes(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(root),
    )
    return _parse_status_tokens(proc.stdout)


def _parse_name_status_z(text: str, root: Path, head: str | None) -> list[str]:
    """Parse ``git diff --name-status -z`` into destination paths per file."""

    tokens = text.split("\0")
    paths: list[str] = []
    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]
        if token == "":
            index += 1
            continue
        status = token
        if status[0] in ("R", "C") and index + 2 < count:
            paths.append(
                _rename_destination(tokens[index + 1], tokens[index + 2], root, head)
            )
            index += 3
        elif index + 1 < count:
            paths.append(tokens[index + 1])
            index += 2
        else:
            index += 1
    return paths


def _rename_destination(first: str, second: str, root: Path, head: str | None) -> str:
    """Return the destination path of a rename record (existence-based)."""

    if head is not None:
        if _exists_in_head(root, head, second):
            return second
        if _exists_in_head(root, head, first):
            return first
    else:
        if (root / second).exists():
            return second
        if (root / first).exists():
            return first
    return second


def _exists_in_head(root: Path, head: str, path: str) -> bool:
    proc = run_git(["cat-file", "-e", f"{head}:{path}"], cwd=str(root))
    return proc.returncode == 0


def _apply_rename_pairs(paths: list[str], renames: dict[str, str]) -> list[str]:
    """Replace deleted source paths with their rename destinations."""

    result = set(paths)
    for old, new in renames.items():
        if old in result:
            result.discard(old)
            result.add(new)
    return sorted(result)


def _parse_hunks(diff_text: str) -> tuple[list[dict[str, int]], set[int]]:
    """Parse ONLY hunk headers/bodies; never infer paths from headers."""

    ranges: list[dict[str, int]] = []
    changed_lines: set[int] = set()
    new_lineno = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            in_hunk = match is not None
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if count > 0:
                    ranges.append({"start": start, "end": start + count - 1})
                new_lineno = start
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            changed_lines.add(new_lineno)
            new_lineno += 1
        elif line.startswith(" "):
            new_lineno += 1
    return ranges, changed_lines


def _file_diff(root: Path, base: str, head: str | None, path: str) -> str:
    """Per-file diff; the path is a single argv item after ``--`` (no shell)."""

    args = ["diff", "--no-color", "-U0", "-M", base]
    if head is not None:
        args += [head]
    args += ["--", path]
    proc = run_git(args, cwd=str(root))
    if proc.returncode != 0:
        raise CrpError(
            "internal_error",
            "git diff failed for path",
            path=path,
            git_error=proc.stderr.strip(),
        )
    return proc.stdout


def _marker_module(path: str, root: Path) -> str | None:
    parent = (root / path).parent
    while True:
        for marker in MODULE_MARKERS:
            if (parent / marker).is_file():
                try:
                    relative = parent.relative_to(root).as_posix()
                except ValueError:
                    relative = "."
                return relative or "."
        if parent == root or parent.parent == parent:
            return None
        parent = parent.parent


def _load_write_sets(path_arg: str | None) -> list[tuple[str, list[str]]]:
    if path_arg is None:
        return []
    try:
        data = json.loads(Path(path_arg).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CrpError("invalid_input", "write-sets file not found", path=path_arg) from error
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CrpError(
            "invalid_input",
            "write-sets file is not valid JSON",
            path=path_arg,
            error=str(error),
        ) from error
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise CrpError("invalid_input", "write-sets must be {\"tasks\": [...]}", path=path_arg)
    tasks: list[tuple[str, list[str]]] = []
    for index, task in enumerate(data["tasks"]):
        if not isinstance(task, dict):
            raise CrpError("invalid_input", "write-sets task must be an object", index=index)
        task_id = task.get("id")
        files = task.get("files")
        if not isinstance(task_id, str) or not task_id:
            raise CrpError(
                "invalid_input",
                "write-sets task id must be a non-empty string",
                index=index,
            )
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise CrpError(
                "invalid_input",
                "write-sets task files must be a list of strings",
                index=index,
            )
        tasks.append((task_id, files))
    return tasks


def _compute_write_set_overlap(tasks: list[tuple[str, list[str]]]) -> dict[str, object]:
    if not tasks:
        return {"state": "unknown", "task_count": 0, "pairs": []}
    pairs: list[dict[str, object]] = []
    for i in range(len(tasks)):
        id_a, files_a = tasks[i]
        for j in range(i + 1, len(tasks)):
            id_b, files_b = tasks[j]
            intersection = sorted(set(files_a) & set(files_b))
            if intersection:
                pairs.append({"task_a": id_a, "task_b": id_b, "intersection": intersection})
    pairs.sort(key=lambda pair: (pair["task_a"], pair["task_b"]))
    return {"state": "confirmed" if pairs else "not_detected", "task_count": len(tasks), "pairs": pairs}


def _is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in _COMMENT_PREFIXES)


def _inside_quotes(line: str, index: int) -> bool:
    """Quote-span check with backslash parity; no AST.

    A quote preceded by an odd number of consecutive backslashes is escaped and
    does not toggle string state; an even count (including zero) is a real
    quote boundary.
    """

    in_double = False
    in_single = False
    i = 0
    while i < index:
        char = line[i]
        if char == "\\":
            i += 1
            continue
        if char in ('"', "'"):
            backslashes = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                backslashes += 1
                j -= 1
            escaped = backslashes % 2 == 1
            if char == '"' and not in_single and not escaped:
                in_double = not in_double
            elif char == "'" and not in_double and not escaped:
                in_single = not in_single
        i += 1
    return in_double or in_single


_TRIPLE_DELIMITERS = ("'''", '"""')


def _is_escaped(line: str, index: int) -> bool:
    """True when ``index`` is preceded by an odd run of backslashes."""

    backslashes = 0
    j = index - 1
    while j >= 0 and line[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


def _scan_block_line(
    line: str,
    in_block: str | None,
) -> tuple[str | None, list[tuple[int, int]]]:
    """Scan one line for triple-quote block coverage.

    Returns (next block state, list of covered inclusive char ranges). A block
    opened and closed on the same line leaves the trailing region outside the
    covered ranges so real occurrences after the close still scan. Raw
    prefixes (``r'''`` / ``r\"\"\"``) are handled implicitly because the triple
    delimiter is found after the prefix character.
    """

    covered: list[tuple[int, int]] = []
    single_q = False
    double_q = False
    state = in_block
    i = 0
    n = len(line)
    while i < n:
        if state is not None:
            close = line.find(state, i)
            if close == -1:
                covered.append((i, n - 1))
                return state, covered
            if _is_escaped(line, close):
                i = close + 1
                continue
            covered.append((i, close - 1))
            state = None
            i = close + 3
            continue
        char = line[i]
        if char in ("'", '"'):
            triple = line[i : i + 3]
            if (
                triple in _TRIPLE_DELIMITERS
                and not _is_escaped(line, i)
                and not single_q
                and not double_q
            ):
                state = triple
                i += 3
                continue
            if not _is_escaped(line, i):
                if char == "'" and not double_q:
                    single_q = not single_q
                elif char == '"' and not single_q:
                    double_q = not double_q
        i += 1
    return state, covered


def _compute_block_coverage(text: str) -> dict[int, list[tuple[int, int]]]:
    """Per-line triple-quote coverage; walks the whole file for state."""

    coverage: dict[int, list[tuple[int, int]]] = {}
    state: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        state, covered = _scan_block_line(line, state)
        if covered:
            coverage[lineno] = covered
    return coverage


def _in_covered_range(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in ranges)


def _scan_evidence(
    files: list[str],
    content_fn,
    line_numbers: dict[str, set[int]],
    patterns: tuple[str, ...],
    max_matches: int = 10,
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for rel in files:
        if len(evidence) >= max_matches:
            break
        allowed = line_numbers.get(rel)
        if not allowed:
            continue
        text = content_fn(rel)
        if text is None or "\x00" in text:
            continue
        block_coverage = _compute_block_coverage(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if lineno not in allowed or _is_noise_line(line):
                continue
            covered_ranges = block_coverage.get(lineno, [])
            for pattern in patterns:
                matched = False
                start = 0
                while True:
                    index = line.find(pattern, start)
                    if index == -1:
                        break
                    if not _in_covered_range(index, covered_ranges) and not _inside_quotes(
                        line, index
                    ):
                        matched = True
                        break
                    start = index + 1
                if matched:
                    evidence.append({"file": rel, "line": lineno, "match": pattern})
                    break
            if len(evidence) >= max_matches:
                break
    return evidence


def _candidate_facts(
    files: list[str],
    content_fn,
    line_numbers: dict[str, set[int]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for key, patterns in CANDIDATE_PATTERNS.items():
        evidence = _scan_evidence(files, content_fn, line_numbers, patterns)
        result[key] = {"state": "candidate" if evidence else "not_detected", "evidence": evidence}
    return result


def _worktree_snapshot(root: Path, base: str, cache: _ReadCache) -> str:
    """Deterministic worktree fingerprint; monkeypatch seam for drift tests."""

    status = run_git_bytes(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(root),
    )
    diff = run_git_bytes(
        ["diff", "--no-color", "-U0", "-M", base],
        cwd=str(root),
    )
    untracked, _ = _parse_status_tokens(status.stdout)
    untracked_hashes: list[dict[str, str]] = []
    for path in untracked:
        raw = cache.raw_for(path, root)
        if raw is None:
            full = root / path
            content_sha = sha256_file(full) if full.is_file() else "missing"
        else:
            content_sha = hashlib.sha256(raw).hexdigest()
        untracked_hashes.append({"path": path, "sha256": content_sha})
    payload = stable_json(
        {
            "status_sha256": hashlib.sha256(status.stdout).hexdigest(),
            "diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
            "untracked": untracked_hashes,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _attempt_hook(root: Path) -> None:
    """Test seam called between collection and the after-snapshot; no-op."""

    return None


def _collect_facts_once(
    root: Path,
    base: str,
    head: str | None,
    write_sets_arg: str | None,
    cache: _ReadCache | None,
) -> dict[str, object]:
    if head is not None:
        name_status = run_git(
            ["diff", "--name-status", "-z", "-M", base, head],
            cwd=str(root),
        )
        untracked: list[str] = []
    else:
        name_status = run_git(
            ["diff", "--name-status", "-z", "-M", base],
            cwd=str(root),
        )
        untracked, status_renames = _status_facts(root)
    if name_status.returncode != 0:
        raise CrpError(
            "internal_error",
            "git diff failed",
            git_error=name_status.stderr.strip(),
        )

    changed = _parse_name_status_z(name_status.stdout, root, head)
    if head is None:
        changed = _apply_rename_pairs(changed, status_renames)
    changed = sorted(set(changed))
    all_files = sorted(set(changed) | set(untracked))

    classes = {path: classify_path(path) for path in all_files}
    changed_file_classes: dict[str, list[str]] = {}
    for path in all_files:
        changed_file_classes.setdefault(classes[path], []).append(path)

    languages = sorted(
        {
            LANGUAGES[Path(path).suffix.lower()]
            for path in all_files
            if Path(path).suffix.lower() in LANGUAGES
        }
    )
    modules = sorted(
        {
            module
            for path in all_files
            if (module := _marker_module(path, root)) is not None
        }
    )

    parsed_ranges: dict[str, list[dict[str, int]]] = {}
    changed_line_numbers: dict[str, set[int]] = {}
    for path in changed:
        diff_text = _file_diff(root, base, head, path)
        ranges, lines = _parse_hunks(diff_text)
        parsed_ranges[path] = ranges
        changed_line_numbers[path] = lines

    diff_ranges: dict[str, object] = {path: parsed_ranges[path] for path in changed}
    for path in untracked:
        count = _untracked_line_count(path, root, cache)
        if count is None:
            diff_ranges[path] = [{"state": "UNKNOWN"}]
        elif count == 0:
            diff_ranges[path] = [{"full_file": True, "end": 0}]
        else:
            diff_ranges[path] = [{"start": 1, "end": count, "full_file": True}]
            changed_line_numbers[path] = set(range(1, count + 1))

    if head is not None:
        blob_cache: dict[str, bytes | None] = {}

        def head_raw(path: str) -> bytes | None:
            if path not in blob_cache:
                proc = run_git_bytes(["show", f"{head}:{path}"], cwd=str(root))
                blob_cache[path] = (
                    proc.stdout
                    if proc.returncode == 0 and len(proc.stdout) <= MAX_CANDIDATE_BYTES
                    else None
                )
            return blob_cache[path]

        def content_fn(path: str) -> str | None:
            raw = head_raw(path)
            return None if raw is None else raw.decode("utf-8", errors="replace")

    else:
        blob_cache = None

        def content_fn(path: str) -> str | None:
            return cache.text_for(path, root) if cache is not None else None

    scan_files: list[str] = []
    for path in all_files:
        if classes[path] not in SCANNABLE_CLASSES or _is_binary_path(path):
            continue
        if head is not None:
            raw = head_raw(path)
        else:
            raw = cache.raw_for(path, root) if cache is not None else None
        if raw is not None and not _is_binary_content(raw):
            scan_files.append(path)

    return {
        "repo_root": str(root),
        "base": base,
        "head": head if head is not None else "WORKTREE",
        "changed_files": changed,
        "untracked_files": untracked,
        "changed_file_classes": changed_file_classes,
        "changed_languages": languages,
        "modules": modules,
        "tests_changed": any(cls == CLASS_TEST for cls in classes.values()),
        "dependency_manifest_changed": any(cls == CLASS_DEPENDENCY_MANIFEST for cls in classes.values()),
        "lockfile_changed": any(cls == CLASS_LOCKFILE for cls in classes.values()),
        "migration_changed": any(cls == CLASS_MIGRATION for cls in classes.values()),
        "generated_file_candidates": sorted(
            path for path, cls in classes.items() if cls == CLASS_GENERATED
        ),
        "write_set_overlap": _compute_write_set_overlap(_load_write_sets(write_sets_arg)),
        "diff_ranges": diff_ranges,
        **_candidate_facts(scan_files, content_fn, changed_line_numbers),
        "generated_at": utc_timestamp(),
    }


def _untracked_line_count(path: str, root: Path, cache: _ReadCache | None) -> int | None:
    if _is_binary_path(path):
        return None
    if cache is not None:
        raw = cache.raw_for(path, root)
        if raw is None or _is_binary_content(raw):
            return None
        text = cache.text_for(path, root)
        return None if text is None else len(text.splitlines())
    return None


def collect_facts(
    repo_arg: str | None,
    base_arg: str | None,
    head_arg: str | None,
    write_sets_arg: str | None,
) -> dict[str, object]:
    root = repo_root(repo_arg)
    base = _resolve_base(root, base_arg)
    if head_arg is not None:
        head = _resolve_commit_ref(root, head_arg)
        return _collect_facts_once(root, base, head, write_sets_arg, cache=None)

    for attempt in range(1, 4):
        cache = _ReadCache()
        before = _worktree_snapshot(root, base, cache)
        facts = _collect_facts_once(root, base, None, write_sets_arg, cache)
        _attempt_hook(root)
        after = _worktree_snapshot(root, base, _ReadCache())
        if before == after:
            return facts
    raise CrpError(
        "snapshot_changed",
        "worktree changed while collecting change facts",
        attempts=3,
    )


class _CrpArgumentParser(argparse.ArgumentParser):
    """argparse subclass that emits UTF-8 structured JSON on usage errors."""

    def error(self, message: str) -> None:
        emit_error(CrpError("invalid_input", "invalid arguments", detail=message))
        raise SystemExit(exit_code("invalid_input"))


def _load_cache(cache_file: str | None, cache_key: str, ttl_seconds: int) -> dict | None:
    """Return cached change facts when the key matches and the entry is fresh."""

    if not cache_file or ttl_seconds <= 0:
        return None
    path = Path(cache_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("key") != cache_key:
        return None
    saved_at = data.get("saved_at")
    if not isinstance(saved_at, (int, float)) or saved_at < 0:
        return None
    import time

    if time.time() - saved_at > ttl_seconds:
        return None
    facts = data.get("facts")
    if not isinstance(facts, dict):
        return None
    return {**facts, "cache_hit": True}


def _write_cache(cache_file: str | None, cache_key: str, facts: dict) -> None:
    if not cache_file:
        return
    import time

    payload = {
        "key": cache_key,
        "saved_at": time.time(),
        "facts": facts,
    }
    try:
        crp_common.atomic_json_write(cache_file, payload)
    except OSError:
        pass  # cache is best-effort; never fail collection on cache IO


def main(argv: list[str] | None = None) -> int:
    reconfigure_stdio()
    parser = _CrpArgumentParser(
        prog="change_facts.py",
        description="Collect deterministic change facts from a git repository.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="repository path (default: current directory)",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="base ref (peeled to commit; default: origin/main, origin/master, main, master, HEAD~1)",
    )
    parser.add_argument(
        "--head",
        default=None,
        help="head ref (peeled to commit); omit to include the working tree",
    )
    parser.add_argument(
        "--write-sets",
        default=None,
        help="JSON file: {\"tasks\": [{\"id\": \"...\", \"files\": [...]}]}",
    )
    parser.add_argument(
        "--cache-file",
        default=None,
        help="optional cache JSON path for reuse between pipeline re-entries",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=600,
        help="cache validity window in seconds (default 600); 0 disables reuse",
    )
    args = parser.parse_args(argv)
    try:
        cache_key = crp_common.hash_json(
            {"repo": args.repo, "base": args.base, "head": args.head, "write_sets": args.write_sets}
        )
        cached = _load_cache(args.cache_file, cache_key, args.cache_ttl)
        if cached is not None:
            print(json.dumps(cached, ensure_ascii=False, sort_keys=True, indent=2))
            return EXIT_OK
        facts = collect_facts(args.repo, args.base, args.head, args.write_sets)
        facts["cache_fingerprint"] = cache_key
        _write_cache(args.cache_file, cache_key, facts)
    except CrpError as error:
        emit_error(error)
        return exit_code(error.code)
    except Exception as error:  # defensive: never leak a traceback as output
        emit_error(CrpError("internal_error", "unexpected failure", detail=str(error)))
        return exit_code("internal_error")
    print(json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
