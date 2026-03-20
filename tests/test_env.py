"""Tests for havn env CLI command and config integration."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from havn.cli import app
from havn.config import load_project

runner = CliRunner()

PROJECT_YML_WITH_ENVS = """\
name: test-project
database:
  path: warehouse.duckdb
environments:
  dev:
    database:
      path: dev.duckdb
  prod:
    database:
      path: prod.duckdb
    connections:
      pg:
        host: prod-db.example.com
  staging:
    database:
      path: staging.duckdb
"""

PROJECT_YML_NO_ENVS = """\
name: test-project
database:
  path: warehouse.duckdb
"""


@pytest.fixture()
def project_with_envs(tmp_path: Path, monkeypatch):
    (tmp_path / "project.yml").write_text(PROJECT_YML_WITH_ENVS)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def project_no_envs(tmp_path: Path, monkeypatch):
    (tmp_path / "project.yml").write_text(PROJECT_YML_NO_ENVS)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- env list ---

def test_env_list_shows_environments(project_with_envs):
    result = runner.invoke(app, ["env", "list"])
    assert result.exit_code == 0
    assert "dev" in result.output
    assert "prod" in result.output
    assert "staging" in result.output


def test_env_list_marks_active(project_with_envs):
    (project_with_envs / ".havn-env").write_text("prod\n")
    result = runner.invoke(app, ["env", "list"])
    assert result.exit_code == 0
    assert "prod" in result.output
    # The active env should be indicated
    assert "Active environment: prod" in result.output


def test_env_list_no_environments(project_no_envs):
    result = runner.invoke(app, ["env", "list"])
    assert result.exit_code == 0
    assert "No environments defined" in result.output


# --- env use ---

def test_env_use_creates_file(project_with_envs):
    result = runner.invoke(app, ["env", "use", "prod"])
    assert result.exit_code == 0
    assert "prod" in result.output

    env_file = project_with_envs / ".havn-env"
    assert env_file.exists()
    assert env_file.read_text().strip() == "prod"


def test_env_use_invalid_name(project_with_envs):
    result = runner.invoke(app, ["env", "use", "nonexistent"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_env_use_no_name(project_with_envs):
    result = runner.invoke(app, ["env", "use"])
    assert result.exit_code == 1
    assert "specify" in result.output.lower() or "name" in result.output.lower()


# --- env show ---

def test_env_show_with_active(project_with_envs):
    (project_with_envs / ".havn-env").write_text("staging\n")
    result = runner.invoke(app, ["env", "show"])
    assert result.exit_code == 0
    assert "staging" in result.output


def test_env_show_default(project_with_envs):
    result = runner.invoke(app, ["env", "show"])
    assert result.exit_code == 0
    assert "default" in result.output


# --- env reset ---

def test_env_reset_deletes_file(project_with_envs):
    env_file = project_with_envs / ".havn-env"
    env_file.write_text("prod\n")
    assert env_file.exists()

    result = runner.invoke(app, ["env", "reset"])
    assert result.exit_code == 0
    assert not env_file.exists()
    assert "Cleared" in result.output


def test_env_reset_no_file(project_with_envs):
    result = runner.invoke(app, ["env", "reset"])
    assert result.exit_code == 0
    assert "No active environment to clear" in result.output


# --- config loading respects .havn-env ---

def test_config_loads_env_from_havn_env_file(tmp_path: Path):
    (tmp_path / "project.yml").write_text(PROJECT_YML_WITH_ENVS)
    (tmp_path / ".havn-env").write_text("prod\n")

    config = load_project(tmp_path)
    assert config.active_environment == "prod"
    assert config.database.path == "prod.duckdb"


def test_config_explicit_env_overrides_havn_env_file(tmp_path: Path):
    (tmp_path / "project.yml").write_text(PROJECT_YML_WITH_ENVS)
    (tmp_path / ".havn-env").write_text("prod\n")

    config = load_project(tmp_path, env="dev")
    assert config.active_environment == "dev"
    assert config.database.path == "dev.duckdb"


def test_config_no_havn_env_defaults_to_dev(tmp_path: Path):
    """When no .havn-env and no --env, falls back to 'dev' if it exists."""
    (tmp_path / "project.yml").write_text(PROJECT_YML_WITH_ENVS)

    config = load_project(tmp_path)
    assert config.active_environment == "dev"
    assert config.database.path == "dev.duckdb"
