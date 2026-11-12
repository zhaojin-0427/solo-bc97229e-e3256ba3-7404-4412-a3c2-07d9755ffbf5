"""Helpers shared by the test-suite."""
from __future__ import annotations

import io
import json
import os
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from app.config import DB_PATH


def reset_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(DB_PATH) + suffix)
        except FileNotFoundError:
            pass


def make_app():
    # Import after env vars are set so the configured DB path takes effect.
    from app.main import create_app

    return create_app()


def wsgi_request(app, method: str, path: str, body: bytes = b"",
                 query: dict | None = None) -> tuple[int, dict]:
    environ: dict = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = path
    if query:
        environ["QUERY_STRING"] = urlencode(query)
    environ["CONTENT_LENGTH"] = str(len(body))
    environ["wsgi.input"] = io.BytesIO(body)
    captured: dict = {}

    def start_response(status, headers):
        captured["status"] = int(status.split(" ", 1)[0])
        captured["headers"] = dict(headers)

    raw = b"".join(app(environ, start_response))
    return captured["status"], json.loads(raw.decode("utf-8"))


def ndjson(records: list[dict]) -> bytes:
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode()


def block(bid: str, page: int, text: str, rect=None) -> dict:
    return {
        "block_id": bid,
        "page": page,
        "text": text,
        "rect": rect or {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
    }
