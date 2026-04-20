"""Tests for `havn init --from <url>`."""
from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import typer

from havn.cli.project import (
    _extract_archive,
    _init_from_remote,
    _resolve_template_url,
)


# ---------- URL resolution ----------


def test_resolve_http_url_passthrough():
    assert _resolve_template_url("https://example.com/t.tar.gz") == "https://example.com/t.tar.gz"
    assert _resolve_template_url("http://example.com/t.zip") == "http://example.com/t.zip"


def test_resolve_github_shorthand_main():
    url = _resolve_template_url("foo/bar")
    assert url == "https://github.com/foo/bar/archive/refs/heads/main.tar.gz"


def test_resolve_github_shorthand_branch():
    url = _resolve_template_url("foo/bar@dev")
    assert url == "https://github.com/foo/bar/archive/refs/heads/dev.tar.gz"


def test_resolve_garbage_raises():
    with pytest.raises(typer.BadParameter):
        _resolve_template_url("not a ref at all")


# ---------- Archive extraction ----------


def _make_tarball(tmp_path: Path, top_dir: str | None = "pkg") -> Path:
    """Build a tarball with an optional single top-level directory (GitHub style)."""
    archive = tmp_path / "src.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        def _add(arcname: str, data: bytes):
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        prefix = f"{top_dir}/" if top_dir else ""
        _add(f"{prefix}project.yml", b"name: demo\n")
        _add(f"{prefix}ingest/run.py", b"print('hi')\n")
        _add(f"{prefix}README.md", b"# demo\n")
    return archive


def _make_zip(tmp_path: Path, top_dir: str | None = "pkg") -> Path:
    archive = tmp_path / "src.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        prefix = f"{top_dir}/" if top_dir else ""
        zf.writestr(f"{prefix}project.yml", "name: demo\n")
        zf.writestr(f"{prefix}ingest/run.py", "print('hi')\n")
        zf.writestr(f"{prefix}README.md", "# demo\n")
    return archive


def test_extract_tarball_with_single_top_dir(tmp_path):
    archive = _make_tarball(tmp_path, top_dir="pkg-main")
    staging = tmp_path / "stage"
    staging.mkdir()
    project_root = _extract_archive(archive, staging)
    assert project_root.name == "pkg-main"
    assert (project_root / "project.yml").exists()
    assert (project_root / "ingest" / "run.py").exists()


def test_extract_zip_with_single_top_dir(tmp_path):
    archive = _make_zip(tmp_path, top_dir="pkg-main")
    staging = tmp_path / "stage"
    staging.mkdir()
    project_root = _extract_archive(archive, staging)
    assert project_root.name == "pkg-main"
    assert (project_root / "project.yml").exists()


def test_extract_tarball_flat(tmp_path):
    """Archive without a single top-level dir should return staging itself."""
    archive = tmp_path / "flat.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, data in [("project.yml", b"x"), ("README.md", b"y")]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    staging = tmp_path / "stage"
    staging.mkdir()
    project_root = _extract_archive(archive, staging)
    assert project_root == staging


def test_extract_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo(name="../evil.txt")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"boom"))
    staging = tmp_path / "stage"
    staging.mkdir()
    with pytest.raises(typer.Exit):
        _extract_archive(archive, staging)


def test_extract_rejects_absolute_paths(tmp_path):
    archive = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/evil", "boom")
    staging = tmp_path / "stage"
    staging.mkdir()
    with pytest.raises(typer.Exit):
        _extract_archive(archive, staging)


def test_extract_rejects_unknown_suffix(tmp_path):
    archive = tmp_path / "something.rar"
    archive.write_bytes(b"not a real archive")
    staging = tmp_path / "stage"
    staging.mkdir()
    with pytest.raises(typer.Exit):
        _extract_archive(archive, staging)


# ---------- End-to-end init_from_remote ----------


def test_init_from_remote_copies_template(tmp_path, monkeypatch):
    """_init_from_remote should download + extract + copy template into target."""
    archive = _make_tarball(tmp_path, top_dir="pkg-main")

    def fake_urlopen(req, timeout=60):  # noqa: ARG001
        class _Resp:
            def __enter__(self_):
                return self_
            def __exit__(self_, *a):
                return False
            def read(self_, n=-1):
                return archive.read_bytes()
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    target = tmp_path / "myproj"
    _init_from_remote(name="myproj", directory=target, url="https://example.com/pkg.tar.gz")

    assert (target / "project.yml").read_text() == "name: demo\n"
    assert (target / "ingest" / "run.py").read_text() == "print('hi')\n"
    assert (target / "README.md").read_text() == "# demo\n"


def test_init_from_remote_refuses_nonempty_target(tmp_path):
    target = tmp_path / "myproj"
    target.mkdir()
    (target / "existing.txt").write_text("leave me alone")
    with pytest.raises(typer.Exit):
        _init_from_remote(name="myproj", directory=target, url="foo/bar")
    assert (target / "existing.txt").read_text() == "leave me alone"
