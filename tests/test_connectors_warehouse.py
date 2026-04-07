"""Tests for warehouse migration connectors (Snowflake, BigQuery, Redshift).

These tests verify connector registration, parameter validation, ImportError
handling, and generated script validity without requiring actual cloud accounts.
"""

from __future__ import annotations

import ast

import pytest


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """Connectors are discoverable via the registry."""

    def test_snowflake_registered(self):
        from havn.engine.connector import get_connector

        c = get_connector("snowflake")
        assert c.name == "snowflake"
        assert c.display_name == "Snowflake"

    def test_bigquery_registered(self):
        from havn.engine.connector import get_connector

        c = get_connector("bigquery")
        assert c.name == "bigquery"
        assert c.display_name == "Google BigQuery"

    def test_redshift_registered(self):
        from havn.engine.connector import get_connector

        c = get_connector("redshift")
        assert c.name == "redshift"
        assert c.display_name == "Amazon Redshift"

    def test_list_connectors_includes_warehouses(self):
        from havn.engine.connector import list_connectors

        available = list_connectors()
        names = [c["name"] for c in available]
        assert "snowflake" in names
        assert "bigquery" in names
        assert "redshift" in names


# ---------------------------------------------------------------------------
# Parameter specs
# ---------------------------------------------------------------------------


class TestParamSpecs:
    """Each connector has the required params."""

    def test_snowflake_params(self):
        from havn.engine.connector import get_connector

        c = get_connector("snowflake")
        param_names = [p.name for p in c.params]
        assert "account" in param_names
        assert "user" in param_names
        assert "password" in param_names
        assert "warehouse" in param_names
        assert "database" in param_names

    def test_bigquery_params(self):
        from havn.engine.connector import get_connector

        c = get_connector("bigquery")
        param_names = [p.name for p in c.params]
        assert "project" in param_names
        assert "dataset" in param_names
        assert "credentials_json" in param_names

    def test_redshift_params(self):
        from havn.engine.connector import get_connector

        c = get_connector("redshift")
        param_names = [p.name for p in c.params]
        assert "host" in param_names
        assert "port" in param_names
        assert "database" in param_names
        assert "user" in param_names
        assert "password" in param_names

    def test_snowflake_password_is_secret(self):
        from havn.engine.connector import get_connector

        c = get_connector("snowflake")
        pw = next(p for p in c.params if p.name == "password")
        assert pw.secret is True

    def test_bigquery_credentials_is_secret(self):
        from havn.engine.connector import get_connector

        c = get_connector("bigquery")
        creds = next(p for p in c.params if p.name == "credentials_json")
        assert creds.secret is True

    def test_redshift_password_is_secret(self):
        from havn.engine.connector import get_connector

        c = get_connector("redshift")
        pw = next(p for p in c.params if p.name == "password")
        assert pw.secret is True

    def test_redshift_default_port(self):
        from havn.engine.connector import get_connector

        c = get_connector("redshift")
        port = next(p for p in c.params if p.name == "port")
        assert port.default == 5439


# ---------------------------------------------------------------------------
# ImportError handling
# ---------------------------------------------------------------------------


class TestImportErrorHandling:
    """Connectors return clear errors when SDKs are not installed."""

    def test_snowflake_test_connection_without_sdk(self):
        from havn.engine.connector import get_connector

        c = get_connector("snowflake")
        result = c.test_connection({
            "account": "test",
            "user": "test",
            "password": "test",
            "warehouse": "test",
            "database": "test",
        })
        # Either succeeds (if SDK installed) or returns ImportError message
        if not result["success"]:
            assert "snowflake" in result["error"].lower() or "install" in result["error"].lower()

    def test_bigquery_test_connection_without_sdk(self):
        from havn.engine.connector import get_connector

        c = get_connector("bigquery")
        result = c.test_connection({
            "project": "test",
            "dataset": "test",
            "credentials_json": "notbase64",
        })
        if not result["success"]:
            assert "error" in result

    def test_snowflake_missing_params(self):
        from havn.engine.connector import get_connector

        c = get_connector("snowflake")
        result = c.test_connection({})
        assert not result["success"]
        assert "required" in result["error"]

    def test_redshift_missing_host(self):
        from havn.engine.connector import get_connector

        c = get_connector("redshift")
        result = c.test_connection({})
        assert not result["success"]
        assert "required" in result["error"]


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------


