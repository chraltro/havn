"""Tests for anomaly detection via statistical profiling."""

from __future__ import annotations

import json

import duckdb
import pytest

from havn.engine.anomaly import (
    AnomalyResult,
    _check_metric,
    _mean,
    _stddev,
    detect_all_anomalies,
    detect_anomalies,
    log_anomalies,
)
from havn.engine.database import ensure_meta_table
from havn.engine.transform.models import ProfileResult


@pytest.fixture
def conn(tmp_path):
    """Create a temporary DuckDB connection with metadata tables."""
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    ensure_meta_table(c)
    yield c
    c.close()


def _insert_profile_history(conn, model, row_count, null_pcts=None, distinct_cts=None):
    """Helper to insert a profile history row."""
    conn.execute(
        """
        INSERT INTO _dp_internal.profile_history
            (model_path, row_count, column_count, null_percentages, distinct_counts, profiled_at)
        VALUES (?, ?, ?, ?::JSON, ?::JSON, current_timestamp)
        """,
        [
            model,
            row_count,
            2,
            json.dumps(null_pcts or {}),
            json.dumps(distinct_cts or {}),
        ],
    )


class TestZScoreCalculation:
    """Test basic statistical functions."""

    def test_mean(self):
        assert _mean([10, 20, 30]) == 20.0
        assert _mean([100]) == 100.0

    def test_stddev(self):
        values = [10, 10, 10]
        assert _stddev(values, 10.0) == 0.0

        values = [0, 10]
        m = _mean(values)
        sd = _stddev(values, m)
        assert sd == 5.0

    def test_check_metric_normal(self):
        """No anomaly when value is within normal range."""
        history = [100.0, 102.0, 98.0, 101.0, 99.0]
        result = _check_metric("test.model", "row_count", 100.0, history, threshold=2.0)
        assert result is not None
        assert not result.is_anomaly

    def test_check_metric_spike(self):
        """Anomaly detected when value spikes."""
        history = [100.0, 102.0, 98.0, 101.0, 99.0]
        result = _check_metric("test.model", "row_count", 200.0, history, threshold=2.0)
        assert result is not None
        assert result.is_anomaly
        assert result.direction == "increase"

    def test_check_metric_drop(self):
        """Anomaly detected when value drops."""
        history = [1000.0, 1020.0, 980.0, 1010.0, 990.0]
        result = _check_metric("test.model", "row_count", 10.0, history, threshold=2.0)
        assert result is not None
        assert result.is_anomaly
        assert result.direction == "decrease"

    def test_check_metric_too_few(self):
        """Skip when fewer than 3 data points."""
        result = _check_metric("test.model", "row_count", 100.0, [100.0, 101.0], threshold=2.0)
        assert result is None

    def test_check_metric_zero_stddev(self):
        """Skip when standard deviation is zero (constant values)."""
        history = [100.0, 100.0, 100.0, 100.0]
        result = _check_metric("test.model", "row_count", 100.0, history, threshold=2.0)
        assert result is None


class TestDetectAnomalies:
    """Test anomaly detection with database history."""

    def test_no_history(self, conn):
        """No anomalies when there's no history."""
        profile = ProfileResult(row_count=100, column_count=2)
        results = detect_anomalies(conn, "test.model", profile)
        assert results == []

    def test_too_few_profiles(self, conn):
        """No anomalies with fewer than 3 historical profiles."""
        _insert_profile_history(conn, "test.model", 100)
        _insert_profile_history(conn, "test.model", 102)
        profile = ProfileResult(row_count=100, column_count=2)
        results = detect_anomalies(conn, "test.model", profile)
        assert results == []

    def test_normal_values_no_anomaly(self, conn):
        """No anomalies when current matches historical pattern."""
        for rc in [100, 102, 98, 101, 99]:
            _insert_profile_history(conn, "silver.orders", rc, {"id": 0.0, "name": 5.0}, {"id": rc, "name": 50})
        profile = ProfileResult(
            row_count=100,
            column_count=2,
            null_percentages={"id": 0.0, "name": 5.0},
            distinct_counts={"id": 100, "name": 50},
        )
        results = detect_anomalies(conn, "silver.orders", profile, threshold=2.0)
        # All results should exist but none should be anomalies
        for r in results:
            assert not r.is_anomaly

    def test_row_count_spike(self, conn):
        """Detect anomaly when row count doubles."""
        for rc in [1000, 1020, 980, 1010, 990]:
            _insert_profile_history(conn, "bronze.events", rc)
        profile = ProfileResult(row_count=2000, column_count=2)
        results = detect_anomalies(conn, "bronze.events", profile, threshold=2.0)
        anomalies = [r for r in results if r.is_anomaly]
        assert len(anomalies) >= 1
        rc_anomaly = next(r for r in anomalies if r.metric == "row_count")
        assert rc_anomaly.direction == "increase"
        assert rc_anomaly.z_score > 2.0

    def test_row_count_drop(self, conn):
        """Detect anomaly when row count drops 90%."""
        for rc in [1000, 1020, 980, 1010, 990]:
            _insert_profile_history(conn, "gold.summary", rc)
        profile = ProfileResult(row_count=100, column_count=2)
        results = detect_anomalies(conn, "gold.summary", profile, threshold=2.0)
        anomalies = [r for r in results if r.is_anomaly]
        assert len(anomalies) >= 1
        rc_anomaly = next(r for r in anomalies if r.metric == "row_count")
        assert rc_anomaly.direction == "decrease"
        assert rc_anomaly.z_score < -2.0

    def test_custom_threshold(self, conn):
        """Higher threshold means fewer anomalies."""
        for rc in [100, 102, 98, 101, 99]:
            _insert_profile_history(conn, "test.model", rc)
        profile = ProfileResult(row_count=110, column_count=2)
        # With low threshold: might flag
        results_low = detect_anomalies(conn, "test.model", profile, threshold=1.0)
        # With very high threshold: should not flag
        results_high = detect_anomalies(conn, "test.model", profile, threshold=100.0)
        low_anomalies = [r for r in results_low if r.is_anomaly]
        high_anomalies = [r for r in results_high if r.is_anomaly]
        assert len(high_anomalies) <= len(low_anomalies)


