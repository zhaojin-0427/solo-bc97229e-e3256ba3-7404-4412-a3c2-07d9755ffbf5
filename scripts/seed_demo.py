#!/usr/bin/env python3
"""Idempotent demo seeder: upload sample NDJSON documents through the HTTP API.

Used by the ``seed`` compose service and usable manually:
    ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/seed_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("ARCHIVE_API_BASE", "http://localhost:8000").rstrip("/")
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"
DEMO_FILES = {
    "gazette": DEMO_DIR / "gazette.ndjson",
    "ledger": DEMO_DIR / "ledger.ndjson",
}


def request(method: str, path: str, data: bytes | None = None,
            content_type: str = "application/x-ndjson"):
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"content-type": content_type} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for_api(timeout_seconds: float = 60.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            status, _ = request("GET", "/health")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1.0)
    raise SystemExit(f"API at {BASE} did not become healthy in time")


def main() -> int:
    wait_for_api()
    status, docs = request("GET", "/documents")
    existing = {d["doc_id"] for d in docs} if status == 200 else set()

    for doc_id, path in DEMO_FILES.items():
        if doc_id in existing:
            print(f"[seed] {doc_id}: already present, skipping")
            continue
        body = path.read_bytes()
        status, payload = request("PUT", f"/documents/{doc_id}", body)
        if status != 202:
            print(f"[seed] {doc_id}: UPLOAD FAILED {status} {payload}")
            return 1
        print(f"[seed] {doc_id}: uploaded, version {payload['version_id']}")

    # Give the background activation tasks a moment, then show what is live.
    time.sleep(1.0)
    status, docs = request("GET", "/documents")
    print("[seed] active documents:")
    for doc in docs:
        print(f"       - {doc['doc_id']}: version {doc['active_version_id']} "
              f"({doc['block_count']} blocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
