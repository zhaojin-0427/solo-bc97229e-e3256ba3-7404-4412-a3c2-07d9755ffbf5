"""Property-style cross-check: engine results vs. brute-force substring scan.

Random documents made of small blocks (including whitespace-only ones and
random internal spaces) are indexed; every query result is compared against
``str.find`` enumeration over the whitespace-stripped concatenation.
"""
from __future__ import annotations

import random
import unittest

from app import db, search
from app.db import get_conn

from tests.helpers import block, make_app, ndjson, reset_db, wsgi_request

ALPHABET = list("甲乙丙丁戊abcde")
SPACES = [" ", "\t", "\n", "　"]


def brute_force_hits(compact_text: str, needle: str) -> list[int]:
    hits, start = [], 0
    while True:
        idx = compact_text.find(needle, start)
        if idx < 0:
            return hits
        hits.append(idx)
        start = idx + 1  # keep overlapping matches


class PropertyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_db()
        self.app = make_app()

    def test_engine_matches_brute_force(self):
        rng = random.Random(20261112)
        for trial in range(8):
            doc_id = f"doc-rand-{trial}"
            records = []
            compact_parts = []
            n_blocks = rng.randint(1, 25)
            for i in range(n_blocks):
                if rng.random() < 0.15:
                    text = "".join(rng.choice(SPACES) for _ in range(rng.randint(1, 3)))
                else:
                    chars = [rng.choice(ALPHABET) for _ in range(rng.randint(1, 8))]
                    text = "".join(
                        ch + (rng.choice(SPACES) if rng.random() < 0.3 else "")
                        for ch in chars
                    )
                records.append(block(f"blk-{i}", (i // 4) + 1, text))
                compact_parts.append("".join(text.split()))
            compact_text = "".join(compact_parts)

            status, _ = wsgi_request(
                self.app, "PUT", f"/documents/{doc_id}", ndjson(records)
            )
            self.assertEqual(status, 202)

            conn = get_conn()
            try:
                vid = db.get_active_version(conn, doc_id)
                self.assertIsNotNone(vid)
                for _ in range(40):
                    length = rng.randint(1, min(6, len(compact_text) or 1))
                    if compact_text:
                        needle = "".join(
                            rng.choice(compact_text) for _ in range(length)
                        )
                    else:
                        needle = rng.choice(ALPHABET)
                    result = search.search(conn, vid, needle)
                    expected = brute_force_hits(compact_text, needle)[:100]
                    got = [h["compact_start"] for h in result["hits"]]
                    self.assertEqual(
                        got, expected,
                        msg=f"doc={doc_id} needle={needle!r} trial={trial}",
                    )
                    self.assertEqual(
                        result["has_more"],
                        len(brute_force_hits(compact_text, needle)) > 100,
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
