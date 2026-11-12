"""Streaming NDJSON ingestion.

The upload is parsed line by line from raw byte chunks: the whole document is
never held in memory, and neither are the full position maps (positions are
computed incrementally and streamed straight into SQLite in bounded batches).

Each NDJSON line must be one JSON object:

    {"block_id": "...", "page": 1, "text": "...",
     "rect": {"x0": 0.1, "y0": 0.2, "x1": 1.3, "y1": 2.4}}

Line order is the reading order. Whitespace inside ``text`` is kept verbatim
for later highlighting; matching always happens on the whitespace-stripped
form, whose global character positions are assigned here.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

from . import config


class IngestError(ValueError):
    """A malformed upload line or an invalid record."""

    def __init__(self, line_no: int, message: str):
        prefix = f"line {line_no}: " if line_no else ""
        super().__init__(f"{prefix}{message}")
        self.line_no = line_no


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_record(record: object, line_no: int) -> dict:
    if not isinstance(record, dict):
        raise IngestError(line_no, "JSON value must be an object")

    block_id = record.get("block_id")
    if not isinstance(block_id, str) or not block_id:
        raise IngestError(line_no, "field 'block_id' must be a non-empty string")

    page = record.get("page")
    if not _is_int(page) or page < 1:
        raise IngestError(line_no, "field 'page' must be a positive integer")

    text = record.get("text")
    if not isinstance(text, str):
        raise IngestError(line_no, "field 'text' must be a string")

    rect = record.get("rect")
    if not isinstance(rect, dict):
        raise IngestError(line_no, "field 'rect' must be an object")
    coords = {}
    for key in ("x0", "y0", "x1", "y1"):
        value = rect.get(key)
        if not _is_number(value):
            raise IngestError(line_no, f"field 'rect.{key}' must be a number")
        coords[key] = float(value)
    if coords["x1"] < coords["x0"] or coords["y1"] < coords["y0"]:
        raise IngestError(
            line_no,
            "rect coordinates must satisfy x1>=x0 and y1>=y0 "
            "(zero-area rectangles are allowed)",
        )

    return {
        "block_id": block_id,
        "page": int(page),
        "text": text,
        "rect": coords,
    }


def iter_ndjson_lines(chunks: Iterator[bytes]) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, decoded_line)`` from raw chunks.

    Incremental UTF-8 decoding handles multi-byte characters split across
    chunks. Blank lines are ignored. Lines exceeding the byte limit fail the
    upload instead of growing memory without bound.
    """
    buffer = b""
    line_no = 0
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            line_no += 1
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if len(raw_line) > config.MAX_LINE_BYTES:
                raise IngestError(line_no, f"line exceeds {config.MAX_LINE_BYTES} bytes")
            if not raw_line.strip():
                continue
            try:
                yield line_no, raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IngestError(line_no, f"invalid UTF-8: {exc.reason}") from exc
    if buffer:
        line_no += 1
        if len(buffer) > config.MAX_LINE_BYTES:
            raise IngestError(line_no, f"line exceeds {config.MAX_LINE_BYTES} bytes")
        if buffer.strip():
            try:
                yield line_no, buffer.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IngestError(line_no, f"invalid UTF-8: {exc.reason}") from exc


def _flush(
    conn: sqlite3.Connection,
    blocks_table: str,
    unigrams_table: str,
    bigrams_table: str,
    block_rows: list[tuple],
    uni_rows: list[tuple[str, int]],
    bi_rows: list[tuple[str, int]],
) -> None:
    try:
        conn.executemany(
            f"INSERT INTO {blocks_table} "
            "(block_order, block_id, page, text, x0, y0, x1, y1, "
            " start_pos, end_pos, nws_len) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            block_rows,
        )
        conn.executemany(
            f"INSERT INTO {unigrams_table}(ch, pos) VALUES (?, ?)", uni_rows
        )
        conn.executemany(
            f"INSERT INTO {bigrams_table}(bg, pos) VALUES (?, ?)", bi_rows
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise IngestError(0, f"duplicate block_id rejected by storage: {exc}") from exc
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise OSError(f"storage write failed: {exc}") from exc


def ingest_version(
    conn: sqlite3.Connection,
    vid: int,
    chunks: Iterator[bytes],
) -> int:
    """Consume the whole upload into version ``vid``.

    Returns the number of ingested blocks. Raises :class:`IngestError` on the
    first bad line and :class:`OSError` on disk write failures; in both cases
    the caller is responsible for marking the version FAILED (the previously
    active version, if any, is never touched).
    """
    blocks_table, unigrams_table, bigrams_table = (
        f"blocks_{vid}",
        f"unigrams_{vid}",
        f"bigrams_{vid}",
    )

    pos = 0
    order = 0
    prev_last: str | None = None
    block_rows: list[tuple] = []
    uni_rows: list[tuple[str, int]] = []
    bi_rows: list[tuple[str, int]] = []
    saw_record = False
    in_batch: set[str] = set()

    for line_no, line in iter_ndjson_lines(chunks):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError(line_no, f"invalid JSON: {exc.msg}") from exc
        rec = validate_record(record, line_no)
        if rec["block_id"] in in_batch:
            raise IngestError(line_no, f"duplicate block_id {rec['block_id']!r}")

        compact = "".join(rec["text"].split())
        start_pos, end_pos = pos, pos + len(compact)

        block_rows.append(
            (
                order,
                rec["block_id"],
                rec["page"],
                rec["text"],
                rec["rect"]["x0"],
                rec["rect"]["y0"],
                rec["rect"]["x1"],
                rec["rect"]["y1"],
                start_pos,
                end_pos,
                len(compact),
            )
        )
        for offset, ch in enumerate(compact):
            uni_rows.append((ch, start_pos + offset))
        for offset in range(len(compact) - 1):
            bi_rows.append((compact[offset : offset + 2], start_pos + offset))
        if compact:
            if prev_last is not None:
                # Cross-block (possibly cross-page) boundary bigram. Its
                # position is that of the first character, i.e. the last
                # compact position of the previous *non-empty* block.
                bi_rows.append((prev_last + compact[0], start_pos - 1))
            prev_last = compact[-1]
        else:
            # A block containing only whitespace contributes no compact
            # characters; it must not bridge a boundary bigram either, since
            # its start_pos already equals the running position.
            prev_last = None

        pos = end_pos
        order += 1
        saw_record = True
        in_batch.add(rec["block_id"])

        if len(block_rows) >= config.INSERT_BATCH:
            _flush(
                conn, blocks_table, unigrams_table, bigrams_table,
                block_rows, uni_rows, bi_rows,
            )
            block_rows, uni_rows, bi_rows = [], [], []
            in_batch.clear()

    if not saw_record:
        raise IngestError(0, "upload contained no records")

    if block_rows:
        _flush(
            conn, blocks_table, unigrams_table, bigrams_table,
            block_rows, uni_rows, bi_rows,
        )
    return order
