"""Runtime configuration.

Everything is overridable through environment variables so the same image can
be used for tests, docker-compose and ad-hoc runs.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ARCHIVE_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("ARCHIVE_DB_PATH", str(DATA_DIR / "archive.sqlite3")))

# Hard limits required by the specification / used to bound memory.
QUERY_MIN_CHARS = 1
QUERY_MAX_CHARS = 80
MAX_LINE_BYTES = int(os.environ.get("ARCHIVE_MAX_LINE_BYTES", str(4 * 1024 * 1024)))
INSERT_BATCH = int(os.environ.get("ARCHIVE_INSERT_BATCH", "500"))
SEARCH_LIMIT = 100


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
