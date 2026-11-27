"""Serving the built frontend: content types, caching and compression.

The policy (which file gets which ``Cache-Control``, when to 404 and when to
fall back to the SPA shell) lives with ``serve_frontend`` in ``app.py``; this
module turns one resolved file into a response:

- the content type from an explicit table first, then ``mimetypes``. On
  Windows ``mimetypes`` reads the registry, which can map ``.js`` to
  ``text/plain`` (module scripts then refuse to load) and maps ``.map`` to
  ``text/plain``; the table wins for every type the build ships.
- an ``ETag`` from the file's content, per encoding, answering
  ``If-None-Match`` with 304.
- compression for textual types when ``Accept-Encoding`` allows it: a
  ``.br``/``.gz`` sibling written by the build if one is there and not older
  than the file, otherwise gzip done once and kept in a bounded in-memory
  cache keyed by path, mtime and size.
"""

from __future__ import annotations

import gzip
import hashlib
import mimetypes
import threading
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from starlette.responses import Response

from havn.server.compression import accepted_encodings

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_REVALIDATE = "no-cache"
# Unhashed files at the build root (favicon, logo, onboarding images): fresh
# for an hour, then revalidated by ETag, so a release reaches them quickly.
CACHE_SHORT = "public, max-age=3600"

_CONTENT_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".wasm": "application/wasm",
    ".txt": "text/plain",
    ".xml": "application/xml",
}

# Worth compressing. Images other than SVG, woff/woff2 and archives are
# already compressed and are sent as they are.
_COMPRESSIBLE_SUFFIXES = {
    ".html", ".js", ".mjs", ".css", ".json", ".map", ".webmanifest", ".svg",
    ".txt", ".xml", ".wasm", ".ttf", ".otf", ".eot", ".ico",
}

# A request whose last segment carries one of these is a file, never a
# client-side route: when the file is missing it gets 404, not index.html.
STATIC_SUFFIXES = set(_CONTENT_TYPES) | {".gz", ".br", ".zip"}

_MIN_COMPRESS_SIZE = 1024
_PRECOMPRESSED = (("br", ".br"), ("gzip", ".gz"))


def content_type_for(path: Path) -> str:
    """Media type for a static file (charset is added by the response)."""
    suffix = path.suffix.lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    guessed, encoding = mimetypes.guess_type(path.name)
    if guessed and encoding is None:
        return guessed
    return "application/octet-stream"


def looks_like_asset(path: str) -> bool:
    """True when a URL path names a static file rather than an SPA route."""
    posix = PurePosixPath(path.replace("\\", "/"))
    if posix.parts and posix.parts[0] == "assets":
        return True
    return posix.suffix.lower() in STATIC_SUFFIXES


class _BytesCache:
    """Thread-safe LRU of bytes with a bound on the total size."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple, value: bytes) -> None:
        if len(value) > self.max_bytes // 4:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._items[key] = value
            self._size += len(value)
            while self._size > self.max_bytes and self._items:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0

    @property
    def size(self) -> int:
        return self._size


# Compressed bodies of the built app (a few MB in total) and content hashes.
_compressed = _BytesCache(64 * 1024 * 1024)
_etags: OrderedDict[tuple, str] = OrderedDict()
_etags_lock = threading.Lock()
_MAX_ETAGS = 4096


def clear_caches() -> None:
    _compressed.clear()
    with _etags_lock:
        _etags.clear()


def _content_etag(key: tuple, file_path: Path) -> tuple[str, bytes | None]:
    """Content hash for the file version ``key``; the bytes if it read them."""
    with _etags_lock:
        tag = _etags.get(key)
        if tag is not None:
            _etags.move_to_end(key)
            return tag, None
    raw = file_path.read_bytes()
    tag = hashlib.sha256(raw).hexdigest()[:32]
    with _etags_lock:
        _etags[key] = tag
        while len(_etags) > _MAX_ETAGS:
            _etags.popitem(last=False)
    return tag, raw


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Weak comparison, as If-None-Match requires (RFC 9110 13.1.2)."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _fresh_sibling(file_path: Path, suffix: str, mtime_ns: int) -> Path | None:
    sibling = file_path.with_name(file_path.name + suffix)
    try:
        st = sibling.stat()
    except OSError:
        return None
    if not sibling.is_file() or st.st_mtime_ns < mtime_ns:
        return None
    return sibling


def file_response(
    file_path: Path,
    *,
    cache_control: str,
    accept_encoding: str | None,
    if_none_match: str | None,
) -> Response:
    """Response for one static file, with caching headers and compression."""
    st = file_path.stat()
    key = (str(file_path), st.st_mtime_ns, st.st_size)
    content_tag, raw = _content_etag(key, file_path)

    encoding: str | None = None
    sibling: Path | None = None
    compressible = file_path.suffix.lower() in _COMPRESSIBLE_SUFFIXES
    if compressible:
        accepted = accepted_encodings(accept_encoding)
        for name, suffix in _PRECOMPRESSED:
            if name in accepted:
                sibling = _fresh_sibling(file_path, suffix, st.st_mtime_ns)
                if sibling is not None:
                    encoding = name
                    break
        if encoding is None and "gzip" in accepted and st.st_size >= _MIN_COMPRESS_SIZE:
            encoding = "gzip"

    etag = f'"{content_tag}-{encoding}"' if encoding else f'"{content_tag}"'
    headers = {"Cache-Control": cache_control, "ETag": etag}
    if compressible:
        headers["Vary"] = "Accept-Encoding"

    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)

    if sibling is not None:
        body = sibling.read_bytes()
    elif encoding == "gzip":
        cache_key = key + ("gzip",)
        body = _compressed.get(cache_key)
        if body is None:
            if raw is None:
                raw = file_path.read_bytes()
            # mtime=0 keeps the output identical for identical input.
            body = gzip.compress(raw, compresslevel=9, mtime=0)
            _compressed.put(cache_key, body)
    else:
        body = raw if raw is not None else file_path.read_bytes()
    if encoding:
        headers["Content-Encoding"] = encoding
    return Response(content=body, media_type=content_type_for(file_path), headers=headers)
