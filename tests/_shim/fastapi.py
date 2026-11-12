"""Minimal ``fastapi`` replacement used only by the offline test shim.

It records route registrations like the real FastAPI and exposes a WSGI
callable so ``wsgiref`` can drive the same application object end to end.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from urllib.parse import parse_qs

from starlette import BackgroundTasks, HTTPException, Request


def _serialize(value):
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


class FastAPI:
    def __init__(self, title: str = "", version: str = "", description: str = "",
                 lifespan=None, **_kwargs):
        self.title = title
        self.version = version
        self.description = description
        self.lifespan = lifespan
        self.routes: list[tuple[str, re.Pattern, list[str], object]] = []
        self._lifespan_started = False

    async def _ensure_started(self) -> None:
        if self._lifespan_started or self.lifespan is None:
            return
        ctx = self.lifespan(self)
        await ctx.__aenter__()
        self._lifespan_started = True

    def _register(self, method: str, path: str, handler, status_code=200, **_kwargs):
        pattern = re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", path)
        self.routes.append(
            (method, re.compile(f"^{pattern}$"),
             re.findall(r"\{([^}]+)\}", path), handler, status_code)
        )
        return handler

    def get(self, path: str, **kwargs):
        def decorator(handler):
            return self._register("GET", path, handler, **kwargs)
        return decorator

    def put(self, path: str, **kwargs):
        def decorator(handler):
            return self._register("PUT", path, handler, **kwargs)
        return decorator

    def post(self, path: str, **kwargs):
        def decorator(handler):
            return self._register("POST", path, handler, **kwargs)
        return decorator

    async def _resolve(self, method: str, path: str, query: dict, body: bytes):
        for route_method, pattern, params, handler, status_code in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            kwargs = {name: match.group(name) for name in params}
            signature = inspect.signature(handler)
            if "q" in signature.parameters:
                kwargs["q"] = query.get("q", [""])[0]
            if "request" in signature.parameters:
                kwargs["request"] = Request(body)
            if "background_tasks" in signature.parameters:
                kwargs["background_tasks"] = BackgroundTasks()
            result = handler(**kwargs)
            if inspect.iscoroutine(result):
                result = await result
            background = kwargs.get("background_tasks")
            if background is not None:
                background.run_all()
            return status_code, result
        raise HTTPException(404, f"no route for {method} {path}")

    def __call__(self, environ, start_response):
        """WSGI entry point used by wsgiref in the offline test harness."""

        async def run():
            length = int(environ.get("CONTENT_LENGTH") or 0)
            body = environ["wsgi.input"].read(length) if length else b""
            query = parse_qs(environ.get("QUERY_STRING", ""))
            await self._ensure_started()
            return await self._resolve(
                environ["REQUEST_METHOD"],
                environ["PATH_INFO"],
                query,
                body,
            )

        try:
            status, result = asyncio.run(run())
            payload = _serialize(result)
            data = json.dumps(
                payload, ensure_ascii=False, default=str
            ).encode("utf-8")
            start_response(
                f"{status} STATUS",
                [("content-type", "application/json; charset=utf-8"),
                 ("content-length", str(len(data)))],
            )
            return [data]
        except HTTPException as exc:
            data = json.dumps(
                {"detail": exc.detail}, ensure_ascii=False, default=str
            ).encode("utf-8")
            start_response(
                f"{exc.status_code} ERROR",
                [("content-type", "application/json; charset=utf-8"),
                 ("content-length", str(len(data)))],
            )
            return [data]
