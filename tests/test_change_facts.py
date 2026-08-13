"""Tests for the Change Facts Engine CLI (plan sections 21-23, 110)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
CHANGE_FACTS = SCRIPTS / "change_facts.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def make_repo(
    base_files: dict[str, str],
    changes: dict[str, str] | None = None,
    untracked: dict[str, str] | None = None,
) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="crp-test-facts-"))
    for rel, content in (base_files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git = _git(repo, "init", "-q")
    if git.returncode != 0:
        raise RuntimeError(git.stderr)
    added = _git(repo, "add", "-A")
    if added.returncode != 0:
        raise RuntimeError(added.stderr)
    committed = _git(
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
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr)
    for rel, content in (changes or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for rel, content in (untracked or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


def commit_all(repo: Path, message: str = "second") -> None:
    added = _git(repo, "add", "-A")
    if added.returncode != 0:
        raise RuntimeError(added.stderr)
    committed = _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        message,
    )
    if committed.returncode != 0:
        raise RuntimeError(committed.stderr)


def head_sha(repo: Path) -> str:
    proc = _git(repo, "rev-parse", "HEAD")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip()


def write_json(data: object) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", prefix="crp-ws-")
    with open(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return Path(name)


def run_facts(
    repo: Path,
    base: str = "HEAD",
    head: str | None = None,
    write_sets: Path | None = None,
) -> tuple[int, dict | None, dict | None]:
    args = [sys.executable, str(CHANGE_FACTS), "--repo", str(repo), "--base", base]
    if head is not None:
        args += ["--head", head]
    if write_sets is not None:
        args += ["--write-sets", str(write_sets)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    err = None
    if proc.stderr.strip():
        try:
            err = json.loads(proc.stderr)
        except json.JSONDecodeError:
            err = {"error": {"code": "unparseable", "message": proc.stderr.strip()}}
    return proc.returncode, out, err


class ChangeFactsCliTest(unittest.TestCase):
    def test_single_java_file(self) -> None:
        repo = make_repo(
            {"src/main/java/App.java": "public class App {}\n"},
            changes={"src/main/java/App.java": "public class App { public void run() {} }\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(facts)
        assert facts is not None
        self.assertEqual(facts["changed_files"], ["src/main/java/App.java"])
        self.assertEqual(facts["untracked_files"], [])
        self.assertEqual(
            facts["changed_file_classes"]["production source"],
            ["src/main/java/App.java"],
        )
        self.assertEqual(facts["changed_languages"], ["Java"])
        self.assertFalse(facts["tests_changed"])
        self.assertFalse(facts["migration_changed"])
        self.assertTrue(facts["diff_ranges"].get("src/main/java/App.java"))
        self.assertEqual(facts["head"], "WORKTREE")
        self.assertEqual(facts["write_set_overlap"]["state"], "unknown")

    def test_test_only(self) -> None:
        repo = make_repo(
            {"src/test/java/AppTest.java": "class AppTest {}\n"},
            changes={"src/test/java/AppTest.java": "class AppTest { void t() {} }\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertTrue(facts["tests_changed"])
        self.assertEqual(facts["changed_file_classes"]["test"], ["src/test/java/AppTest.java"])

    def test_migration(self) -> None:
        repo = make_repo(
            {"db/migration/V1__init.sql": "-- v1\n"},
            changes={"db/migration/V1__init.sql": "-- v1\n-- v2\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertTrue(facts["migration_changed"])
        self.assertEqual(facts["changed_file_classes"]["migration"], ["db/migration/V1__init.sql"])

    def test_pom_change(self) -> None:
        repo = make_repo({"pom.xml": "<project/>\n"}, changes={"pom.xml": "<project><version>2</version></project>\n"})
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertTrue(facts["dependency_manifest_changed"])
        self.assertFalse(facts["lockfile_changed"])
        self.assertEqual(facts["changed_file_classes"]["dependency manifest"], ["pom.xml"])

    def test_lockfile(self) -> None:
        repo = make_repo(
            {"package-lock.json": "{\n}\n"},
            changes={"package-lock.json": "{\n  \"lockfileVersion\": 3\n}\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertTrue(facts["lockfile_changed"])
        self.assertFalse(facts["dependency_manifest_changed"])
        self.assertEqual(facts["changed_file_classes"]["lockfile"], ["package-lock.json"])

    def test_proto_contract(self) -> None:
        repo = make_repo(
            {"api/user.proto": 'syntax = "proto3";\n'},
            changes={"api/user.proto": 'syntax = "proto3";\nmessage User {}\n'},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(
            facts["changed_file_classes"]["contract/interface candidate"],
            ["api/user.proto"],
        )
        self.assertIn("Protocol Buffers", facts["changed_languages"])

    def test_untracked_file(self) -> None:
        repo = make_repo(
            {"README.md": "readme\n"},
            untracked={"scripts/new_tool.py": "print('hello')\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["changed_files"], [])
        self.assertEqual(facts["untracked_files"], ["scripts/new_tool.py"])
        self.assertEqual(facts["changed_languages"], ["Python"])

    def test_untracked_diff_range_full_file(self) -> None:
        repo = make_repo(
            {"README.md": "readme\n"},
            untracked={"notes/新笔记.txt": "line1\nline2\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(
            facts["diff_ranges"]["notes/新笔记.txt"],
            [{"start": 1, "end": 2, "full_file": True}],
        )

    def test_multiple_modules(self) -> None:
        repo = make_repo(
            {
                "module-a/pom.xml": "<project/>\n",
                "module-a/src/AppA.java": "class AppA {}\n",
                "module-b/pom.xml": "<project/>\n",
                "module-b/src/AppB.java": "class AppB {}\n",
            },
            changes={
                "module-a/src/AppA.java": "class AppA { void a() {} }\n",
                "module-b/src/AppB.java": "class AppB { void b() {} }\n",
            },
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["modules"], ["module-a", "module-b"])

    def test_generated_path(self) -> None:
        repo = make_repo(
            {"build/generated/sources/Gen.java": "class Gen {}\n"},
            changes={"build/generated/sources/Gen.java": "class Gen { void x() {} }\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(
            facts["generated_file_candidates"],
            ["build/generated/sources/Gen.java"],
        )
        self.assertEqual(facts["changed_file_classes"]["generated"], ["build/generated/sources/Gen.java"])

    def test_unknown_extension(self) -> None:
        repo = make_repo({"weird.xyz": "data\n"}, changes={"weird.xyz": "data2\n"})
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["changed_file_classes"]["UNKNOWN"], ["weird.xyz"])

    def test_candidate_facts_only_from_changed_lines(self) -> None:
        source = (
            "@Transactional\n"
            "public class RiskService {\n"
            "  public String call(String password) {\n"
            "    synchronized (this) { return new RestTemplate().getForObject(url, String.class); }\n"
            "  }\n"
            "}\n"
        )
        repo = make_repo(
            {"src/main/java/RiskService.java": "class RiskService {}\n"},
            changes={"src/main/java/RiskService.java": source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        for key in (
            "transaction_candidate",
            "public_api_candidate",
            "security_candidate",
            "concurrency_candidate",
            "external_side_effect_candidate",
        ):
            candidate = facts[key]
            self.assertEqual(candidate["state"], "candidate", key)
            self.assertTrue(candidate["evidence"], key)

    def test_candidate_scan_excludes_generated_and_unchanged_lines(self) -> None:
        repo = make_repo(
            {
                "src/main/java/Real.java": "class Real {}\n",
                "build/generated/Gen.java": "@Transactional class Gen {}\n",
            },
            changes={"src/main/java/Real.java": "class Real { String password = \"x\"; }\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        self.assertEqual(security["evidence"][0]["file"], "src/main/java/Real.java")
        self.assertNotIn("build/generated/Gen.java", [e["file"] for e in facts["transaction_candidate"]["evidence"]])

    def test_staged_unstaged_rename_delete_special_paths(self) -> None:
        repo = make_repo(
            {
                "a b.java": "class A {}\nline2\n",
                "special [1] (2).java": "class T {}\n",
                "中文.java": "class C {}\n",
                "normal.java": "class N {}\n",
                "del.java": "class D {}\n",
            }
        )
        renamed = _git(repo, "mv", "a b.java", "renamed.java")
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        removed = _git(repo, "rm", "-q", "del.java")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        (repo / "normal.java").write_text("class N { void staged() {} }\n", encoding="utf-8")
        self.assertEqual(_git(repo, "add", "normal.java").returncode, 0)
        (repo / "renamed.java").write_text(
            "class A {}\nline2\nclass A2 { void renamed() {} }\n",
            encoding="utf-8",
        )
        (repo / "中文.java").write_text("class C { void unstaged() {} }\n", encoding="utf-8")
        (repo / "new 文件.txt").write_text("x\n", encoding="utf-8")

        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(
            facts["changed_files"],
            ["del.java", "normal.java", "renamed.java", "中文.java"],
        )
        self.assertEqual(facts["untracked_files"], ["new 文件.txt"])
        self.assertNotIn("a b.java", facts["changed_files"])
        self.assertNotIn("special [1] (2).java", facts["changed_files"])
        self.assertTrue(facts["diff_ranges"].get("renamed.java"))
        self.assertTrue(facts["diff_ranges"].get("中文.java"))

    def test_nul_parsing_handles_tab_and_rename(self) -> None:
        import sys as sys_module

        if str(SCRIPTS) not in sys_module.path:
            sys_module.path.insert(0, str(SCRIPTS))
        import change_facts as cf_module

        root = Path(tempfile.mkdtemp(prefix="crp-nul-"))
        self.assertEqual(
            cf_module._parse_name_status_z("R100\0old\tname.java\0new name.java\0", root, None),
            ["new name.java"],
        )
        self.assertEqual(
            cf_module._parse_name_status_z("M\0dir/name\twith tab.java\0", root, None),
            ["dir/name\twith tab.java"],
        )
        self.assertEqual(
            cf_module._parse_name_status_z("M\0dir/name\nwith newline.java\0", root, None),
            ["dir/name\nwith newline.java"],
        )

    def test_special_path_with_literal_bf_slash(self) -> None:
        repo = make_repo(
            {"src/a b/file.java": "class F {}\n"},
            changes={"src/a b/file.java": "class F { void x() {} }\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["changed_files"], ["src/a b/file.java"])
        self.assertTrue(facts["diff_ranges"].get("src/a b/file.java"))
        self.assertEqual(set(facts["diff_ranges"]), {"src/a b/file.java"})

    def test_tree_and_blob_refs_invalid_input(self) -> None:
        repo = make_repo({"src/A.java": "class A {}\n"})
        code, out, err = run_facts(repo, base="HEAD^{tree}")
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

        blob = _git(repo, "rev-parse", "HEAD:src/A.java").stdout.strip()
        code, out, err = run_facts(repo, base="HEAD", head=blob)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_binary_untracked_png_no_nul(self) -> None:
        repo = make_repo(
            {"README.md": "r\n"},
            untracked={"image.png": "PNG password without nul\n"},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["untracked_files"], ["image.png"])
        self.assertEqual(facts["diff_ranges"]["image.png"], [{"state": "UNKNOWN"}])
        self.assertEqual(facts["security_candidate"]["state"], "not_detected")

    def test_candidate_self_noise_filtered(self) -> None:
        noise = (
            "# pattern table comment\n"
            'PATTERNS = ("@Transactional", "public class ", "password", "synchronized", "RestTemplate")\n'
            'SECRETS = ["access_token", "secret", "Authorization"]\n'
            "token = parse_token()\n"
        )
        repo = make_repo(
            {"noise.py": "x = 1\n"},
            changes={"noise.py": "x = 1\n" + noise},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        for key in (
            "transaction_candidate",
            "public_api_candidate",
            "security_candidate",
            "concurrency_candidate",
            "external_side_effect_candidate",
        ):
            self.assertEqual(facts[key]["state"], "not_detected", key)

    def test_worktree_drift_with_monkeypatched_snapshot(self) -> None:
        import sys as sys_module

        if str(SCRIPTS) not in sys_module.path:
            sys_module.path.insert(0, str(SCRIPTS))
        import change_facts as cf_module

        repo = make_repo(
            {"src/A.java": "class A {}\n"},
            changes={"src/A.java": "class A { void x() {} }\n"},
        )
        with mock.patch.object(cf_module, "_worktree_snapshot", return_value="stable"):
            facts = cf_module.collect_facts(str(repo), "HEAD", None, None)
            self.assertEqual(facts["changed_files"], ["src/A.java"])

        with mock.patch.object(
            cf_module,
            "_worktree_snapshot",
            side_effect=["a", "b", "b", "b"],
        ):
            facts = cf_module.collect_facts(str(repo), "HEAD", None, None)
            self.assertEqual(facts["changed_files"], ["src/A.java"])

        with mock.patch.object(
            cf_module,
            "_worktree_snapshot",
            side_effect=["a", "b", "c", "d", "e", "f"],
        ):
            with self.assertRaises(cf_module.CrpError) as context:
                cf_module.collect_facts(str(repo), "HEAD", None, None)
            self.assertEqual(context.exception.code, "snapshot_changed")

    def test_real_untracked_mutation_drift_then_retry(self) -> None:
        import sys as sys_module

        if str(SCRIPTS) not in sys_module.path:
            sys_module.path.insert(0, str(SCRIPTS))
        import change_facts as cf_module

        repo = make_repo(
            {"README.md": "r\n"},
            untracked={"data.txt": "v1\n"},
        )
        calls = {"count": 0}

        def mutate(root: Path) -> None:
            if calls["count"] == 0:
                (root / "data.txt").write_text("v2\nv2b\n", encoding="utf-8")
            calls["count"] += 1

        with mock.patch.object(cf_module, "_attempt_hook", side_effect=mutate):
            facts = cf_module.collect_facts(str(repo), "HEAD", None, None)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(facts["untracked_files"], ["data.txt"])
        self.assertEqual(
            facts["diff_ranges"]["data.txt"],
            [{"start": 1, "end": 2, "full_file": True}],
        )

    def test_real_untracked_mutation_three_attempts_fails(self) -> None:
        import sys as sys_module

        if str(SCRIPTS) not in sys_module.path:
            sys_module.path.insert(0, str(SCRIPTS))
        import change_facts as cf_module

        repo = make_repo(
            {"README.md": "r\n"},
            untracked={"data.txt": "v1\n"},
        )
        calls = {"count": 0}

        def mutate(root: Path) -> None:
            calls["count"] += 1
            (root / "data.txt").write_text(f"v{calls['count'] + 1}\n", encoding="utf-8")

        with mock.patch.object(cf_module, "_attempt_hook", side_effect=mutate):
            with self.assertRaises(cf_module.CrpError) as context:
                cf_module.collect_facts(str(repo), "HEAD", None, None)
        self.assertEqual(calls["count"], 3)
        self.assertEqual(context.exception.code, "snapshot_changed")

    def test_candidate_all_occurrences_quoted_first_real_later(self) -> None:
        source = (
            "class App {\n"
            '  String x = "password"; String y = user.password;\n'
            "}\n"
        )
        repo = make_repo(
            {"src/main/java/App.java": "class App {}\n"},
            changes={"src/main/java/App.java": source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        self.assertTrue(
            any(
                entry["file"] == "src/main/java/App.java" and entry["line"] == 2
                for entry in security["evidence"]
            )
        )

    def test_escaped_quote_inside_string_does_not_leak_evidence(self) -> None:
        source = (
            "class App {\n"
            '  String s = "he said \\"password\\" now";\n'
            "}\n"
        )
        repo = make_repo(
            {"src/main/java/App.java": "class App {}\n"},
            changes={"src/main/java/App.java": source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["security_candidate"]["state"], "not_detected")

    def test_even_backslashes_real_closing_quote_then_code_hit(self) -> None:
        source = (
            "class App {\n"
            '  String s = "a\\\\"; String t = user.password;\n'
            "}\n"
        )
        repo = make_repo(
            {"src/main/java/App.java": "class App {}\n"},
            changes={"src/main/java/App.java": source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        self.assertTrue(
            any(
                entry["file"] == "src/main/java/App.java" and entry["line"] == 2
                for entry in security["evidence"]
            )
        )

    def test_quoted_escaped_noise_then_real_occurrence_hits(self) -> None:
        source = (
            "class App {\n"
            '  String a = "he said \\"password\\""; String b = user.password;\n'
            "}\n"
        )
        repo = make_repo(
            {"src/main/java/App.java": "class App {}\n"},
            changes={"src/main/java/App.java": source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        self.assertTrue(
            any(
                entry["file"] == "src/main/java/App.java" and entry["line"] == 2
                for entry in security["evidence"]
            )
        )

    def test_block_string_text_block_internal_not_hit_then_real_hit(self) -> None:
        source = (
            "class App {\n"
            '  String s = """\n'
            "    password inside block\n"
            '    """\n'
            "  String t = user.password;\n"
            "}\n"
        )
        repo = make_repo(
            {"src/main/java/App.java": "class App {}\n"},
            changes={"src/main/java/App.java": source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        lines = {
            entry["line"]
            for entry in security["evidence"]
            if entry["file"] == "src/main/java/App.java"
        }
        self.assertNotIn(3, lines)
        self.assertIn(5, lines)

    def test_block_string_raw_triple_internal_not_hit_then_real_hit(self) -> None:
        source = (
            "text = r'''\n"
            "password inside raw\n"
            "'''\n"
            "call(user.password)\n"
        )
        repo = make_repo(
            {"noise.py": "x = 1\n"},
            changes={"noise.py": "x = 1\n" + source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        lines = {
            entry["line"]
            for entry in security["evidence"]
            if entry["file"] == "noise.py"
        }
        self.assertNotIn(3, lines)
        self.assertIn(5, lines)

    def test_block_string_single_quote_triple_only_internal_not_hit(self) -> None:
        source = (
            "text = '''\n"
            "password only inside\n"
            "'''\n"
        )
        repo = make_repo(
            {"noise.py": "x = 1\n"},
            changes={"noise.py": "x = 1\n" + source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["security_candidate"]["state"], "not_detected")

    def test_block_string_same_line_open_close_then_real_hit(self) -> None:
        source = 's = """block password"""; t = user.password;\n'
        repo = make_repo(
            {"noise.py": "x = 1\n"},
            changes={"noise.py": "x = 1\n" + source},
        )
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        security = facts["security_candidate"]
        self.assertEqual(security["state"], "candidate")
        self.assertTrue(
            any(
                entry["file"] == "noise.py" and entry["line"] == 2
                for entry in security["evidence"]
            )
        )

    def test_argparse_usage_error_json(self) -> None:
        proc = subprocess.run([sys.executable, str(CHANGE_FACTS), "--bogus"], capture_output=True)
        self.assertEqual(proc.returncode, 2)
        error = json.loads(proc.stderr.decode("utf-8"))
        self.assertEqual(error["error"]["code"], "invalid_input")
        self.assertIn("invalid arguments", error["error"]["message"])

    def test_head_mode_ignores_worktree(self) -> None:
        repo = make_repo({"src/A.java": "class A {}\n", "README.md": "readme\n"})
        c1 = head_sha(repo)
        (repo / "src/A.java").write_text("class A { void x() {} }\n", encoding="utf-8")
        (repo / "src/B.java").write_text("class B {}\n", encoding="utf-8")
        self.assertEqual(_git(repo, "rm", "-q", "README.md").returncode, 0)
        commit_all(repo)
        c2 = head_sha(repo)
        (repo / "src/B.java").write_text("class B { void dirty() {} }\n", encoding="utf-8")
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        code, facts, err = run_facts(repo, base=c1, head=c2)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["head"], c2)
        self.assertEqual(facts["untracked_files"], [])
        self.assertEqual(facts["changed_files"], ["README.md", "src/A.java", "src/B.java"])

    def test_worktree_mode_includes_dirty(self) -> None:
        repo = make_repo({"src/A.java": "class A {}\n"})
        (repo / "src/A.java").write_text("class A { void x() {} }\n", encoding="utf-8")
        (repo / "new.txt").write_text("n\n", encoding="utf-8")
        code, facts, err = run_facts(repo)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["head"], "WORKTREE")
        self.assertEqual(facts["changed_files"], ["src/A.java"])
        self.assertEqual(facts["untracked_files"], ["new.txt"])

    def test_head_mode_candidates_from_blob(self) -> None:
        repo = make_repo({"src/A.java": "class A {}\n"})
        c1 = head_sha(repo)
        (repo / "src/A.java").write_text("class A { void x() {} }\n", encoding="utf-8")
        commit_all(repo)
        c2 = head_sha(repo)
        (repo / "src/A.java").write_text("class A { @Transactional void x() {} }\n", encoding="utf-8")

        code, facts, err = run_facts(repo, base=c1, head=c2)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["transaction_candidate"]["state"], "not_detected")

        code, facts, err = run_facts(repo, base=c1)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["transaction_candidate"]["state"], "candidate")

    def test_write_set_overlap_explicit(self) -> None:
        repo = make_repo({"README.md": "r\n"}, untracked={"a.java": "class A {}\n", "b.java": "class B {}\n"})
        overlapping = write_json(
            {
                "tasks": [
                    {"id": "t1", "files": ["a.java", "b.java"]},
                    {"id": "t2", "files": ["b.java"]},
                ]
            }
        )
        try:
            code, facts, err = run_facts(repo, write_sets=overlapping)
        finally:
            overlapping.unlink(missing_ok=True)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(
            facts["write_set_overlap"],
            {
                "state": "confirmed",
                "task_count": 2,
                "pairs": [{"task_a": "t1", "task_b": "t2", "intersection": ["b.java"]}],
            },
        )

        disjoint = write_json(
            {"tasks": [{"id": "t1", "files": ["a.java"]}, {"id": "t2", "files": ["b.java"]}]}
        )
        try:
            code, facts, err = run_facts(repo, write_sets=disjoint)
        finally:
            disjoint.unlink(missing_ok=True)
        self.assertEqual(code, 0, err)
        assert facts is not None
        self.assertEqual(facts["write_set_overlap"]["state"], "not_detected")
        self.assertEqual(facts["write_set_overlap"]["pairs"], [])

    def test_invalid_write_sets_schema(self) -> None:
        repo = make_repo({"README.md": "r\n"})
        bad = write_json({"tasks": [{"id": "t1", "files": "a.java"}]})
        try:
            code, out, err = run_facts(repo, write_sets=bad)
        finally:
            bad.unlink(missing_ok=True)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_undecodable_write_sets_exits_2(self) -> None:
        repo = make_repo({"README.md": "r\n"})
        bad = Path(tempfile.mkdtemp(prefix="crp-ws-bad-")) / "write-sets.json"
        bad.write_bytes(b'{"tasks": [{"id": "t1", "files": ["\xff"]}]}')
        try:
            code, out, err = run_facts(repo, write_sets=bad)
        finally:
            bad.unlink(missing_ok=True)
        self.assertEqual(code, 2, err)
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "invalid_input")

    def test_utf8_bytes_output(self) -> None:
        repo = make_repo(
            {"src/main/java/服务.java": "class 服务 {}\n"},
            changes={"src/main/java/服务.java": "class 服务 { void 运行() {} }\n"},
        )
        proc = subprocess.run(
            [sys.executable, str(CHANGE_FACTS), "--repo", str(repo), "--base", "HEAD"],
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0)
        text = proc.stdout.decode("utf-8")  # strict: proves UTF-8 bytes on stdout
        facts = json.loads(text)
        self.assertIn("src/main/java/服务.java", facts["changed_files"])

    def test_not_a_git_repo(self) -> None:
        repo = Path(tempfile.mkdtemp(prefix="crp-test-nongit-"))
        code, out, err = run_facts(repo)
        self.assertEqual(code, 3)
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"]["code"], "not_git_repo")


if __name__ == "__main__":
    unittest.main()
