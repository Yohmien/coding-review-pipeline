"""Tests for change_facts cache reuse (P8)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "coding-review-pipeline" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import change_facts  # noqa: E402


class ChangeFactsCacheTest(unittest.TestCase):
    def _facts(self) -> dict:
        return {"changed_files": ["a.py"], "untracked_files": [], "diff_ranges": {}}

    def test_missing_cache_returns_none(self, tmp=None):
        self.assertIsNone(change_facts._load_cache(None, "k", 600))

    def test_fresh_matching_cache_hits(self):
        import os
        import tempfile
        import time

        fd, name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": "k", "saved_at": time.time(), "facts": self._facts()}, fh)
        try:
            cached = change_facts._load_cache(name, "k", 600)
            self.assertIsNotNone(cached)
            self.assertTrue(cached["cache_hit"])
        finally:
            Path(name).unlink(missing_ok=True)

    def test_stale_cache_misses(self):
        import os
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": "k", "saved_at": 1.0, "facts": self._facts()}, fh)
        try:
            self.assertIsNone(change_facts._load_cache(name, "k", 600))
        finally:
            Path(name).unlink(missing_ok=True)

    def test_key_mismatch_misses(self):
        import os
        import tempfile
        import time

        fd, name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": "other", "saved_at": time.time(), "facts": self._facts()}, fh)
        try:
            self.assertIsNone(change_facts._load_cache(name, "k", 600))
        finally:
            Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
