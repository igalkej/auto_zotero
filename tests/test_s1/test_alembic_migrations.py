"""Smoke tests for the Alembic migration graph.

Applies every migration from ``base`` to ``head`` on a throw-away
SQLite DB and verifies:

- The final tables + columns match what ``init_s1`` creates in code.
- ``downgrade base`` removes everything cleanly.
- ``20260422_classifier_columns.down_revision`` points at the initial
  schema revision (guards against a future merge that resets the chain).
"""

from __future__ import annotations

import pathlib
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _config(db_path: pathlib.Path) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_head_creates_full_s1_schema(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "state.db"
    cfg = _config(db_path)
    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    # alembic_version is the bookkeeping table; everything else is ours.
    assert {"item", "run", "apicall"}.issubset(tables)

    with sqlite3.connect(db_path) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(item)")}
    # Classifier columns are added by the second migration and must be
    # present after upgrade to head.
    assert "classification" in cols
    assert "needs_review" in cols
    # Fields from the initial schema are still there.
    assert {"id", "zotero_item_key", "tags_json", "stage_completed"}.issubset(cols)


def test_downgrade_base_removes_all_s1_tables(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "state.db"
    cfg = _config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    with sqlite3.connect(db_path) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    # Only alembic's bookkeeping should survive base.
    assert tables <= {"alembic_version"}


def test_classifier_migration_depends_on_initial_schema(tmp_path: pathlib.Path) -> None:
    cfg = _config(tmp_path / "irrelevant.db")
    script_dir = ScriptDirectory.from_config(cfg)
    classifier = script_dir.get_revision("20260422_classifier_columns")
    assert classifier is not None
    assert classifier.down_revision == "20260420_initial_schema"
