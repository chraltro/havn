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


def test_gzip_only_when_accepted(client):
    r = client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip, deflate"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["vary"] == "Accept-Encoding"
    assert int(r.headers["content-length"]) < len(APP_JS)
    # httpx decodes transparently; check the decoded body is the original.
    assert r.content == APP_JS

    plain = client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.headers["vary"] == "Accept-Encoding"
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


def test_already_compressed_formats_not_compressed(client):
    for path in ("/assets/font-9f8e.woff2", "/onboarding/dag.png"):
        r = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert "vary" not in r.headers


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

    server_app._force_checkpoint()
    assert deps._get_shared_conn().execute("SELECT 1").fetchone() == (1,)
    assert deps._inflight["count"] == 0
