"""End-to-end tests for the archive search service (runs under unittest/pytest)."""
from __future__ import annotations

import json
import sqlite3
import tracemalloc
import unittest

from app import config, db, ingest, search
from app.db import get_conn

from tests.helpers import block, make_app, ndjson, reset_db, wsgi_request


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_db()
        self.app = make_app()

    def tearDown(self) -> None:
        pass

    # ---- upload / search basics -----------------------------------------

    def test_basic_upload_and_search_with_highlight(self):
        records = [
            block("b1", 1, "Hello world"),
            block("b2", 1, "foo bar"),
        ]
        status, body = wsgi_request(
            self.app, "PUT", "/documents/doc-a", ndjson(records)
        )
        self.assertEqual(status, 202, body)
        vid = body["version_id"]

        status, body = wsgi_request(
            self.app, "GET", f"/documents/doc-a/search", query={"q": "world"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        hit = body["hits"][0]
        self.assertEqual(hit["matched_text"], "world")
        span = hit["spans"][0]
        self.assertEqual(span["block_id"], "b1")
        self.assertEqual(span["char_start"], 6)
        self.assertEqual(span["char_end"], 11)
        self.assertEqual(span["page"], 1)
        self.assertEqual(span["rect"]["x1"], 10.0)
        self.assertFalse(hit["crosses_pages"])
        self.assertEqual(body["version_id"], vid)

    def test_whitespace_ignored_in_query_and_text(self):
        records = [block("b1", 1, "ab  c\t d\nef")]
        status, _ = wsgi_request(
            self.app, "PUT", "/documents/doc-ws", ndjson(records)
        )
        self.assertEqual(status, 202)
        for query in ("abcdef", "ab c d ef", " a b c d e f ", "abc def"):
            status, body = wsgi_request(
                self.app,
                "GET",
                "/documents/doc-ws/search",
                query={"q": query},
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["result_count"], 1, query)
            # Character ranges map back into the ORIGINAL (whitespace-bearing) text.
            span = body["hits"][0]["spans"][0]
            self.assertEqual(
                records[0]["text"][span["char_start"]:span["char_end"]],
                records[0]["text"],
            )

    def test_cross_block_and_cross_page_match(self):
        records = [
            block("b1", 1, "一二三四五"),
            block("b2", 2, "六七八"),
            block("b3", 2, "九"),
        ]
        status, _ = wsgi_request(
            self.app, "PUT", "/documents/doc-cp", ndjson(records)
        )
        self.assertEqual(status, 202)

        # Match straddling page 1 -> page 2.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-cp/search",
            query={"q": "五六七"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        hit = body["hits"][0]
        self.assertTrue(hit["crosses_blocks"])
        self.assertTrue(hit["crosses_pages"])
        self.assertEqual([s["block_id"] for s in hit["spans"]], ["b1", "b2"])
        self.assertEqual(hit["matched_text"], "五六七")
        self.assertEqual(hit["spans"][0]["char_start"], 4)
        self.assertEqual(hit["spans"][0]["char_end"], 5)
        self.assertEqual(hit["spans"][1]["char_start"], 0)
        self.assertEqual(hit["spans"][1]["char_end"], 2)

        # Match spanning three blocks; this one stays on page 2.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-cp/search",
            query={"q": "七八九"},
        )
        self.assertEqual(status, 200)
        hit = body["hits"][0]
        self.assertEqual([s["block_id"] for s in hit["spans"]], ["b2", "b3"])
        self.assertTrue(hit["crosses_blocks"])
        self.assertFalse(hit["crosses_pages"])

        # A three-block span crossing the page boundary as well.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-cp/search",
            query={"q": "五六七八九"},
        )
        self.assertEqual(status, 200)
        hit = body["hits"][0]
        self.assertEqual(
            [s["block_id"] for s in hit["spans"]], ["b1", "b2", "b3"]
        )
        self.assertTrue(hit["crosses_blocks"])
        self.assertTrue(hit["crosses_pages"])

        # Single-character query uses the unigram posting list.
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-cp/search", query={"q": "五"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        self.assertEqual(body["hits"][0]["compact_start"], 4)

    def test_overlapping_matches_kept_and_ordered(self):
        records = [block("b1", 1, "aaaaa")]
        status, _ = wsgi_request(
            self.app, "PUT", "/documents/doc-ov", ndjson(records)
        )
        self.assertEqual(status, 202)
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-ov/search", query={"q": "aa"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 4)
        starts = [h["compact_start"] for h in body["hits"]]
        self.assertEqual(starts, [0, 1, 2, 3])
        self.assertFalse(body["has_more"])

    def test_limit_100_earliest_hits_and_has_more(self):
        records = [block("b1", 1, "ab" * 300)]  # 600 chars, "ab" x300
        status, _ = wsgi_request(
            self.app, "PUT", "/documents/doc-lim", ndjson(records)
        )
        self.assertEqual(status, 202)
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-lim/search", query={"q": "ab"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 100)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["hits"][0]["compact_start"], 0)
        self.assertEqual(body["hits"][-1]["compact_start"], 198)

    def test_no_match(self):
        records = [block("b1", 1, "abcdef")]
        wsgi_request(self.app, "PUT", "/documents/doc-nm", ndjson(records))
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-nm/search", query={"q": "xyz"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 0)
        self.assertEqual(body["hits"], [])
        self.assertFalse(body["has_more"])

    def test_query_validation(self):
        wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-q",
            ndjson([block("b1", 1, "abc")]),
        )
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-q/search", query={"q": "   "}
        )
        self.assertEqual(status, 400)
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-q/search",
            query={"q": "a" * 81},
        )
        self.assertEqual(status, 400)
        # 80 non-whitespace characters is accepted.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-q/search",
            query={"q": ("a " * 40).strip()},
        )
        self.assertEqual(status, 200)

    def test_unknown_document_404(self):
        status, body = wsgi_request(
            self.app, "GET", "/documents/ghost/search", query={"q": "x"}
        )
        self.assertEqual(status, 404)

    # ---- validation failures ---------------------------------------------

    def test_bad_json_line_rejected_and_marked_failed(self):
        payload = (
            json.dumps(block("b1", 1, "ok")) + "\n"
            + "{not json}\n"
        ).encode()
        status, body = wsgi_request(
            self.app, "PUT", "/documents/doc-bad", payload
        )
        self.assertEqual(status, 400)
        self.assertIn("line 2", body["detail"])
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-bad/versions"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body[0]["state"], "failed")
        # Staging tables were removed.
        conn = get_conn()
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        self.assertNotIn("blocks_1", names)

    def test_invalid_record_fields_rejected(self):
        bad_records = [
            {"block_id": "", "page": 1, "text": "x",
             "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}},
            {"block_id": "b", "page": 0, "text": "x",
             "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}},
            {"block_id": "b", "page": 1, "text": 42,
             "rect": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}},
            {"block_id": "b", "page": 1, "text": "x",
             "rect": {"x0": 0, "y0": 0, "x1": 1}},
            {"block_id": "b", "page": 1, "text": "x",
             "rect": {"x0": 0, "y0": 1, "x1": 1, "y1": 0}},
        ]
        for rec in bad_records:
            status, body = wsgi_request(
                self.app, "PUT", "/documents/doc-invalid", ndjson([rec])
            )
            self.assertEqual(status, 400, rec)

    def test_duplicate_block_id_rejected(self):
        records = [block("dup", 1, "alpha"), block("dup", 1, "beta")]
        status, body = wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-dup",
            ndjson(records),
        )
        self.assertEqual(status, 400)
        self.assertIn("duplicate block_id", body["detail"])

    def test_empty_upload_rejected(self):
        status, body = wsgi_request(
            self.app, "PUT", "/documents/doc-empty", b"\n  \n")
        self.assertEqual(status, 400)

    # ---- replacement semantics -------------------------------------------

    def test_replacement_keeps_old_version_searchable_until_switch(self):
        status, _ = wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-rep",
            ndjson([block("b1", 1, "version one text")]),
        )
        self.assertEqual(status, 202)
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-rep/search",
            query={"q": "version one text"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        old_vid = body["version_id"]

        # A failed replacement must leave the old version intact and searchable.
        bad = (json.dumps(block("b1", 1, "version two")) + "\n{bad").encode()
        status, body = wsgi_request(
            self.app, "PUT", "/documents/doc-rep", bad
        )
        self.assertEqual(status, 400)
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-rep/search",
            query={"q": "version one text"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version_id"], old_vid)

        # A successful replacement switches atomically and drops old tables.
        status, body = wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-rep",
            ndjson([block("c1", 2, "version two corrected text")]),
        )
        self.assertEqual(status, 202)
        new_vid = body["version_id"]
        self.assertNotEqual(old_vid, new_vid)
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-rep/search",
            query={"q": "corrected"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version_id"], new_vid)
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-rep/search",
            query={"q": "version one text"},
        )
        self.assertEqual(body["result_count"], 0)

        conn = get_conn()
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            states = {
                r["state"]
                for r in conn.execute(
                    "SELECT state FROM versions WHERE doc_id = 'doc-rep'"
                )
            }
        finally:
            conn.close()
        self.assertNotIn(f"blocks_{old_vid}", names)
        self.assertIn("ready", states)

    def test_concurrent_second_replacement_rejected(self):
        # Two layers enforce "one replacement per document":
        # (1) the catalog state machine itself rejects a second INGESTING
        #     version while one is still open;
        conn = get_conn()
        try:
            db.init_schema(conn)
            vid = db.create_version(conn, "doc-locked")
            with self.assertRaises(RuntimeError):
                db.create_version(conn, "doc-locked")
            db.fail_version(conn, vid, "test cleanup")
        finally:
            conn.close()

        # (2) the API returns 409 in that situation.
        status, body = wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-locked2",
            ndjson([block("b1", 1, "x")]),
        )
        self.assertEqual(status, 202)
        # Re-create a stuck version directly (bypassing startup recovery)
        # and confirm the endpoint translates the DB backstop into HTTP 409.
        conn = get_conn()
        try:
            db.create_version(conn, "doc-locked2")
        finally:
            conn.close()
        status, body = wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-locked2",
            ndjson([block("b2", 1, "y")]),
        )
        self.assertEqual(status, 409)
        self.assertIn("already", body["detail"])

    # ---- crash recovery ---------------------------------------------------

    def test_disk_write_failure_keeps_old_version(self):
        # Baseline working version.
        status, _ = wsgi_request(
            self.app,
            "PUT",
            "/documents/doc-disk",
            ndjson([block("b1", 1, "original stable text")]),
        )
        self.assertEqual(status, 202)

        # A connection whose writes always fail simulates a disk problem.
        class FailingConn:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                attr = getattr(self._real, name)
                if name in ("execute", "executemany"):
                    def fail(*_a, **_kw):
                        raise sqlite3.OperationalError("disk I/O error")
                    return fail
                return attr

        from app import ingest as ingest_mod

        conn = get_conn()
        try:
            vid = db.create_version(conn, "doc-disk")
        finally:
            conn.close()

        conn = get_conn()
        try:
            with self.assertRaises(OSError):
                ingest_mod.ingest_version(
                    FailingConn(conn),
                    vid,
                    iter([ndjson([block("b2", 1, "would be new text")])]),
                )
        finally:
            # Mark the half-built version failed exactly like the API path does.
            db.fail_version(conn, vid, "storage write failed: disk I/O error")
            conn.close()

        # Old version remains the active, searchable one.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-disk/search",
            query={"q": "original stable text"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        status, versions = wsgi_request(
            self.app, "GET", "/documents/doc-disk/versions"
        )
        self.assertEqual(versions[-1]["state"], "failed")
        self.assertIn("disk I/O error", versions[-1]["error"])

    def test_startup_recovery_marks_interrupted_version_failed(self):
        # Simulate a process killed between ingestion and activation.
        conn = get_conn()
        try:
            db.init_schema(conn)
            vid = db.create_version(conn, "doc-crash")
            ingest.ingest_version(
                conn,
                vid,
                iter([ndjson([block("b1", 1, "half indexed text")])]),
            )
            db.set_state(conn, vid, db.STATE_INDEXING)
        finally:
            conn.close()

        # A fresh app instance performs startup recovery.
        app2 = make_app()
        status, body = wsgi_request(
            app2, "GET", "/documents/doc-crash/versions"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body[0]["state"], "failed")
        self.assertIn("interrupted", body[0]["error"])

        status, report = wsgi_request(app2, "GET", "/recovery")
        self.assertEqual(status, 200)
        self.assertTrue(any("doc-crash" in m for m in report["recovered"]))

        conn = get_conn()
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        self.assertNotIn(f"blocks_{vid}", names)

    # ---- scale / streaming behaviour -------------------------------------

    def test_large_document_streaming_memory_is_bounded(self):
        n_blocks = 20_000

        def chunks():
            buf = []
            for i in range(n_blocks):
                buf.append(
                    json.dumps(
                        block(f"blk-{i}", (i // 50) + 1, f"文本块编号{i}内容ABCD"),
                        ensure_ascii=False,
                    )
                )
                if len(buf) == 256:
                    yield ("\n".join(buf) + "\n").encode()
                    buf.clear()
            if buf:
                yield ("\n".join(buf) + "\n").encode()

        conn = get_conn()
        try:
            db.init_schema(conn)
            vid = db.create_version(conn, "doc-big")
            tracemalloc.start()
            count = ingest.ingest_version(conn, vid, chunks())
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            db.activate_version(conn, "doc-big", vid, count)
        finally:
            conn.close()

        self.assertEqual(count, n_blocks)
        # Peak Python allocations must stay far below the document size
        # (the generated payload alone is several MB).
        self.assertLess(peak, 4 * 1024 * 1024, f"peak={peak}")

        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-big/search",
            query={"q": "内容ABCD文本块编号"},  # cross-block boundary hit
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["result_count"], 1)
        hit = body["hits"][0]
        self.assertTrue(hit["crosses_blocks"])

        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-big/search",
            query={"q": "编号19999"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        self.assertEqual(body["hits"][0]["spans"][0]["block_id"], "blk-19999")

    def test_whitespace_only_blocks_do_not_bridge_boundary_bigrams(self):
        records = [
            block("b1", 1, "甲乙"),
            block("b2", 1, "   "),  # whitespace-only block: no compact chars
            block("b3", 2, "丙丁"),
        ]
        status, _ = wsgi_request(
            self.app, "PUT", "/documents/doc-gap", ndjson(records)
        )
        self.assertEqual(status, 202)
        # The spurious boundary bigram 乙丙 must not exist across the gap.
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-gap/search", query={"q": "乙丙"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 0)
        # Real adjacency inside compact-bearing blocks still works.
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-gap/search", query={"q": "丙丁"}
        )
        self.assertEqual(body["result_count"], 1)

    def test_long_query_and_query_longer_than_document(self):
        records = [block("b1", 1, "甲乙丙丁戊")]
        wsgi_request(
            self.app, "PUT", "/documents/doc-long", ndjson(records)
        )
        # Exactly 80 non-whitespace characters: valid, simply absent.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-long/search",
            query={"q": "甲" * 80},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 0)
        self.assertFalse(body["has_more"])
        # A long query whose bigrams do not exist at all.
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-long/search",
            query={"q": "丙丁戊己庚辛"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 0)

    def test_unicode_surrogate_and_multibyte_handling(self):
        records = [block("b1", 1, "café 🍺 naïve 日本語")]
        status, _ = wsgi_request(
            self.app, "PUT", "/documents/doc-uni", ndjson(records)
        )
        self.assertEqual(status, 202)
        status, body = wsgi_request(
            self.app,
            "GET",
            "/documents/doc-uni/search",
            query={"q": "café🍺naïve"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)
        status, body = wsgi_request(
            self.app, "GET", "/documents/doc-uni/search", query={"q": "日本語"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result_count"], 1)


if __name__ == "__main__":
    unittest.main()
