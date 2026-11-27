"""Response compression for the API: complete bodies only, never streams.

Starlette's ``GZipMiddleware`` skips ``text/event-stream`` but compresses every
other streaming response chunk by chunk through one ``GzipFile`` that is never
flushed, so an NDJSON or CSV stream reaches the client in compressor-sized
lumps instead of row by row. Placed outside ``@app.middleware("http")``
layers it is worse: ``BaseHTTPMiddleware`` re-sends every body as a stream,
so all responses take that path.

``ApiGzipMiddleware`` compresses a response only when the application sent
its whole body in one message (``more_body`` false), which is what
``JSONResponse`` and FastAPI's serialised return values do. Anything sent in
pieces (SSE, NDJSON, CSV and file downloads) passes through untouched, so
incremental delivery cannot be broken by construction; a start message
without ``content-length`` or of a streaming type is forwarded at once
rather than held for the first chunk. A strong ``ETag`` on a compressed
response is made weak. It must be the
innermost user middleware (added to the app first) to see the route's own
messages before ``BaseHTTPMiddleware`` re-chunks them.
"""

from __future__ import annotations

import gzip

import anyio
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Bodies smaller than this are sent as they are: the gzip header and the
# CPU are not worth it.
API_MIN_SIZE = 1024
# Level 6 is zlib's default: most of level 9's ratio at a fraction of the CPU,
# which matters because API bodies are compressed per request.
API_COMPRESS_LEVEL = 6
# Larger bodies are compressed on a worker thread so a multi-megabyte query
# result never stalls the event loop.
_OFFLOAD_SIZE = 64 * 1024

_COMPRESSIBLE_PREFIXES = ("text/",)
_COMPRESSIBLE_TYPES = {
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-ndjson",
    "application/manifest+json",
    "application/problem+json",
    "image/svg+xml",
}


def is_compressible_type(content_type: str) -> bool:
    """True for textual media types worth gzipping (never event streams)."""
    base = content_type.split(";", 1)[0].strip().lower()
    if not base or base == "text/event-stream":
        return False
    return base.startswith(_COMPRESSIBLE_PREFIXES) or base in _COMPRESSIBLE_TYPES


# Media types that are streamed by nature: never held back, never compressed.
_STREAMING_TYPES = {"application/x-ndjson", "text/csv", "text/event-stream"}


def _may_be_complete(headers: Headers, content_type: str) -> bool:
    """Whether a start message can be followed by one complete body.

    Starlette's ``Response`` (and so ``JSONResponse``, ``HTMLResponse`` and
    FastAPI's serialised return values) always sets ``content-length`` from
    its body; ``StreamingResponse`` never does. Without it, or for a type
    that is streamed by nature, the response is a stream and its headers go
    out at once.
    """
    if "content-length" not in headers:
        return False
    return content_type.split(";", 1)[0].strip().lower() not in _STREAMING_TYPES


def accepted_encodings(header: str | None) -> set[str]:
    """Content codings an ``Accept-Encoding`` header allows (q=0 excluded).

    ``*`` is expanded to the codings this server can produce or serve.
    """
    allowed: set[str] = set()
    refused: set[str] = set()
    wildcard = False
    for part in (header or "").split(","):
        token, *params = [p.strip() for p in part.split(";")]
        token = token.lower()
        if not token:
            continue
        q = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q <= 0:
            refused.add(token)
        elif token == "*":
            wildcard = True
        else:
            allowed.add(token)
    if wildcard:
        allowed |= {"gzip", "br"} - refused
    return allowed - refused


class ApiGzipMiddleware:
    """Gzip complete, textual responses under ``prefix`` when accepted."""

    def __init__(self, app: ASGIApp, prefix: str = "/api/") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        wants_gzip = "gzip" in accepted_encodings(Headers(scope=scope).get("accept-encoding"))
        start: Message | None = None
        passthrough = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            kind = message["type"]
            if kind == "http.response.start":
                headers = Headers(raw=message.get("headers", []))
                content_type = headers.get("content-type", "")
                if (
                    "content-encoding" in headers
                    or not is_compressible_type(content_type)
                    or not _may_be_complete(headers, content_type)
                ):
                    # Sent at once: a stream's headers must not wait for
                    # its first chunk, which may be slow work away.
                    passthrough = True
                    await send(message)
                else:
                    # Held back until the first body shows whether this is a
                    # complete response or a stream.
                    start = message
                return
            if start is None:  # pragma: no cover - body before start
                await send(message)
                return
            held, start = start, None
            passthrough = True
            if kind != "http.response.body" or message.get("more_body", False):
                # A stream (or pathsend): forward as is, chunk by chunk.
                await send(held)
                await send(message)
                return
            headers = MutableHeaders(scope=held)
            headers.add_vary_header("Accept-Encoding")
            body = message.get("body", b"")
            if wants_gzip and len(body) >= API_MIN_SIZE:
                if len(body) >= _OFFLOAD_SIZE:
                    compressed = await anyio.to_thread.run_sync(gzip.compress, body, API_COMPRESS_LEVEL)
                else:
                    compressed = gzip.compress(body, API_COMPRESS_LEVEL)
                if len(compressed) < len(body):
                    headers["Content-Encoding"] = "gzip"
                    headers["Content-Length"] = str(len(compressed))
                    etag = headers.get("etag")
                    if etag and not etag.startswith("W/"):
                        # The route's strong validator names the identity
                        # bytes; the gzipped bytes are a different
                        # representation, so only a weak one fits both.
                        headers["ETag"] = "W/" + etag
                    message = {**message, "body": compressed}
            await send(held)
            await send(message)

        await self.app(scope, receive, send_wrapper)
