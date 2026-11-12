"""Minimal ``starlette`` pieces used by the application under the test shim."""
from __future__ import annotations

import asyncio


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def run_in_threadpool(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


class Request:
    """Subset of starlette.requests.Request backed by a WSGI-like environ."""

    def __init__(self, body: bytes):
        self._body = body

    async def stream(self):
        # Deliver in small-ish pieces so the chunking logic is exercised.
        view = memoryview(self._body)
        step = 64 * 1024
        for i in range(0, len(view), step):
            yield bytes(view[i : i + step])


class BackgroundTasks:
    def __init__(self):
        self.tasks: list = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append((func, args, kwargs))

    def run_all(self) -> None:
        for func, args, kwargs in self.tasks:
            func(*args, **kwargs)
