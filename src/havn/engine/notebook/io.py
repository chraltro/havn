"""Notebook I/O: create, load, save, and cell ID generation."""

from __future__ import annotations

from havn.textio import read_project_text

import json
from pathlib import Path


def create_notebook(title: str = "Untitled") -> dict:
    """Create a blank notebook structure."""
    return {
        "title": title,
        "cells": [
            {
                "id": "cell_1",
                "type": "markdown",
                "source": f"# {title}\n\nUse this notebook to explore your data.",
            },
            {
                "id": "cell_2",
                "type": "sql",
                "source": "SELECT 1 AS hello",
                "outputs": [],
            },
        ],
    }


def load_notebook(path: Path) -> dict:
    """Load a notebook from a .dpnb file."""
    if not path.exists():
        raise FileNotFoundError(f"Notebook not found: {path}")
    return json.loads(read_project_text(path))


def save_notebook(path: Path, notebook: dict) -> None:
    """Save a notebook to a .dpnb file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(notebook, indent=2) + "\n", encoding="utf-8")


def _make_cell_id() -> str:
    """Generate a unique cell ID."""
    import secrets
    return f"cell_{secrets.token_hex(6)}"
