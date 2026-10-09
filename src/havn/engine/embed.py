"""Embedding published dashboards in other sites (iframe).

Only published pages (``/p/<key>``) may be framed. The server sends a
``Content-Security-Policy: frame-ancestors`` built from
``sharing.embed.allowed_origins`` in ``project.yml``; every other page keeps
``X-Frame-Options: DENY``.
"""

from __future__ import annotations

import html
import re

# scheme://host[:port], no path, no wildcards except a leading "*." label.
_ORIGIN_RE = re.compile(
    r"^https?://(\*\.)?[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*(?::\d{1,5})?$"
)


def valid_origins(origins: list[str] | None) -> list[str]:
    """The configured origins that are well-formed; anything else is dropped.

    A value is spliced into a response header, so a stray ``;`` or space
    would let project.yml inject other CSP directives. Only plain origins pass.
    """
    out: list[str] = []
    for o in origins or []:
        if not isinstance(o, str):
            continue
        o = o.strip().rstrip("/")
        if _ORIGIN_RE.match(o) and o not in out:
            out.append(o)
    return out


def frame_ancestors_policy(origins: list[str] | None) -> str:
    """The CSP for a published page: same-origin plus the allowed origins."""
    allowed = valid_origins(origins)
    return "frame-ancestors " + " ".join(["'self'", *allowed])


def embed_snippet(url: str, title: str = "havn dashboard", height: int = 600) -> str:
    """An iframe snippet for a published dashboard URL."""
    sep = "&" if "?" in url else "?"
    src = html.escape(f"{url}{sep}embed=1", quote=True)
    return (
        f'<iframe src="{src}" title="{html.escape(title, quote=True)}" '
        f'width="100%" height="{int(height)}" style="border:0" loading="lazy" '
        f'referrerpolicy="no-referrer"></iframe>'
    )
