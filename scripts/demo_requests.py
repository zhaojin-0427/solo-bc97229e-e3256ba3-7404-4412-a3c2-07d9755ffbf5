#!/usr/bin/env python3
"""End-to-end demo against a running API (stdlib only).

Walks through: cross-page search, whitespace-insensitive matching, overlapping
hits, and a correction via full-document re-upload.

    ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/demo_requests.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("ARCHIVE_API_BASE", "http://localhost:8000").rstrip("/")
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def call(method: str, path: str, data: bytes | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/x-ndjson"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def heading(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_search(query: str) -> None:
    status, body = call("GET", f"/documents/gazette/search?q={urllib.parse.quote(query)}")
    print(f"\nGET /documents/gazette/search?q={query!r}  -> HTTP {status}")
    if status != 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    print(f"result_count={body['result_count']} has_more={body['has_more']} "
          f"version={body['version_id']}")
    for i, hit in enumerate(body["hits"][:5], 1):
        pages = sorted({s["page"] for s in hit["spans"]})
        blocks = [s["block_id"] for s in hit["spans"]]
        ranges = [f"{s['char_start']}:{s['char_end']}" for s in hit["spans"]]
        print(f"  #{i} compact[{hit['compact_start']}:{hit['compact_end']}] "
              f"pages={pages} blocks={blocks} char_ranges={ranges}")
        print(f"      matched_text={hit['matched_text']!r} "
              f"crosses_pages={hit['crosses_pages']}")


def main() -> int:
    heading("0. Health and documents")
    print(call("GET", "/health"))
    print(call("GET", "/documents"))

    heading("1. Cross-page match (phrase split across scan pages 1 and 2)")
    # Page 1 ends "...真實、完整與可用。" and page 2 begins "第二條 ...", so
    # this phrase straddles the two scan pages (punctuation is part of text).
    show_search("可用。第二條")
    # A cross-block hit that stays on the same page.
    show_search("轉型，保障")

    heading("2. Whitespace-insensitive query (arbitrary spaces in the query)")
    show_search("檢索 系統應當 忽略排版空白")

    heading("3. Single-character query (unigram index, ordered hits)")
    show_search("檔")

    heading("4. No-match response")
    show_search("不存在的詞組xyz")

    heading("5. Correction: full re-upload replaces the document atomically")
    corrected = (DEMO_DIR / "gazette.ndjson").read_text().replace(
        "本規範自公 布之日起施行。",
        "本規範自公布之日起施行，並廢止舊版條款。",
    ).encode("utf-8")
    status, payload = call("PUT", "/documents/gazette", corrected)
    print(f"PUT /documents/gazette -> HTTP {status}: {payload}")
    time.sleep(1.0)
    print("versions after replacement:")
    print(json.dumps(call("GET", "/documents/gazette/versions")[1],
                     ensure_ascii=False, indent=2))
    show_search("廢止舊版條款")

    heading("6. Recovery report (empty unless a restart recovered a crash)")
    print(call("GET", "/recovery"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
