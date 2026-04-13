"""Tests for project configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from havn.config import DatabaseConfig, load_project


def test_load_missing_config(tmp_path):
    """Loading from a dir without project.yml returns defaults."""
    config = load_project(tmp_path)
    assert config.database.path == "warehouse.duckdb"
    assert config.streams == {}


def test_load_config(tmp_path):
    (tmp_path / "project.yml").write_text(
        """
name: test-project
description: "A test project"

database:
  path: my.duckdb

connections:
  pg:
    type: postgres
    host: localhost
    port: 5432

streams:
  pipeline:
    description: "Test pipeline"
    steps:
      - ingest: [customers]
      - transform: [all]
      - export: [to_csv]
    schedule: "0 6 * * *"

lint:
  dialect: duckdb
"""
    )

    config = load_project(tmp_path)
    assert config.name == "test-project"
    assert config.database.path == "my.duckdb"
    assert "pg" in config.connections
    assert config.connections["pg"].type == "postgres"
    assert "pipeline" in config.streams
    assert len(config.streams["pipeline"].steps) == 3
    assert config.streams["pipeline"].schedule == "0 6 * * *"
    assert config.lint.dialect == "duckdb"


def test_stream_steps_parsing(tmp_path):
    (tmp_path / "project.yml").write_text(
        """
name: test
streams:
  s1:
    steps:
      - ingest: [a, b]
      - transform: all
      - export: [x]
"""
    )
    config = load_project(tmp_path)
    steps = config.streams["s1"].steps
    assert steps[0].action == "ingest"
    assert steps[0].targets == ["a", "b"]
    assert steps[1].action == "transform"
    assert steps[1].targets == ["all"]
    assert steps[2].action == "export"
    assert steps[2].targets == ["x"]


# --- DatabaseConfig backend abstraction (SPEC 01) ---------------------------


def test_database_config_default_is_duckdb():
    cfg = DatabaseConfig()
    assert cfg.backend == "duckdb"
    assert cfg.path == "warehouse.duckdb"
    assert cfg.catalog is None
    assert cfg.data_path is None
    assert cfg.encrypted is False


def test_database_config_ducklake_requires_catalog_and_data_path():
    with pytest.raises(ValidationError):
        DatabaseConfig(backend="ducklake")
    with pytest.raises(ValidationError):
        DatabaseConfig(backend="ducklake", catalog="cat.ducklake")
    with pytest.raises(ValidationError):
        DatabaseConfig(backend="ducklake", data_path="./data/")
    # both present: OK
    cfg = DatabaseConfig(backend="ducklake", catalog="cat.ducklake", data_path="./data/")
    assert cfg.backend == "ducklake"


def test_database_config_invalid_backend():
    with pytest.raises(ValidationError):
        DatabaseConfig(backend="sqlite")


def test_load_config_memory_limit_and_threads_not_dropped(tmp_path):
    """Regression: load_project used to silently drop memory_limit/threads."""
    (tmp_path / "project.yml").write_text(
        """
name: test
database:
  path: foo.duckdb
  memory_limit: "2GB"
  threads: 4
"""
    )
    config = load_project(tmp_path)
    assert config.database.path == "foo.duckdb"
    assert config.database.memory_limit == "2GB"
    assert config.database.threads == 4


def test_load_config_ducklake_backend(tmp_path):
    (tmp_path / "project.yml").write_text(
        """
name: test
database:
  backend: ducklake
  catalog: .havn/catalog.ducklake
  data_path: .havn/data/
  encrypted: true
  threads: 8
"""
    )
    config = load_project(tmp_path)
    assert config.database.backend == "ducklake"
    assert config.database.catalog == ".havn/catalog.ducklake"
    assert config.database.data_path == ".havn/data/"
    assert config.database.encrypted is True
    assert config.database.threads == 8


def test_environment_override_merges_all_database_fields(tmp_path):
    """env override of database: should merge with base fields, not replace."""
    (tmp_path / "project.yml").write_text(
        """
name: test
database:
  path: base.duckdb
  memory_limit: "1GB"
  threads: 2
environments:
  prod:
    database:
      path: prod.duckdb
      threads: 8
"""
    )
    config = load_project(tmp_path, env="prod")
    # overridden fields
    assert config.database.path == "prod.duckdb"
    assert config.database.threads == 8
    # non-overridden field preserved from base
    assert config.database.memory_limit == "1GB"


def test_environment_override_can_switch_backend(tmp_path):
    (tmp_path / "project.yml").write_text(
        """
name: test
database:
  path: local.duckdb
environments:
  prod:
    database:
      backend: ducklake
      catalog: prod.ducklake
      data_path: ./prod-data/
"""
    )
    config = load_project(tmp_path, env="prod")
    assert config.database.backend == "ducklake"
    assert config.database.catalog == "prod.ducklake"
    assert config.database.data_path == "./prod-data/"
