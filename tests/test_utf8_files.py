"""Project files are UTF-8 on every platform, not the OS default encoding.

On Windows the default is cp1252, which turned 'Tromsø' in an ingest script
or a model into 'TromsÃ¸' in the warehouse.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

from havn.engine.database import ensure_meta_table
from havn.engine.runner import run_script
from havn.engine.transform import run_transform

SRC = Path(__file__).resolve().parents[1] / "src" / "havn"


def test_no_text_read_or_write_relies_on_the_default_encoding():
    offenders = []
    for py in SRC.rglob("*.py"):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.read_text\(\)", line) or re.search(r"\.write_text\([^)]*\)\s*$", line) and "encoding" not in line:
                offenders.append(f"{py.relative_to(SRC)}:{n}: {line.strip()}")
    assert offenders == []


def test_nordic_letters_survive_a_script_and_a_model(tmp_path):
    (tmp_path / "ingest").mkdir()
    (tmp_path / "ingest" / "ports.py").write_text(
        'db.execute("CREATE SCHEMA IF NOT EXISTS landing")\n'
        "db.execute(\"CREATE OR REPLACE TABLE landing.ports AS SELECT 'Tromsø' AS port\")\n",
        encoding="utf-8",
    )
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "ports.sql").write_text(
        "@config materialized=table\n\nSELECT port, 'Ålesund' AS twin FROM landing.ports\n",
        encoding="utf-8",
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    try:
        ensure_meta_table(conn)
        assert run_script(conn, tmp_path / "ingest" / "ports.py", "ingest")["status"] == "success"
        run_transform(conn, tmp_path / "transform", db_path=str(tmp_path / "warehouse.duckdb"))
        assert conn.execute("SELECT port, twin FROM silver.ports").fetchone() == ("Tromsø", "Ålesund")
    finally:
        conn.close()


def test_a_bom_or_a_legacy_encoded_file_still_reads(tmp_path):
    from havn.textio import read_project_text

    bom = tmp_path / "bom.sql"
    bom.write_bytes("﻿@config materialized=table\nSELECT 'Tromsø'".encode("utf-8"))
    assert read_project_text(bom).startswith("@config")

    legacy = tmp_path / "legacy.py"
    legacy.write_bytes("x = 'caf\xe9'".encode("cp1252"))  # not valid UTF-8
    assert read_project_text(legacy).startswith("x = 'caf")