class TestScriptGeneration:
    """Generated scripts are valid Python."""

    def _generate_and_parse(self, connector_name, config, tables):
        from havn.engine.connector import get_connector

        c = get_connector(connector_name)
        script = c.generate_script(config, tables, target_schema="landing")
        assert isinstance(script, str)
        assert len(script) > 100
        # Parse as Python to verify syntax
        ast.parse(script)
        return script

    def test_snowflake_full_refresh(self):
        script = self._generate_and_parse("snowflake", {
            "account": "xy12345.eu-west-1",
            "user": "${CONN_SF_USER}",
            "password": "${CONN_SF_PASSWORD}",
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
            "schema": "PUBLIC",
        }, ["orders", "customers"])
        assert "snowflake.connector" in script
        assert "fetch_arrow_all" in script
        assert "orders" in script
        assert "customers" in script
        assert "landing" in script

    def test_snowflake_incremental(self):
        script = self._generate_and_parse("snowflake", {
            "account": "xy12345.eu-west-1",
            "user": "etl",
            "password": "${CONN_SF_PASSWORD}",
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
            "schema": "PUBLIC",
            "cdc_column": "UPDATED_AT",
        }, ["orders"])
        assert "get_watermark" in script
        assert "update_watermark" in script
        assert "UPDATED_AT" in script

    def test_snowflake_with_role(self):
        script = self._generate_and_parse("snowflake", {
            "account": "xy12345",
            "user": "etl",
            "password": "${PW}",
            "warehouse": "WH",
            "database": "DB",
            "role": "ANALYST",
        }, ["t1"])
        assert "ANALYST" in script

    def test_bigquery_full_refresh(self):
        script = self._generate_and_parse("bigquery", {
            "project": "my-project-123",
            "dataset": "analytics",
            "credentials_json": "${CONN_BQ_CREDS}",
            "location": "EU",
        }, ["events", "users"])
        assert "google.cloud" in script
        assert "to_arrow" in script
        assert "events" in script
        assert "users" in script

    def test_bigquery_incremental(self):
        script = self._generate_and_parse("bigquery", {
            "project": "proj",
            "dataset": "ds",
            "credentials_json": "${CREDS}",
            "cdc_column": "updated_at",
        }, ["orders"])
        assert "get_watermark" in script
        assert "updated_at" in script

    def test_redshift_full_refresh(self):
        script = self._generate_and_parse("redshift", {
            "host": "cluster.abc.us-east-1.redshift.amazonaws.com",
            "port": 5439,
            "database": "dev",
            "user": "${CONN_RS_USER}",
            "password": "${CONN_RS_PASSWORD}",
            "schema": "public",
        }, ["orders", "products"])
        assert "postgres" in script.lower()
        assert "ATTACH" in script
        assert "orders" in script
        assert "products" in script

    def test_redshift_incremental(self):
        script = self._generate_and_parse("redshift", {
            "host": "cluster.abc.us-east-1.redshift.amazonaws.com",
            "database": "dev",
            "user": "admin",
            "password": "${PW}",
            "cdc_column": "updated_at",
        }, ["orders"])
        assert "get_watermark" in script
        assert "updated_at" in script

    def test_redshift_no_extra_dependencies(self):
        """Redshift uses DuckDB's postgres extension, no pip install needed."""
        script = self._generate_and_parse("redshift", {
            "host": "x", "database": "dev", "user": "u", "password": "${P}",
        }, ["t"])
        assert "pip install" not in script
        assert "ImportError" not in script
