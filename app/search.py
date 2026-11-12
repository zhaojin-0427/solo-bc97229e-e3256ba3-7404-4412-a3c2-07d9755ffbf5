"""Whitespace-insensitive, cross-block/cross-page search.

Index model (all built during ingestion, per version)
-----------------------------------------------------
* ``unigrams_<vid>(ch, pos)``          - every non-whitespace character and its
                                         global position in the compact text
                                         (compact = original text with every
                                         Unicode whitespace removed).
* ``bigrams_<vid>(bg, pos)``           - every adjacent pair of compact
                                         characters, including the boundary
                                         pair between consecutive blocks, so
                                         matches can cross blocks and pages.

A search never materialises the compact document nor the whole index:

* a one-character query reads an ordered slice of ``unigrams``;
* a longer query picks its least frequent bigram and walks that posting list
  in position order, verifying each candidate by fetching only the few blocks
  its span touches (positions are mapped back via the per-block ranges).

Overlapping matches are kept naturally (every position is a distinct row) and
the first 100 hits in reading order are returned.
"""
from __future__ import annotations

import sqlite3

from . import config


def compact(text: str) -> str:
    return "".join(text.split())


def _compact_length(conn: sqlite3.Connection, vid: int) -> int:
    row = conn.execute(
        f"SELECT end_pos FROM blocks_{vid} ORDER BY block_order DESC LIMIT 1"
    ).fetchone()
    return int(row["end_pos"]) if row else 0


def _read_compact_range(
    conn: sqlite3.Connection, vid: int, start: int, length: int
) -> str:
    """Read ``length`` compact characters starting at global position ``start``.

    Only blocks intersecting the requested range are loaded.
    """
    end = start + length
    rows = conn.execute(
        f"SELECT text, start_pos, end_pos FROM blocks_{vid} "
        "WHERE start_pos < ? AND end_pos > ? ORDER BY block_order",
        (end, start),
    ).fetchall()
    parts: list[str] = []
    for row in rows:
        s, e = int(row["start_pos"]), int(row["end_pos"])
        if e <= s:  # block containing only whitespace: no compact characters
            continue
        local_lo = max(start, s) - s
        local_hi = min(end, e) - s
        parts.append(compact(row["text"])[local_lo:local_hi])
        if e >= end:
            break
    return "".join(parts)


def _bigram_counts(
    conn: sqlite3.Connection, vid: int, needle: str
) -> list[tuple[str, int]]:
    table = f"bigrams_{vid}"
    counts = []
    for i in range(len(needle) - 1):
        bg = needle[i : i + 2]
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE bg = ?", (bg,)
        ).fetchone()
        counts.append((bg, int(row["c"])))
    return counts


def _iter_candidates(
    conn: sqlite3.Connection, vid: int, needle: str
):
    """Yield candidate global start positions in reading order.

    Positions of the rarest query bigram are shifted back by the bigram's
    offset inside the query, which turns them into candidate match starts.
    """
    if len(needle) == 1:
        table = f"unigrams_{vid}"
        cursor = conn.execute(
            f"SELECT pos FROM {table} WHERE ch = ? ORDER BY pos", (needle,)
        )
        for row in cursor:
            yield int(row["pos"])
        return

    counts = _bigram_counts(conn, vid, needle)
    bg, _ = min(counts, key=lambda item: item[1])
    offset = next(i for i in range(len(needle) - 1) if needle[i : i + 2] == bg)
    table = f"bigrams_{vid}"
    cursor = conn.execute(
        f"SELECT pos FROM {table} WHERE bg = ? ORDER BY pos", (bg,)
    )
    for row in cursor:
        yield int(row["pos"]) - offset


def _build_highlight(
    conn: sqlite3.Connection,
    vid: int,
    hit_start: int,
    hit_end: int,
    needle: str,
) -> dict:
    """Translate a compact-text range into original per-block character ranges."""
    rows = conn.execute(
        f"SELECT block_order, block_id, page, text, x0, y0, x1, y1, "
        f"start_pos, end_pos, nws_len FROM blocks_{vid} "
        "WHERE start_pos < ? AND end_pos > ? ORDER BY block_order",
        (hit_end, hit_start),
    ).fetchall()

    spans = []
    matched_parts = []
    for row in rows:
        s, e = int(row["start_pos"]), int(row["end_pos"])
        overlap_lo = max(hit_start, s)
        overlap_hi = min(hit_end, e)
        if overlap_hi <= overlap_lo:
            continue  # whitespace-only block inside the span: no chars to mark
        text = row["text"]
        # Map compact offsets inside this block to indices in the original text.
        compact_lo = overlap_lo - s
        compact_hi = overlap_hi - s
        orig_lo = orig_hi = -1
        compact_idx = 0
        for orig_idx, ch in enumerate(text):
            if ch.isspace():
                continue
            if compact_idx == compact_lo:
                orig_lo = orig_idx
            if compact_idx == compact_hi - 1:
                orig_hi = orig_idx + 1
                break
            compact_idx += 1
        if orig_lo < 0 or orig_hi < 0:  # defensive: should be impossible
            raise RuntimeError("compact-to-original position mapping failed")
        spans.append(
            {
                "block_id": row["block_id"],
                "page": int(row["page"]),
                "rect": {
                    "x0": row["x0"],
                    "y0": row["y0"],
                    "x1": row["x1"],
                    "y1": row["y1"],
                },
                "char_start": orig_lo,
                "char_end": orig_hi,
                "block_order": int(row["block_order"]),
            }
        )
        matched_parts.append(text[orig_lo:orig_hi])

    return {
        "compact_start": hit_start,
        "compact_end": hit_end,
        "spans": spans,
        "matched_text": "".join(matched_parts),
        "crosses_blocks": len(spans) > 1,
        "crosses_pages": len({span["page"] for span in spans}) > 1,
    }


def search(
    conn: sqlite3.Connection,
    vid: int,
    raw_query: str,
    limit: int = config.SEARCH_LIMIT,
) -> dict:
    needle = compact(raw_query)
    if not config.QUERY_MIN_CHARS <= len(needle) <= config.QUERY_MAX_CHARS:
        raise ValueError(
            f"query must contain {config.QUERY_MIN_CHARS}-{config.QUERY_MAX_CHARS} "
            "non-whitespace characters"
        )

    total_len = _compact_length(conn, vid)
    hits: list[dict] = []
    # After collecting ``limit`` matches we keep checking candidates with a
    # bounded budget so ``has_more`` means a real 101st match exists, rather
    # than merely that the posting list had more rows.
    overflow_budget = max(limit, 1000)
    has_more = False

    # One read-only transaction gives a consistent snapshot for the whole query.
    conn.execute("BEGIN DEFERRED")
    try:
        for candidate in _iter_candidates(conn, vid, needle):
            if candidate < 0 or candidate + len(needle) > total_len:
                continue
            if len(hits) >= limit:
                if has_more or overflow_budget <= 0:
                    has_more = True
                    break
                overflow_budget -= 1
            window = _read_compact_range(conn, vid, candidate, len(needle))
            if window != needle:
                continue
            if len(hits) >= limit:
                has_more = True
                break
            hits.append(
                _build_highlight(conn, vid, candidate, candidate + len(needle), needle)
            )
    finally:
        conn.rollback()  # read-only snapshot: nothing to commit

    return {
        "query": raw_query,
        "compact_query": needle,
        "version_id": vid,
        "limit": limit,
        "result_count": len(hits),
        "has_more": has_more,
        "hits": hits,
    }
