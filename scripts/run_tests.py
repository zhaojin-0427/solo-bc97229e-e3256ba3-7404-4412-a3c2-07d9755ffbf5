#!/usr/bin/env python3
"""Run the test-suite with stdlib unittest (also works under pytest).

Usage:  python3 -I scripts/run_tests.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARCHIVE_DATA_DIR", str(ROOT / "data" / "test"))
os.environ.setdefault(
    "ARCHIVE_DB_PATH", str(ROOT / "data" / "test" / "test.sqlite3")
)

if importlib.util.find_spec("fastapi") is None:
    sys.path.insert(0, str(ROOT / "tests" / "_shim"))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(1 if result.failures or result.errors else 0)
