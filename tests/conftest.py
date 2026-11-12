"""Test bootstrap: point imports at the offline stdlib shims when FastAPI is
not installed (this sandbox has no package index). In CI/Docker the real
FastAPI and Pydantic are used and this conftest changes nothing.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "tests" / "_shim"

sys.path.insert(0, str(ROOT))

if importlib.util.find_spec("fastapi") is None:
    sys.path.insert(0, str(SHIM))

os.environ.setdefault("ARCHIVE_DATA_DIR", str(ROOT / "data" / "test"))
os.environ.setdefault(
    "ARCHIVE_DB_PATH", str(ROOT / "data" / "test" / "test.sqlite3")
)
