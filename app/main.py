"""FastAPI application: streaming upload, versioned search, replacement workflow.

Concurrency model (single uvicorn worker)
-----------------------------------------
* An in-process lock per document enforces "at most one replacement at a time";
  the catalog table enforces the same rule inside SQLite as a backstop.
* Ingestion runs in a worker thread, fed through a bounded queue, so an
  arbitrarily large upload never resides in memory and never blocks the loop.
* Indexing happens after the upload response (202) is returned; the previous
  active version stays fully searchable until the atomic catalog switch.
* Search opens a short-lived read-only snapshot of the active version.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import config, db, ingest, search
from .schemas import (
    DocumentInfo,
    Health,
    RecoveryReport,
    ReplacementAccepted,
    SearchResponse,
    VersionInfo,
)

logger = logging.getLogger("archive")

DOC_ID_MAX_LEN = 200
# Bounded in-memory backlog of raw chunks for the ingestion thread.
CHUNK_QUEUE_MAX = 32


def _validate_doc_id(doc_id: str) -> str:
    if not doc_id or len(doc_id) > DOC_ID_MAX_LEN:
        raise HTTPException(status_code=400, detail="invalid doc_id")
    return doc_id


async def _stream_ingest(
    conn, vid: int, chunk_iter: Iterator[bytes]
) -> int:
    """Bridge an async byte stream to the synchronous ingestion routine."""
    q: queue.Queue = queue.Queue(maxsize=CHUNK_QUEUE_MAX)
    sentinel = object()
    producer_error: list[BaseException] = []
    loop = asyncio.get_running_loop()

    async def produce() -> None:
        try:
            async for chunk in chunk_iter:
                if chunk:
                    await loop.run_in_executor(None, q.put, chunk)
        except BaseException as exc:  # surfaced through the consumer
            producer_error.append(exc)
        finally:
            # Always unblock the consumer, even on client disconnect.
            q.put(sentinel)

    producer = asyncio.create_task(produce())

    def consume() -> int:
        def gen() -> Iterator[bytes]:
            while True:
                item = q.get()
                if item is sentinel:
                    return
                yield item

        return ingest.ingest_version(conn, vid, gen())

    try:
        result = await run_in_threadpool(consume)
    finally:
        if not producer.done():
            producer.cancel()
            with contextlib.suppress(Exception):
                await producer
    if producer_error and not isinstance(
        producer_error[0], asyncio.CancelledError
    ):
        raise producer_error[0]
    return result


def _index_in_background(doc_id: str, vid: int, block_count: int) -> None:
    """Promote a fully ingested version; any failure keeps the old version."""
    conn = db.get_conn()
    try:
        db.set_state(conn, vid, db.STATE_INDEXING)
        # Indexes were built incrementally during ingestion; the tables are
        # already durable. This step is the single atomic visibility switch.
        db.activate_version(conn, doc_id, vid, block_count)
        logger.info("document %r switched to version %s", doc_id, vid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("background indexing failed for version %s", vid)
        with contextlib.suppress(Exception):
            db.fail_version(conn, vid, f"indexing failed: {exc}")
    finally:
        conn.close()


def create_app() -> FastAPI:
    config.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.get_conn()
        try:
            db.init_schema(conn)
            recovery_messages.extend(db.startup_recovery(conn))
            for msg in recovery_messages:
                logger.warning("recovery: %s", msg)
        finally:
            conn.close()
        yield

    app = FastAPI(
        title="Archive OCR cross-page search API",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "Upload OCR block streams (UTF-8 NDJSON) per document and search "
            "the whitespace-insensitive text, including matches that cross "
            "blocks and scan pages. Re-uploading a document builds a new "
            "version that replaces the old one atomically."
        ),
    )

    locks: dict[str, asyncio.Lock] = {}
    recovery_messages: list[str] = []

    def lock_for(doc_id: str) -> asyncio.Lock:
        lock = locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[doc_id] = lock
        return lock

    @app.get("/health", response_model=Health, tags=["meta"])
    def health() -> Health:
        return Health(status="ok", db_path=str(config.DB_PATH))

    @app.get("/recovery", response_model=RecoveryReport, tags=["meta"])
    def recovery() -> RecoveryReport:
        return RecoveryReport(recovered=list(recovery_messages))

    @app.get("/documents", response_model=list[DocumentInfo], tags=["documents"])
    def list_documents() -> list[DocumentInfo]:
        conn = db.get_conn()
        try:
            return [DocumentInfo(**dict(row)) for row in db.list_documents(conn)]
        finally:
            conn.close()

    @app.get(
        "/documents/{doc_id}/versions",
        response_model=list[VersionInfo],
        tags=["documents"],
    )
    def list_versions(doc_id: str) -> list[VersionInfo]:
        _validate_doc_id(doc_id)
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT version_id, doc_id, state, created_at, finalized_at, "
                "block_count, error FROM versions WHERE doc_id = ? "
                "ORDER BY version_id",
                (doc_id,),
            ).fetchall()
            return [VersionInfo(**dict(row)) for row in rows]
        finally:
            conn.close()

    @app.put(
        "/documents/{doc_id}",
        response_model=ReplacementAccepted,
        status_code=202,
        tags=["documents"],
        responses={
            409: {"description": "a replacement for this document is already running"},
            400: {"description": "malformed NDJSON upload"},
            507: {"description": "storage write failure"},
        },
    )
    async def replace_document(
        doc_id: str, request: Request, background_tasks: BackgroundTasks
    ) -> ReplacementAccepted:
        """Replace (or create) a document with an NDJSON block stream.

        Body: one JSON object per line, each with unique ``block_id``,
        ``page``, ``text`` and ``rect`` (``x0/y0/x1/y1``). Line order is the
        reading order. The old version stays searchable while the new one is
        being prepared.
        """
        _validate_doc_id(doc_id)
        lock = lock_for(doc_id)
        if lock.locked():
            raise HTTPException(
                status_code=409,
                detail="a replacement for this document is already in progress",
            )

        async with lock:
            conn = db.get_conn()
            try:
                try:
                    vid = db.create_version(conn, doc_id)
                except RuntimeError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

                try:
                    block_count = await _stream_ingest(
                        conn, vid, request.stream()
                    )
                except ingest.IngestError as exc:
                    db.fail_version(conn, vid, str(exc))
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except OSError as exc:
                    db.fail_version(conn, vid, str(exc))
                    raise HTTPException(status_code=507, detail=str(exc)) from exc
            finally:
                conn.close()

        background_tasks.add_task(_index_in_background, doc_id, vid, block_count)
        return ReplacementAccepted(
            doc_id=doc_id,
            version_id=vid,
            state=db.STATE_INDEXING,
            detail="upload accepted; new version is being prepared",
        )

    @app.get(
        "/documents/{doc_id}/search",
        response_model=SearchResponse,
        tags=["search"],
        responses={404: {"description": "document not found or no active version"}},
    )
    def search_document(doc_id: str, q: str) -> SearchResponse:
        """Search one document.

        Whitespace in ``q`` and in the stored text is ignored. Matches may span
        multiple blocks and pages; overlapping matches are returned separately.
        At most the earliest 100 matches are returned.
        """
        _validate_doc_id(doc_id)
        compact_q = "".join(q.split())
        if not (config.QUERY_MIN_CHARS <= len(compact_q) <= config.QUERY_MAX_CHARS):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"query must contain {config.QUERY_MIN_CHARS}-"
                    f"{config.QUERY_MAX_CHARS} non-whitespace characters"
                ),
            )
        conn = db.get_conn()
        try:
            vid = db.get_active_version(conn, doc_id)
            if vid is None:
                raise HTTPException(
                    status_code=404,
                    detail="document not found or no searchable version yet",
                )
            try:
                result = search.search(conn, vid, q)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return SearchResponse(**result)
        finally:
            conn.close()

    return app


app = create_app()
