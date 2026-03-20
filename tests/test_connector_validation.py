"""Tests for connector ParamSpec validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from havn.engine.connector import (
    BaseConnector,
    ParamSpec,
    validate_connector_config,
    validate_param,
)


# ---------------------------------------------------------------------------
# validate_param tests
# ---------------------------------------------------------------------------


class TestRequiredValidation:
    def test_required_missing_none(self):
        spec = ParamSpec("host", "Database host", required=True)
        assert validate_param(spec, None) == "host is required"

    def test_required_missing_empty(self):
        spec = ParamSpec("host", "Database host", required=True)
        assert validate_param(spec, "") == "host is required"

    def test_required_missing_whitespace(self):
        spec = ParamSpec("host", "Database host", required=True)
        assert validate_param(spec, "   ") == "host is required"

    def test_required_present(self):
        spec = ParamSpec("host", "Database host", required=True)
        assert validate_param(spec, "localhost") is None

    def test_optional_missing(self):
        spec = ParamSpec("host", "Database host", required=False)
        assert validate_param(spec, None) is None

    def test_optional_empty(self):
        spec = ParamSpec("host", "Database host", required=False)
        assert validate_param(spec, "") is None


class TestIntegerValidation:
    def test_valid_integer(self):
        spec = ParamSpec("port", "Port", param_type="integer", min_value=1, max_value=65535)
        assert validate_param(spec, 5432) is None

    def test_valid_integer_string(self):
        spec = ParamSpec("port", "Port", param_type="integer", min_value=1, max_value=65535)
        assert validate_param(spec, "5432") is None

    def test_integer_too_high(self):
        spec = ParamSpec("port", "Port", param_type="integer", min_value=1, max_value=65535)
        err = validate_param(spec, 99999)
        assert err is not None
        assert "99999" in err
        assert "between" in err

    def test_integer_too_low(self):
        spec = ParamSpec("port", "Port", param_type="integer", min_value=1, max_value=65535)
        err = validate_param(spec, 0)
        assert err is not None
        assert "0" in err

    def test_integer_not_numeric(self):
        spec = ParamSpec("port", "Port", param_type="integer")
        err = validate_param(spec, "abc")
        assert err is not None
        assert "must be an integer" in err

    def test_integer_min_only(self):
        spec = ParamSpec("timeout", "Timeout", param_type="integer", min_value=1)
        assert validate_param(spec, 0) is not None
        assert validate_param(spec, 1) is None
        assert validate_param(spec, 9999) is None

    def test_integer_max_only(self):
        spec = ParamSpec("limit", "Limit", param_type="integer", max_value=100)
        assert validate_param(spec, 101) is not None
        assert validate_param(spec, 100) is None
        assert validate_param(spec, -5) is None

    def test_integer_boundary_values(self):
        spec = ParamSpec("port", "Port", param_type="integer", min_value=1, max_value=65535)
        assert validate_param(spec, 1) is None
        assert validate_param(spec, 65535) is None


class TestBooleanValidation:
    def test_valid_bool_true(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        assert validate_param(spec, True) is None

    def test_valid_bool_false(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        assert validate_param(spec, False) is None

    def test_valid_bool_string_true(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        assert validate_param(spec, "true") is None

    def test_valid_bool_string_false(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        assert validate_param(spec, "false") is None

    def test_valid_bool_yes_no(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        assert validate_param(spec, "yes") is None
        assert validate_param(spec, "no") is None

    def test_valid_bool_one_zero(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        assert validate_param(spec, "1") is None
        assert validate_param(spec, "0") is None

    def test_invalid_bool(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        err = validate_param(spec, "maybe")
        assert err is not None
        assert "boolean" in err

    def test_invalid_bool_number(self):
        spec = ParamSpec("append", "Append", param_type="boolean")
        err = validate_param(spec, 42)
        assert err is not None
        assert "boolean" in err


class TestEnumValidation:
    def test_valid_enum(self):
        spec = ParamSpec("method", "HTTP method", param_type="enum", enum_values=["GET", "POST"])
        assert validate_param(spec, "GET") is None

    def test_invalid_enum(self):
        spec = ParamSpec("method", "HTTP method", param_type="enum", enum_values=["GET", "POST"])
        err = validate_param(spec, "DELETE")
        assert err is not None
        assert "GET" in err
        assert "POST" in err
        assert "DELETE" in err

    def test_enum_case_sensitive(self):
        spec = ParamSpec("sslmode", "SSL mode", param_type="enum", enum_values=["disable", "require"])
        err = validate_param(spec, "DISABLE")
        assert err is not None


class TestURLValidation:
    def test_valid_http(self):
        spec = ParamSpec("url", "URL", param_type="url")
        assert validate_param(spec, "http://example.com") is None

    def test_valid_https(self):
        spec = ParamSpec("url", "URL", param_type="url")
        assert validate_param(spec, "https://api.example.com/v1") is None

    def test_invalid_url(self):
        spec = ParamSpec("url", "URL", param_type="url")
        err = validate_param(spec, "ftp://example.com")
        assert err is not None
        assert "http://" in err

    def test_no_protocol(self):
        spec = ParamSpec("url", "URL", param_type="url")
        err = validate_param(spec, "example.com")
        assert err is not None


class TestPatternValidation:
    def test_valid_s3_path(self):
        spec = ParamSpec("path", "Bucket path", pattern=r"^(s3|gs)://")
        assert validate_param(spec, "s3://my-bucket/data/") is None

    def test_valid_gs_path(self):
        spec = ParamSpec("path", "Bucket path", pattern=r"^(s3|gs)://")
        assert validate_param(spec, "gs://my-bucket/data/") is None

    def test_invalid_path_pattern(self):
        spec = ParamSpec("path", "Bucket path", pattern=r"^(s3|gs)://")
        err = validate_param(spec, "/local/path/data")
        assert err is not None
        assert "pattern" in err

    def test_stripe_key_pattern(self):
        spec = ParamSpec("api_key", "Stripe key", pattern=r"^sk_")
        assert validate_param(spec, "sk_live_abc123") is None
        err = validate_param(spec, "pk_live_abc123")
        assert err is not None

    def test_pattern_on_optional_empty(self):
        """Pattern should not be checked on empty optional params."""
        spec = ParamSpec("path", "Path", required=False, pattern=r"^s3://")
        assert validate_param(spec, "") is None
        assert validate_param(spec, None) is None


class TestPathValidation:
    def test_valid_path(self):
        spec = ParamSpec("path", "File path", param_type="path")
        assert validate_param(spec, "/data/file.csv") is None

    def test_path_required_empty(self):
        spec = ParamSpec("path", "File path", param_type="path", required=True)
        assert validate_param(spec, "") is not None


# ---------------------------------------------------------------------------
# validate_connector_config tests
# ---------------------------------------------------------------------------


class FakeConnector(BaseConnector):
    name = "fake"
    display_name = "Fake"
    description = "Test connector"
    params = [
        ParamSpec("host", "Database host", required=True),
        ParamSpec("port", "Port", required=False, default=5432, param_type="integer", min_value=1, max_value=65535),
        ParamSpec("sslmode", "SSL mode", required=False, param_type="enum", enum_values=["disable", "require"]),
        ParamSpec("url", "URL", required=False, param_type="url"),
    ]


class TestValidateConnectorConfig:
    def test_valid_config(self):
        connector = FakeConnector()
        errors = validate_connector_config(connector, {"host": "localhost", "port": 5432})
        assert errors == []

    def test_missing_required(self):
        connector = FakeConnector()
        errors = validate_connector_config(connector, {"port": 5432})
        assert len(errors) == 1
        assert "host" in errors[0]

    def test_multiple_errors(self):
        """Bulk validation returns all errors, not just the first."""
        connector = FakeConnector()
        errors = validate_connector_config(connector, {
            "port": 99999,
            "sslmode": "invalid",
            "url": "not-a-url",
        })
        # Missing host + port too high + invalid enum + invalid URL = 4 errors
        assert len(errors) == 4

    def test_env_var_references_skipped(self):
        """Env var references like ${VAR} should skip validation."""
        connector = FakeConnector()
        errors = validate_connector_config(connector, {
            "host": "${DB_HOST}",
            "port": "${DB_PORT}",
        })
        assert errors == []

    def test_default_values_used(self):
        """Default values should be validated when no explicit value given."""
        connector = FakeConnector()
        errors = validate_connector_config(connector, {"host": "localhost"})
        # port defaults to 5432, which is valid
        assert errors == []

    def test_returns_all_errors_not_just_first(self):
        """Ensure all errors are collected."""
        params = [
            ParamSpec("a", "A", required=True),
            ParamSpec("b", "B", required=True),
            ParamSpec("c", "C", required=True),
        ]
        connector = FakeConnector()
        connector.params = params
        errors = validate_connector_config(connector, {})
        assert len(errors) == 3


# ---------------------------------------------------------------------------
# Integration: setup_connector rejects invalid config
# ---------------------------------------------------------------------------


class TestSetupConnectorValidation:
    def test_setup_rejects_invalid_port(self, tmp_path: Path):
        """setup_connector should reject config with invalid port before connecting."""
        import havn.connectors  # noqa: F401 — register all connectors
        from havn.engine.connector import setup_connector

        result = setup_connector(
            project_dir=tmp_path,
            connector_type="postgres",
            connection_name="test_pg",
            config={
                "host": "localhost",
                "port": 99999,
                "database": "testdb",
                "user": "postgres",
                "password": "secret",
            },
        )
        assert result["status"] == "error"
        assert "validation" in result["error"].lower() or "99999" in result.get("error", "")
        assert "validation_errors" in result

    def test_setup_rejects_invalid_enum(self, tmp_path: Path):
        """setup_connector should reject config with invalid enum value."""
        import havn.connectors  # noqa: F401
        from havn.engine.connector import setup_connector

        result = setup_connector(
            project_dir=tmp_path,
            connector_type="postgres",
            connection_name="test_pg",
            config={
                "host": "localhost",
                "database": "testdb",
                "user": "postgres",
                "password": "secret",
                "sslmode": "invalid_mode",
            },
        )
        assert result["status"] == "error"
        assert "validation_errors" in result

    def test_setup_passes_valid_config_through(self, tmp_path: Path):
        """Valid config should not be blocked by validation (will fail at connection test)."""
        import havn.connectors  # noqa: F401
        from havn.engine.connector import setup_connector

        result = setup_connector(
            project_dir=tmp_path,
            connector_type="csv",
            connection_name="test_csv",
            config={"path": "/nonexistent/file.csv"},
        )
        # CSV connector will fail at test_connection (file not found),
        # but should NOT fail at validation
        assert result["status"] == "error"
        assert "validation_errors" not in result  # validation passed


# ---------------------------------------------------------------------------
# Integration: test_connector validates before testing
# ---------------------------------------------------------------------------


class TestTestConnectorValidation:
    def test_test_rejects_invalid_s3_path(self):
        """test_connector should reject config with invalid S3 path pattern."""
        import havn.connectors  # noqa: F401
        from havn.engine.connector import test_connector

        result = test_connector("s3_gcs", {"path": "/local/path/data.csv"})
        assert result.get("success") is False
        assert "validation_errors" in result

    def test_test_rejects_invalid_url(self):
        """test_connector should reject config with invalid URL."""
        import havn.connectors  # noqa: F401
        from havn.engine.connector import test_connector

        result = test_connector("rest_api", {"url": "not-a-url"})
        assert result.get("success") is False
        assert "validation_errors" in result


# ---------------------------------------------------------------------------
# Real connector ParamSpec tests
# ---------------------------------------------------------------------------


class TestRealConnectorParams:
    """Test that actual connectors have sensible validation metadata."""

    def test_postgres_port_validation(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        pg = get_connector("postgres")
        port_spec = next(p for p in pg.params if p.name == "port")
        assert port_spec.param_type == "integer"
        assert port_spec.min_value == 1
        assert port_spec.max_value == 65535

    def test_postgres_sslmode_enum(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        pg = get_connector("postgres")
        ssl_spec = next(p for p in pg.params if p.name == "sslmode")
        assert ssl_spec.param_type == "enum"
        assert "disable" in ssl_spec.enum_values
        assert "require" in ssl_spec.enum_values

    def test_rest_api_url_type(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        rest = get_connector("rest_api")
        url_spec = next(p for p in rest.params if p.name == "url")
        assert url_spec.param_type == "url"

    def test_rest_api_method_enum(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        rest = get_connector("rest_api")
        method_spec = next(p for p in rest.params if p.name == "method")
        assert method_spec.param_type == "enum"
        assert method_spec.enum_values == ["GET", "POST"]

    def test_s3_path_pattern(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        s3 = get_connector("s3_gcs")
        path_spec = next(p for p in s3.params if p.name == "path")
        assert path_spec.pattern is not None
        assert "s3" in path_spec.pattern

    def test_csv_path_type(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        csv = get_connector("csv")
        path_spec = next(p for p in csv.params if p.name == "path")
        assert path_spec.param_type == "path"

    def test_webhook_boolean_type(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        wh = get_connector("webhook")
        append_spec = next(p for p in wh.params if p.name == "append")
        assert append_spec.param_type == "boolean"

    def test_stripe_key_pattern(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        stripe = get_connector("stripe")
        key_spec = next(p for p in stripe.params if p.name == "api_key")
        assert key_spec.pattern is not None
        assert "sk_" in key_spec.pattern

    def test_mysql_port_validation(self):
        import havn.connectors  # noqa: F401
        from havn.engine.connector import get_connector

        mysql = get_connector("mysql")
        port_spec = next(p for p in mysql.params if p.name == "port")
        assert port_spec.param_type == "integer"
        assert port_spec.min_value == 1
        assert port_spec.max_value == 65535
