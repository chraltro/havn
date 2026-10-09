"""Reading project files (SQL, scripts, YAML, .env) the same way on every OS."""
from __future__ import annotations

import locale
from pathlib import Path


def read_project_text(path: str | Path) -> str:
    """Return a project file's text, decoded as UTF-8.

    A byte order mark (Notepad adds one) is dropped, so it never ends up in
    front of `@config`. A file that is not valid UTF-8 is decoded with the
    platform's default encoding instead, which is how havn read every file
    before 0.2.30; a cp1252 script saved on Windows keeps working.
    """
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode(locale.getpreferredencoding(False), errors="replace")
    # Universal newlines, as read_text() gives: offsets computed on the text
    # (rename, sentinel fixes) must not count a CR.
    return text.replace("\r\n", "\n").replace("\r", "\n")
