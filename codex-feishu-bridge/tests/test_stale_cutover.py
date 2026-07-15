from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path
from types import ModuleType

import pytest


def load_script() -> ModuleType:
    path = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "restart-after-stale-heavy-turns.py"
    )
    spec = importlib.util.spec_from_file_location("stale_cutover", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_database(path: Path) -> None:
    now = int(time.time())
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE turn_jobs(
                message_id TEXT, thread_id TEXT, turn_id TEXT,
                state TEXT, created_at INTEGER
            );
            CREATE TABLE inbox_messages(message_id TEXT, state TEXT);
            CREATE TABLE outbox_messages(state TEXT);
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
            CREATE TABLE thread_leases(thread_id TEXT PRIMARY KEY);
            """
        )
        rows = [
            ("message-a", "thread-a", "turn-a", "accepted", now - 7200),
            ("message-b", "thread-b", "turn-b", "running", now - 3600),
            ("message-live", "thread-live", "turn-live", "accepted", now),
        ]
        db.executemany("INSERT INTO turn_jobs VALUES(?,?,?,?,?)", rows)
        db.executemany(
            "INSERT INTO settings VALUES(?,?,?)",
            [
                ("token_usage:thread-a:input", "118704", now),
                ("token_usage:thread-b:input", "212788", now),
            ],
        )
        db.executemany(
            "INSERT INTO inbox_messages VALUES(?,?)",
            [
                ("message-a", "done"),
                ("message-b", "done"),
                ("message-live", "dispatching"),
            ],
        )
        db.execute("INSERT INTO outbox_messages VALUES('pending')")
        db.executemany(
            "INSERT INTO thread_leases VALUES(?)", [("thread-a",), ("thread-b",)]
        )


def test_stale_cutover_validates_named_heavy_turns_and_counts_other_work(
    tmp_path: Path,
) -> None:
    module = load_script()
    database = tmp_path / "bridge.sqlite"
    make_database(database)

    threads = module.verify_stale_heavy_jobs(
        database,
        ["turn-a", "turn-b"],
        min_input_tokens=100_000,
        min_age_seconds=1800,
    )
    assert set(threads) == {"thread-a", "thread-b"}
    assert module.durable_other_work(database, ["turn-a", "turn-b"]) == (1, 1, 1)

    with pytest.raises(RuntimeError, match="not found"):
        module.verify_stale_heavy_jobs(
            database,
            ["missing"],
            min_input_tokens=100_000,
            min_age_seconds=1800,
        )

    module.clear_dead_leases(database, threads)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM thread_leases").fetchone()[0] == 0