class TestDetectAllAnomalies:
    """Test batch anomaly detection across multiple models."""

    def test_multiple_models(self, conn):
        """Detect anomalies across multiple models."""
        # Normal model
        for rc in [100, 102, 98, 101, 99]:
            _insert_profile_history(conn, "silver.normal", rc)
        # Anomalous model
        for rc in [1000, 1020, 980, 1010, 990]:
            _insert_profile_history(conn, "silver.spiked", rc)

        profiles = {
            "silver.normal": ProfileResult(row_count=100, column_count=2),
            "silver.spiked": ProfileResult(row_count=5000, column_count=2),
        }
        anomalies = detect_all_anomalies(conn, profiles, threshold=2.0)
        # Only the spiked model should have anomalies
        models_with_anomalies = {a.model for a in anomalies}
        assert "silver.spiked" in models_with_anomalies


class TestAnomalyLogging:
    """Test logging anomalies to the database."""

    def test_log_anomalies(self, conn):
        """Anomalies are saved to anomaly_log table."""
        anomalies = [
            AnomalyResult(
                model="bronze.events",
                metric="row_count",
                current_value=2000.0,
                mean=1000.0,
                stddev=15.0,
                z_score=66.67,
                is_anomaly=True,
                direction="increase",
                message="row_count up 100% (2K vs mean 1K)",
            ),
        ]
        log_anomalies(conn, anomalies)
        rows = conn.execute(
            "SELECT model_name, metric, z_score, direction FROM _dp_internal.anomaly_log"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "bronze.events"
        assert rows[0][1] == "row_count"
        assert rows[0][3] == "increase"

    def test_log_multiple_anomalies(self, conn):
        """Multiple anomalies are all logged."""
        anomalies = [
            AnomalyResult(
                model="silver.orders",
                metric="row_count",
                current_value=100.0,
                mean=1000.0,
                stddev=15.0,
                z_score=-60.0,
                is_anomaly=True,
                direction="decrease",
                message="row_count down 90%",
            ),
            AnomalyResult(
                model="silver.orders",
                metric="null_percentage",
                current_value=50.0,
                mean=5.0,
                stddev=2.0,
                z_score=22.5,
                is_anomaly=True,
                direction="increase",
                message="null_percentage up 900%",
            ),
        ]
        log_anomalies(conn, anomalies)
        rows = conn.execute(
            "SELECT COUNT(*) FROM _dp_internal.anomaly_log"
        ).fetchone()
        assert rows[0] == 2


class TestAlertIntegration:
    """Test that anomalies trigger alerts."""

    def test_alert_anomalies_no_config(self, conn):
        """Alert function is safe when no channels configured."""
        from havn.engine.anomaly import alert_anomalies

        anomalies = [
            AnomalyResult(
                model="test.model",
                metric="row_count",
                current_value=100.0,
                mean=1000.0,
                stddev=15.0,
                z_score=-60.0,
                is_anomaly=True,
                direction="decrease",
                message="row_count dropped",
            ),
        ]

        class FakeConfig:
            slack_webhook_url = None
            webhook_url = None
            channels = []

        # Should not raise
        alert_anomalies(anomalies, FakeConfig(), conn)

    def test_alert_anomalies_empty(self, conn):
        """No alerts sent when no anomalies."""
        from havn.engine.anomaly import alert_anomalies

        class FakeConfig:
            channels = ["log"]

        alert_anomalies([], FakeConfig(), conn)
