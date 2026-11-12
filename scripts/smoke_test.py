#!/usr/bin/env python3
"""Offline HTTP smoke test.

Starts the (shim-backed) WSGI application on a daemon thread and drives the
whole flow over real HTTP using only the standard library: seeding, cross-page
search, correction/replacement and validation errors. The production stack is
the same application object served by uvicorn; this script exists solely for
the package-index-free sandbox.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if importlib.util.find_spec("fastapi") is None:
    sys.path.insert(0, str(ROOT / "tests" / "_shim"))

WORKDIR = Path(tempfile.mkdtemp(prefix="archive-smoke-"))
os.environ["ARCHIVE_DATA_DIR"] = str(WORKDIR)
os.environ["ARCHIVE_DB_PATH"] = str(WORKDIR / "smoke.sqlite3")

from app.main import app  # noqa: E402


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass


def http(method: str, url: str, data: bytes | None = None):
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/x-ndjson"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> int:
    httpd = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    failures = []

    def check(label: str, condition: bool, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {label} {detail}")
        if not condition:
            failures.append(label)

    try:
        # 1. health
        status, body = http("GET", f"{base}/health")
        check("health endpoint", status == 200 and body["status"] == "ok")

        # 2. upload the two demo documents
        for name in ("gazette", "ledger"):
            payload = (ROOT / "demo" / f"{name}.ndjson").read_bytes()
            status, body = http("PUT", f"{base}/documents/{name}", payload)
            check(f"upload {name}", status == 202, str(body))

        # 3. cross-page hit at the page 1 -> page 2 boundary (the full stop
        #    is part of the original text, so it must appear in the query)
        q = urllib.parse.quote("可用。第二條")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        ok = (
            status == 200
            and body["result_count"] == 1
            and body["hits"][0]["crosses_pages"] is True
            and [s["page"] for s in body["hits"][0]["spans"]] == [1, 2]
        )
        check("cross-page match at page boundary", ok, json.dumps(body, ensure_ascii=False))

        # 3b. cross-block hit within a single page
        q = urllib.parse.quote("轉型，保障")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        ok = (
            status == 200
            and body["result_count"] == 1
            and body["hits"][0]["crosses_blocks"] is True
            and body["hits"][0]["crosses_pages"] is False
        )
        check("cross-block match on one page", ok, json.dumps(body, ensure_ascii=False))

        # 4. whitespace-insensitive query
        q = urllib.parse.quote("檢索 系統應當 忽略排版空白")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        check("whitespace-insensitive search", status == 200 and body["result_count"] == 1)

        # 5. overlapping hits are kept and ordered
        q = urllib.parse.quote("檔")
        status, body = http("GET", f"{base}/documents/gazette/search?q={q}")
        starts = [h["compact_start"] for h in body["hits"]]
        check("overlapping/ordered hits", starts == sorted(starts) and len(starts) >= 2, str(starts))

        # 6. correction: full re-upload atomically switches versions
        corrected = (ROOT / "demo" / "gazette.ndjson").read_text().replace(
            "本規範自公 布之日起施行。",
            "本規範自公布之日起施行，並廢止舊版條款。",
        ).encode()
        status, body = http("PUT", f"{base}/documents/gazette", corrected)
        check("re-upload accepted", status == 202, str(body))
        new_vid = body["version_id"]
        status, body = http(
            "GET",
            f"{base}/documents/gazette/search?q={urllib.parse.quote('廢止舊版條款')}",
        )
        check(
            "new version live after correction",
            status == 200 and body["result_count"] == 1 and body["version_id"] == new_vid,
        )

        # 7. bad line fails the replacement and leaves old version usable
        bad = (ROOT / "demo" / "gazette.ndjson").read_text().encode() + b"{broken json\n"
        status, body = http("PUT", f"{base}/documents/gazette", bad)
        check("malformed line rejected with 400", status == 400 and "line" in body["detail"], str(body))
        status, body = http(
            "GET",
            f"{base}/documents/gazette/search?q={urllib.parse.quote('廢止舊版條款')}",
        )
        check("old version still searchable after failed replacement",
              status == 200 and body["version_id"] == new_vid)

        # 8. query validation
        status, _ = http("GET", f"{base}/documents/gazette/search?q=" + urllib.parse.quote(" "))
        check("blank query rejected with 400", status == 400)
        status, _ = http(
            "GET", f"{base}/documents/gazette/search?q=" + urllib.parse.quote("a" * 81)
        )
        check("81-char query rejected with 400", status == 400)

        # 9. unknown document
        status, _ = http("GET", f"{base}/documents/nope/search?q=x")
        check("unknown document returns 404", status == 404)
    finally:
        httpd.shutdown()
        shutil.rmtree(WORKDIR, ignore_errors=True)

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
