"""Graph selectors: grammar, tags, and the callers that share them."""

from __future__ import annotations

from pathlib import Path

import pytest

from havn.engine.transform.analysis import validate_models
from havn.engine.transform.discovery import discover_models


# ---------------------------------------------------------------------------
# @config tags=
# ---------------------------------------------------------------------------


def _write(transform_dir: Path, rel: str, sql: str) -> Path:
    path = transform_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql)
    return path


def test_tags_parsed_as_list(tmp_path):
    transform = tmp_path / "transform"
    _write(
        transform,
        "gold/orders.sql",
        "@config materialized=table, tags=daily,finance\nSELECT 1 AS x\n",
    )
    model = discover_models(transform)[0]
    assert model.tags == ["daily", "finance"]


def test_tags_default_to_empty(tmp_path):
    transform = tmp_path / "transform"
    _write(transform, "gold/orders.sql", "@config materialized=table\nSELECT 1 AS x\n")
    assert discover_models(transform)[0].tags == []


def test_tags_tolerate_spaces_after_commas(tmp_path):
    transform = tmp_path / "transform"
    _write(
        transform,
        "gold/orders.sql",
        "@config tags=daily, finance, nightly\nSELECT 1 AS x\n",
    )
    assert discover_models(transform)[0].tags == ["daily", "finance", "nightly"]


def test_retagging_does_not_change_content_hash(tmp_path):
    """Tags are metadata, not build semantics: retagging must not rebuild.

    Everything else that `@config` sets is folded into `content_hash` so a
    change rebuilds the model. Tags deliberately are not -- adding
    `tags=finance` to a hundred models should not rebuild a hundred tables.
    """
    transform = tmp_path / "transform"
    path = _write(
        transform, "gold/orders.sql", "@config materialized=table\nSELECT 1 AS x\n"
    )
    before = discover_models(transform)[0].content_hash

    path.write_text("@config materialized=table, tags=finance\nSELECT 1 AS x\n")
    after_model = discover_models(transform)[0]
    assert after_model.tags == ["finance"]
    assert after_model.content_hash == before

    # But a real config change still does move the hash.
    path.write_text("@config materialized=view, tags=finance\nSELECT 1 AS x\n")
    assert discover_models(transform)[0].content_hash != before


def test_tags_is_a_known_config_key(tmp_path):
    """`tags` must not be reported as an unknown @config key."""
    transform = tmp_path / "transform"
    _write(transform, "gold/orders.sql", "@config tags=daily\nSELECT 1 AS x\n")
    models = discover_models(transform)
    errors = validate_models(None, models)
    assert not [e for e in errors if "Unknown @config key" in e.message]


@pytest.mark.parametrize("bad", ["2fast", "has space", "with.dot", "with:colon"])
def test_validate_models_rejects_non_identifier_tags(tmp_path, bad):
    transform = tmp_path / "transform"
    _write(transform, "gold/orders.sql", f"@config tags={bad}\nSELECT 1 AS x\n")
    models = discover_models(transform)
    errors = validate_models(None, models)
    assert [e for e in errors if "Invalid tag" in e.message], (
        f"expected {bad!r} to be rejected"
    )


def test_validate_models_accepts_identifier_tags(tmp_path):
    transform = tmp_path / "transform"
    _write(
        transform,
        "gold/orders.sql",
        "@config tags=daily,finance_v2,end-of-day\nSELECT 1 AS x\n",
    )
    models = discover_models(transform)
    errors = validate_models(None, models)
    assert not [e for e in errors if "Invalid tag" in e.message]
