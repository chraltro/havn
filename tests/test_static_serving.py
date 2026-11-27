"""Frontend serving (caching, compression, types, 404s), API gzip and the
background checkpoint."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading

import duckdb
import pytest
from fastapi.testclient import TestClient

APP_JS = ("export const x = 1;\n" * 400).encode()  # compressible, > 1 KB
INDEX_HTML = (
    "<!doctype html><html><head><script type=module src=/assets/app-abc123.js></script>"
    "</head><body>" + "<div>havn</div>" * 200 + "</body></html>"
).encode()


@pytest.fixture
def frontend(tmp_path, monkeypatch):
    import havn.server.app as server_app
    from havn.server import static_files

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX_HTML)
    (dist / "assets" / "app-abc123.js").write_bytes(APP_JS)
    (dist / "assets" / "codicon-1a2b.ttf").write_bytes(b"\x00\x01\x00\x00" + b"\x00" * 3000)
    (dist / "assets" / "font-9f8e.woff2").write_bytes(b"wOF2" + bytes(range(256)) * 8)
    (dist / "manifest.json").write_bytes(json.dumps({"name": "havn", "pad": "x" * 2000}).encode())
    (dist / "favicon.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    (dist / "onboarding").mkdir()
    (dist / "onboarding" / "dag.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    # A secret next to the build, which traversal must never reach.
    (tmp_path / "secret.js").write_bytes(b"SECRET")
    (tmp_path / ".env").write_bytes(b"SECRET=1")
    monkeypatch.setattr(server_app, "_FRONTEND_DIR", dist)
    static_files.clear_caches()
    return dist


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\n")
    duckdb.connect(str(proj / "warehouse.duckdb")).close()
    return proj


@pytest.fixture
def client(project, frontend, monkeypatch):
    import havn.server.app as server_app

    monkeypatch.setattr(server_app, "PROJECT_DIR", project)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", False)
    # Keep the periodic checkpoint out of the way unless a test wants it.
    monkeypatch.setattr(server_app, "_last_checkpoint", float("inf"))
    return TestClient(server_app.app)


# --- caching ---------------------------------------------------------------


def test_hashed_asset_is_immutable(client):
    r = client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.content == APP_JS


def test_index_is_revalidated_with_etag_and_304(client):
    r = client.get("/", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.content == INDEX_HTML
    etag = r.headers["etag"]
    again = client.get("/", headers={"If-None-Match": etag, "Accept-Encoding": "identity"})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    # Weak form and lists match too; a different tag does not.
    assert client.get("/index.html", headers={"If-None-Match": f'"x", W/{etag}',
                                              "Accept-Encoding": "identity"}).status_code == 304
    assert client.get("/", headers={"If-None-Match": '"other"'}).status_code == 200


def test_new_release_changes_index_etag(client, frontend):
    first = client.get("/", headers={"Accept-Encoding": "identity"}).headers["etag"]
    (frontend / "index.html").write_bytes(INDEX_HTML.replace(b"abc123", b"def456") + b" ")
    r = client.get("/", headers={"If-None-Match": first, "Accept-Encoding": "identity"})
    assert r.status_code == 200 and b"def456" in r.content
    assert r.headers["etag"] != first


def test_spa_fallback_is_index_with_no_cache(client):
    r = client.get("/develop/editor", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.content == INDEX_HTML
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert "etag" in r.headers


def test_other_root_files_short_max_age(client):
    r = client.get("/favicon.svg")
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "etag" in r.headers


# --- compression -----------------------------------------------------------


def _vary(response) -> set[str]:
    """Tokens of the Vary header. Other middleware may add its own (newer
    Starlette's CORS layer always adds Origin), so tests look for a token
    instead of comparing the whole header."""
    return {t.strip().lower() for t in response.headers.get("vary", "").split(",") if t.strip()}


def test_gzip_only_when_accepted(client):
    r = client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip, deflate"})
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in _vary(r)
    assert int(r.headers["content-length"]) < len(APP_JS)
    # httpx decodes transparently; check the decoded body is the original.
    assert r.content == APP_JS

    plain = client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert "accept-encoding" in _vary(plain)
    assert plain.content == APP_JS
    # Each representation has its own ETag.
    assert plain.headers["etag"] != r.headers["etag"]

    refused = client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip;q=0"})
    assert "content-encoding" not in refused.headers


def test_gzip_bytes_decode_to_original(client):
    with client.stream("GET", "/", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert r.headers["content-encoding"] == "gzip"
    assert gzip.decompress(raw) == INDEX_HTML


def test_compressed_bytes_are_cached(client, monkeypatch):
    from havn.server import static_files

    calls = []
    real = gzip.compress
    monkeypatch.setattr(static_files.gzip, "compress", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(3):
        client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip"})
    assert len(calls) == 1
    assert static_files._compressed.size > 0


def test_cache_is_bounded():
    from havn.server.static_files import _BytesCache

    cache = _BytesCache(1000)
    for i in range(10):
        cache.put((i,), b"x" * 200)
    assert cache.size <= 1000
    assert cache.get((0,)) is None and cache.get((9,)) is not None
    cache.put(("big",), b"x" * 600)  # more than a quarter of the bound: not kept
    assert cache.get(("big",)) is None


def test_precompressed_sibling_preferred(client, frontend):
    br_body = b"pretend-brotli-bytes"
    (frontend / "assets" / "app-abc123.js.br").write_bytes(br_body)
    with client.stream("GET", "/assets/app-abc123.js", headers={"Accept-Encoding": "br, gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert r.headers["content-encoding"] == "br"
    assert raw == br_body
    gz = gzip.compress(APP_JS)
    (frontend / "assets" / "app-abc123.js.gz").write_bytes(gz)
    with client.stream("GET", "/assets/app-abc123.js", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert r.headers["content-encoding"] == "gzip" and raw == gz


def test_precompressed_sibling_has_its_own_etag(client, frontend):
    """A prebuilt .gz and on-the-fly gzip are different bytes: they must never
    share a validator, or a client holding one gets 304 for the other."""
    gz_headers = {"Accept-Encoding": "gzip"}
    on_the_fly = client.get("/assets/app-abc123.js", headers=gz_headers).headers["etag"]

    sibling = frontend / "assets" / "app-abc123.js.gz"
    sibling.write_bytes(gzip.compress(APP_JS, compresslevel=1))
    r = client.get("/assets/app-abc123.js", headers=gz_headers)
    pre = r.headers["etag"]
    assert pre != on_the_fly
    # Holding the on-the-fly validator does not earn a 304 for the sibling.
    assert client.get("/assets/app-abc123.js",
                      headers={**gz_headers, "If-None-Match": on_the_fly}).status_code == 200
    assert client.get("/assets/app-abc123.js",
                      headers={**gz_headers, "If-None-Match": pre}).status_code == 304

    # Different sibling bytes, same source file: a different validator.
    import os

    sibling.write_bytes(gzip.compress(APP_JS, compresslevel=9) + b"")
    st = sibling.stat()
    os.utime(sibling, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    again = client.get("/assets/app-abc123.js", headers=gz_headers)
    assert again.headers["content-encoding"] == "gzip"
    assert again.headers["etag"] != pre
    assert client.get("/assets/app-abc123.js",
                      headers={**gz_headers, "If-None-Match": pre}).status_code == 200


def test_already_compressed_formats_not_compressed(client):
    for path in ("/assets/font-9f8e.woff2", "/onboarding/dag.png"):
        r = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert "accept-encoding" not in _vary(r)


# --- content types ---------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/assets/app-abc123.js", "text/javascript"),
    ("/assets/font-9f8e.woff2", "font/woff2"),
    ("/assets/codicon-1a2b.ttf", "font/ttf"),
    ("/manifest.json", "application/json"),
    ("/favicon.svg", "image/svg+xml"),
    ("/onboarding/dag.png", "image/png"),
    ("/index.html", "text/html"),
])
def test_content_types(client, path, expected):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == expected


def test_content_type_overrides_beat_registry():
    from pathlib import Path

    from havn.server.static_files import content_type_for

    assert content_type_for(Path("a.js")) == "text/javascript"
    assert content_type_for(Path("a.js.map")) == "application/json"
    assert content_type_for(Path("a.wasm")) == "application/wasm"
    assert content_type_for(Path("a.webp")) == "image/webp"
    assert content_type_for(Path("a.jpg")) == "image/jpeg"
    # .gz is an encoding to mimetypes, not a type: never "text/html" for index.html.gz.
    assert content_type_for(Path("index.html.gz")) == "application/octet-stream"


# --- 404s, fallback and guards ---------------------------------------------


@pytest.mark.parametrize("path", [
    "/assets/app-OLDHASH.js",
    "/assets/missing",
    "/missing.css",
    "/onboarding/nope.png",
    "/fonts/x.woff2",
])
def test_missing_asset_is_404(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert b"<html" not in r.content.lower()


@pytest.mark.parametrize("path", ["/develop", "/explore/dag", "/p/s8b7xYz2"])
def test_route_like_paths_fall_back(client, path):
    r = client.get(path, headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.content == INDEX_HTML


@pytest.mark.parametrize("path", [
    "/%2e%2e%2fsecret.js",
    "/assets/%2e%2e%2f%2e%2e%2fsecret.js",
    "/%2e%2e%2f.env",
    "/..%2f..%2f.env",
])
def test_traversal_guard(client, path):
    r = client.get(path, headers={"Accept-Encoding": "identity"})
    assert b"SECRET" not in r.content
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.content == INDEX_HTML


# Sent by a real client: httpx keeps percent-escapes in the path, Starlette
# decodes them, and the route's :path parameter gets the decoded string.
HOSTILE_PATHS = [
    "/%2F%2Fattacker.example/share/x.js",
    "/%2F%2F%2Fattacker.example/share/x.js",
    "/%5C%5Cattacker.example%5Cshare%5Cx.js",
    "/%5C%5C%3F%5CUNC%5Cattacker.example%5Cshare%5Cx.js",
    "/%2F%5Cattacker.example%5Cshare%5Cx.js",
    "/C:/Windows/win.ini",
    "/C:%5CWindows%5Cwin.ini",
    "/c:secret.js",
    "/%2Fetc/passwd",
    "/%2e%2e%2fsecret.js",
    "/%2e%2e%5csecret.js",
    "/assets/..%5C..%5Csecret.js",
    "/assets/%2e%2e%2f%2e%2e%2fsecret.js",
    "/assets/...%5C...%5Csecret.js",
    "/x%00.js",
]


@pytest.mark.parametrize("path", HOSTILE_PATHS)
def test_hostile_paths_never_reach_resolve_outside_the_frontend(
    client, frontend, project, monkeypatch, path
):
    """No lookup of a path outside the build, not even one the containment
    check would refuse afterwards (on Windows, resolving \\\\host\\share opens
    an SMB connection). The server's own project directory is resolved by
    the middleware and is not attacker-chosen."""
    import os
    from pathlib import Path

    allowed = tuple(os.path.normcase(os.path.abspath(p)) for p in (frontend, project))
    seen: list[str] = []
    real_resolve = Path.resolve

    def spy(self, *args, **kwargs):
        seen.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    r = client.get(path, headers={"Accept-Encoding": "identity"})
    monkeypatch.setattr(Path, "resolve", real_resolve)
    assert b"SECRET" not in r.content
    assert r.status_code in (200, 404)
    outside = [
        p for p in seen
        if "\x00" in p
        or not os.path.normcase(os.path.normpath(p)).startswith(allowed)
    ]
    assert outside == [], f"resolve() was called on {outside}"


@pytest.mark.parametrize("path,size", [
    ("/onboarding/dag.png", 108),
    ("/assets/app-abc123.js", len(APP_JS)),
    ("/assets/sub/deeper/chunk-1.js", 5),
])
def test_nested_paths_are_still_served(client, frontend, path, size):
    (frontend / "assets" / "sub" / "deeper").mkdir(parents=True)
    (frontend / "assets" / "sub" / "deeper" / "chunk-1.js").write_bytes(b"x=1;\n")
    r = client.get(path, headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert len(r.content) == size


def test_unsafe_static_path_rules():
    from havn.server.app import _unsafe_static_path

    for bad in ["/x", "\\x", "//h/s/x", "\\\\h\\s\\x", "C:/x", "c:x", "C:\\x",
                "../x", "a/../x", "a\\..\\x", "a/.../x", "a/.. /x", "x\x00.js"]:
        assert _unsafe_static_path(bad), bad
    for ok in ["", "index.html", "assets/app.js", "onboarding/dag.png", "a/b/c.js",
               "develop/editor", "p/s8b7xYz2", "a..b.js", ".well-known/x"]:
        assert not _unsafe_static_path(ok), ok


def test_unknown_api_and_reserved_paths_404(client):
    r = client.get("/api/x")
    assert r.status_code == 404
    assert r.json()["detail"] == "API endpoint not found"
    assert client.get("/api").status_code == 404
    assert client.get("/redoc").status_code == 200  # FastAPI's own page wins
    assert client.get("/openapi.json").headers["content-type"].startswith("application/json")


def test_frontend_not_built_page(client, frontend, monkeypatch):
    import havn.server.app as server_app

    monkeypatch.setattr(server_app, "_FRONTEND_DIR", frontend / "nowhere")
    r = client.get("/develop")
    assert r.status_code == 200
    assert b"Frontend not built" in r.content


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-xss-protection"] == "0"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" not in r.headers
    # Published pages keep their own rules, including no-store over no-cache.
    p = client.get("/p/somekey")
    assert p.headers["cache-control"] == "no-store"
    assert p.headers["referrer-policy"] == "no-referrer"
    assert "x-frame-options" not in p.headers


# --- API compression -------------------------------------------------------


def test_api_json_gzipped_when_accepted(client):
    body = {"sql": "SELECT i, 'row number ' || i AS label FROM range(500) t(i)"}
    r = client.post("/api/query", json=body, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200, r.text
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()
    assert len(r.json()["rows"]) == 500
    plain = client.post("/api/query", json=body, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.json()["rows"] == r.json()["rows"]
    # Small bodies are not worth it.
    small = client.get("/api/x", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers


def _drive(app, path, *, accept="gzip, deflate, br"):
    """Run one request through the full ASGI stack and watch messages arrive.

    TestClient collects the whole body before returning, so it cannot show
    incremental delivery; this drives the app directly.
    """
    async def run():
        messages: list[dict] = []
        first_body = asyncio.Event()
        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"accept-encoding", accept.encode())],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        }
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_body.wait(), timeout=10)
        still_streaming = not task.done()
        snapshot = list(messages)
        disconnect.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            task.cancel()
        return snapshot, still_streaming

    return asyncio.run(run())


def test_sse_still_delivered_incrementally(client):
    """/api/resources/stream sends a snapshot, then sleeps 2 s, forever.

    The first event must reach the client while the stream is still open,
    uncompressed, even though the request accepts gzip.
    """
    import havn.server.app as server_app

    messages, still_streaming = _drive(server_app.app, "/api/resources/stream")
    assert still_streaming
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in headers
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert body.startswith(b"data: ")


def test_api_gzip_passes_streams_through():
    """Unit check: a multi-chunk response is forwarded chunk by chunk."""
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    from havn.server.compression import ApiGzipMiddleware

    async def rows(request):
        async def gen():
            for i in range(3):
                yield json.dumps({"i": i, "pad": "x" * 2000}) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    app = ApiGzipMiddleware(Starlette(routes=[Route("/api/rows", rows)]))
    r = TestClient(app).get("/api/rows", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers
    assert len(r.text.splitlines()) == 3


def test_gzipped_api_response_weakens_a_strong_etag(client, project):
    """GET /api/files/... sets a strong ETag for the identity bytes; gzipped,
    the same strong tag would name a second byte sequence."""
    (project / "transform" / "bronze").mkdir(parents=True)
    sql = "SELECT 1 AS a\n" + "-- padding line to make the body compressible\n" * 60
    (project / "transform" / "bronze" / "x.sql").write_text(sql)
    plain = client.get("/api/files/transform/bronze/x.sql", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200, plain.text
    strong = plain.headers["etag"]
    assert not strong.startswith("W/")
    gz = client.get("/api/files/transform/bronze/x.sql", headers={"Accept-Encoding": "gzip"})
    assert gz.headers["content-encoding"] == "gzip"
    assert gz.headers["etag"] == "W/" + strong
    # The save path's conflict check uses the body's file_hash, which is
    # unchanged by compression, so a save from a gzipped read still succeeds.
    assert gz.json()["file_hash"] == plain.json()["file_hash"]
    r = client.put("/api/files/transform/bronze/x.sql",
                   json={"content": sql + "-- edit\n", "expected_hash": gz.json()["file_hash"]})
    assert r.status_code == 200, r.text


def test_gzip_leaves_a_weak_etag_alone():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from havn.server.compression import ApiGzipMiddleware

    async def doc(request):
        return JSONResponse({"pad": "x" * 4000}, headers={"ETag": 'W/"v1"'})

    app = ApiGzipMiddleware(Starlette(routes=[Route("/api/doc", doc)]))
    r = TestClient(app).get("/api/doc", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["etag"] == 'W/"v1"'


@pytest.mark.parametrize("media_type,length", [
    ("application/x-ndjson", False),
    ("text/csv", False),
    ("application/json", False),  # compressible type, but no content-length
    ("text/csv", True),  # a streaming type, even with a length
])
def test_stream_headers_are_not_held_for_the_first_chunk(media_type, length):
    """A stream that does slow work before its first yield must still send
    its status and headers at once."""
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    from havn.server.compression import ApiGzipMiddleware

    async def slow(request):
        async def gen():
            await asyncio.Event().wait()  # never yields: slow work, forever
            yield b""
        headers = {"Content-Length": "100"} if length else None
        return StreamingResponse(gen(), media_type=media_type, headers=headers)

    app = ApiGzipMiddleware(Starlette(routes=[Route("/api/slow", slow)]))

    async def run():
        sent: list[dict] = []
        started = asyncio.Event()

        async def receive():
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                started.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/api/slow", "raw_path": b"/api/slow",
            "query_string": b"", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"accept-encoding", b"gzip")],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return sent

    sent = asyncio.run(run())
    assert [m["type"] for m in sent] == ["http.response.start"]
    headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
    assert "content-encoding" not in headers


def test_accepted_encodings_parsing():
    from havn.server.compression import accepted_encodings

    assert accepted_encodings("gzip, deflate, br") == {"gzip", "deflate", "br"}
    assert accepted_encodings("gzip;q=0, br") == {"br"}
    assert accepted_encodings("*") == {"gzip", "br"}
    assert accepted_encodings("*, gzip;q=0") == {"br"}
    assert accepted_encodings(None) == set()
    assert accepted_encodings("GZIP;q=0.5") == {"gzip"}


# --- checkpoint ------------------------------------------------------------


def test_checkpoint_runs_off_the_request_path(client, monkeypatch):
    import havn.server.app as server_app

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_checkpoint():
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(timeout=30)

    monkeypatch.setattr(server_app, "_force_checkpoint", slow_checkpoint)
    monkeypatch.setattr(server_app, "_last_checkpoint", 0.0)
    import time

    try:
        t0 = time.monotonic()
        r = client.get("/api/x")
        elapsed = time.monotonic() - t0
        assert r.status_code == 404
        assert started.wait(timeout=5), "checkpoint was not started"
        # The response came back while the checkpoint is still blocked (it
        # waits up to 30 s); inline, the request would have taken that long.
        assert elapsed < 5
        assert not release.is_set()
        # A request while one runs does not start a second, even when due.
        monkeypatch.setattr(server_app, "_last_checkpoint", 0.0)
        assert client.get("/api/x").status_code == 404
        assert len(calls) == 1
        assert calls[0] == "havn-checkpoint"
    finally:
        release.set()
    for _ in range(100):
        if not server_app._checkpoint_running:
            break
        threading.Event().wait(0.05)
    assert not server_app._checkpoint_running


def test_checkpoint_not_due_does_nothing(client, monkeypatch):
    import time

    import havn.server.app as server_app

    calls = []
    monkeypatch.setattr(server_app, "_force_checkpoint", lambda: calls.append(1))
    monkeypatch.setattr(server_app, "_last_checkpoint", time.time())
    client.get("/api/x")
    assert calls == []


def test_force_checkpoint_on_real_warehouse(client):
    """The real checkpoint runs on a cursor, leaves the shared connection
    usable and does not leave the in-flight counter raised."""
    import havn.server.app as server_app
    from havn.server import deps

    from contextlib import contextmanager

    deps._get_shared_conn()  # the checkpoint only uses a connection that exists
    calls = []
    real = deps.background_write_cursor

    @contextmanager
    def watching():
        with real() as cur:
            calls.append(cur)
            yield cur

    deps.background_write_cursor = watching
    try:
        server_app._force_checkpoint()
    finally:
        deps.background_write_cursor = real
    assert calls and calls[0] is not None
    assert deps._get_shared_conn().execute("SELECT 1").fetchone() == (1,)
    assert deps._inflight["count"] == 0


def test_checkpoint_never_opens_the_warehouse(client):
    import havn.server.app as server_app
    from havn.server import deps

    deps.reset_shared_conn()
    server_app._force_checkpoint()
    assert deps._write_queue is None


def test_running_checkpoint_counts_as_in_flight(client, monkeypatch):
    """While a checkpoint runs, after the request that started it has
    finished, a branch switch must see the warehouse as busy."""
    import havn.server.app as server_app
    from havn.server import deps

    started = threading.Event()
    release = threading.Event()

    def blocked_checkpoint():
        started.set()
        release.wait(timeout=30)

    monkeypatch.setattr(server_app, "_force_checkpoint", blocked_checkpoint)
    monkeypatch.setattr(server_app, "_last_checkpoint", 0.0)
    try:
        assert client.get("/api/x").status_code == 404
        assert started.wait(timeout=5)
        # The request is over; only the checkpoint is left in flight.
        for _ in range(100):
            if deps._inflight["count"] == 1:
                break
            threading.Event().wait(0.02)
        assert deps._inflight["count"] == 1
        assert deps._switch_blocker(0) is not None
    finally:
        release.set()
    for _ in range(100):
        if not server_app._checkpoint_running and deps._inflight["count"] == 0:
            break
        threading.Event().wait(0.05)
    assert deps._inflight["count"] == 0
    assert deps._switch_blocker(0) is None


def test_failed_thread_start_releases_the_in_flight_count(client, monkeypatch):
    import havn.server.app as server_app
    from havn.server import deps

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(server_app._threading, "Thread", NoThread)
    monkeypatch.setattr(server_app, "_last_checkpoint", 0.0)
    assert server_app._maybe_start_checkpoint() is False
    assert deps._inflight["count"] == 0
    assert not server_app._checkpoint_running


def test_reset_waits_for_a_running_checkpoint_and_interrupts_it(client):
    """reset_shared_conn() must not close the connection while the background
    cursor is in use; it interrupts the checkpoint and waits for it."""
    from havn.server import deps

    deps._get_shared_conn()
    inside = threading.Event()
    leave = threading.Event()
    interrupted = []
    order: list[str] = []

    def background():
        with deps.background_write_cursor() as cur:
            assert cur is not None
            # Record the interrupt instead of delivering it: nothing runs yet.
            deps._checkpoint_cursor = _InterruptSpy(interrupted)
            inside.set()
            leave.wait(timeout=30)
            cur.execute("SELECT 1").fetchone()  # the connection is still open
            order.append("checkpoint done")

    t = threading.Thread(target=background)
    t.start()
    assert inside.wait(timeout=5)
    resetter = threading.Thread(target=lambda: (deps.reset_shared_conn(), order.append("reset")))
    resetter.start()
    for _ in range(100):
        if interrupted:
            break
        threading.Event().wait(0.02)
    assert interrupted, "the reset did not interrupt the checkpoint"
    assert order == []  # still waiting
    leave.set()
    t.join(timeout=10)
    resetter.join(timeout=10)
    assert order == ["checkpoint done", "reset"]
    assert deps._write_queue is None


class _InterruptSpy:
    def __init__(self, calls):
        self._calls = calls

    def interrupt(self):
        self._calls.append(1)


_RESET_RACE_SCRIPT = r"""
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import duckdb
from fastapi.testclient import TestClient

import havn.server.app as server_app
from havn.server import deps

proj = Path(sys.argv[2])
server_app.PROJECT_DIR = proj
server_app.AUTH_ENABLED = False
server_app._CHECKPOINT_INTERVAL = -1  # every request starts a checkpoint
server_app._last_checkpoint = 0.0
client = TestClient(server_app.app)
for i in range(int(sys.argv[3])):
    client.get("/api/tables")  # starts a background checkpoint...
    deps.reset_shared_conn()   # ...which a reset then races, as between tests
deps.reset_shared_conn()
print("clean exit")
"""


def test_connection_resets_racing_checkpoints_do_not_crash(project):
    """Each request starts a background checkpoint and a reset follows at
    once. Before the fix this killed the process natively (no traceback)
    within a few iterations, so it runs in a subprocess."""
    import subprocess
    import sys
    from pathlib import Path

    import havn

    src = str(Path(havn.__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, "-c", _RESET_RACE_SCRIPT, src, str(project), "60"],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:])
    assert "clean exit" in proc.stdout
