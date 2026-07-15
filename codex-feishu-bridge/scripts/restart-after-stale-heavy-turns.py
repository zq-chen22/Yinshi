#!/usr/bin/env python3
"""Activate a release after explicitly designated image-heavy turns go stale.

This is an operator recovery tool, not a normal updater. The named turns are
intentionally terminated by replacing the old bridge process, but only after
all other durable jobs and outbound messages drain. Each named turn must be old
enough and its recorded input usage must exceed the configured safety floor.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--database", type=Path, required=True)
    result.add_argument("--stale-turn-id", action="append", required=True)
    result.add_argument("--service", default="codex-feishu-bridge.service")
    result.add_argument("--min-input-tokens", type=int, default=100_000)
    result.add_argument("--min-age-seconds", type=int, default=1800)
    result.add_argument("--max-wait-seconds", type=float, default=21_600.0)
    return result


def placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def stale_jobs(
    database: Path, turn_ids: list[str]
) -> list[tuple[str, str, str, int]]:
    uri = f"file:{database.expanduser().resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        rows = connection.execute(
            "SELECT turn_id,thread_id,state,created_at FROM turn_jobs "
            f"WHERE turn_id IN ({placeholders(turn_ids)})",
            turn_ids,
        ).fetchall()
    return [(str(a), str(b), str(c), int(d)) for a, b, c, d in rows]


def durable_other_work(
    database: Path, stale_turn_ids: list[str]
) -> tuple[int, int, int]:
    uri = f"file:{database.expanduser().resolve()}?mode=ro"
    marker = placeholders(stale_turn_ids)
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        message_rows = connection.execute(
            f"SELECT message_id FROM turn_jobs WHERE turn_id IN ({marker})",
            stale_turn_ids,
        ).fetchall()
        message_ids = [str(row[0]) for row in message_rows]
        turns = int(
            connection.execute(
                "SELECT COUNT(*) FROM turn_jobs "
                f"WHERE state IN ('accepted', 'running') AND turn_id NOT IN ({marker})",
                stale_turn_ids,
            ).fetchone()[0]
        )
        if message_ids:
            inbox_marker = placeholders(message_ids)
            inbox = int(
                connection.execute(
                    "SELECT COUNT(*) FROM inbox_messages "
                    "WHERE state IN ('processing', 'queued', 'dispatching') "
                    f"AND message_id NOT IN ({inbox_marker})",
                    message_ids,
                ).fetchone()[0]
            )
        else:
            inbox = int(
                connection.execute(
                    "SELECT COUNT(*) FROM inbox_messages "
                    "WHERE state IN ('processing', 'queued', 'dispatching')"
                ).fetchone()[0]
            )
        outbox = int(
            connection.execute(
                "SELECT COUNT(*) FROM outbox_messages "
                "WHERE state IN ('pending', 'retry', 'sending')"
            ).fetchone()[0]
        )
    return turns, inbox, outbox


def verify_stale_heavy_jobs(
    database: Path,
    turn_ids: list[str],
    *,
    min_input_tokens: int,
    min_age_seconds: int,
) -> list[str]:
    jobs = stale_jobs(database, turn_ids)
    found = {turn_id for turn_id, _, _, _ in jobs}
    missing = sorted(set(turn_ids) - found)
    if missing:
        raise RuntimeError(f"stale turn jobs not found: {', '.join(missing)}")
    now = int(time.time())
    thread_ids: list[str] = []
    uri = f"file:{database.expanduser().resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        for turn_id, thread_id, state, created_at in jobs:
            if state not in {"accepted", "running"}:
                raise RuntimeError(
                    f"refusing cutover: {turn_id} is already {state}"
                )
            age = max(0, now - created_at)
            if age < min_age_seconds:
                raise RuntimeError(
                    f"refusing cutover: {turn_id} age {age}s is below floor"
                )
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?",
                (f"token_usage:{thread_id}:input",),
            ).fetchone()
            input_tokens = int(row[0]) if row else 0
            if input_tokens < min_input_tokens:
                raise RuntimeError(
                    f"refusing cutover: {turn_id} has only {input_tokens} input tokens"
                )
            thread_ids.append(thread_id)
    return thread_ids


def service_pid(service: str) -> int:
    result = subprocess.run(
        ["systemctl", "--user", "show", service, "-p", "MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip() or "0")


def clear_dead_leases(database: Path, thread_ids: list[str]) -> None:
    with sqlite3.connect(database, timeout=30) as connection:
        connection.executemany(
            "DELETE FROM thread_leases WHERE thread_id=?",
            [(thread_id,) for thread_id in thread_ids],
        )


def record_status(database: Path, value: dict[str, object]) -> None:
    now = int(time.time())
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            (
                "stale_heavy_turn_cutover",
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )


def main() -> int:
    args = parser().parse_args()
    turn_ids = list(dict.fromkeys(args.stale_turn_id))
    thread_ids = verify_stale_heavy_jobs(
        args.database,
        turn_ids,
        min_input_tokens=args.min_input_tokens,
        min_age_seconds=args.min_age_seconds,
    )
    deadline = time.monotonic() + max(0.0, args.max_wait_seconds)
    idle_since: float | None = None
    last_counts: tuple[int, int, int] | None = None
    while time.monotonic() < deadline:
        counts = durable_other_work(args.database, turn_ids)
        if counts != last_counts:
            print(
                f"waiting: other_turns={counts[0]} inbox={counts[1]} outbox={counts[2]}",
                flush=True,
            )
            last_counts = counts
        if counts == (0, 0, 0):
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= 5:
                break
        else:
            idle_since = None
        time.sleep(2)
    else:
        record_status(args.database, {"ok": False, "stage": "wait-timeout"})
        return 2

    # Revalidate immediately before the intentional process replacement.
    thread_ids = verify_stale_heavy_jobs(
        args.database,
        turn_ids,
        min_input_tokens=args.min_input_tokens,
        min_age_seconds=args.min_age_seconds,
    )
    old_pid = service_pid(args.service)
    if not old_pid:
        raise RuntimeError("bridge service has no running MainPID")
    subprocess.run(
        [
            "systemctl",
            "--user",
            "kill",
            "--kill-whom=all",
            "--signal=SIGKILL",
            args.service,
        ],
        check=True,
    )
    clear_dead_leases(args.database, thread_ids)
    subprocess.run(["systemctl", "--user", "start", args.service], check=False)

    active_deadline = time.monotonic() + 90
    while time.monotonic() < active_deadline:
        new_pid = service_pid(args.service)
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", args.service],
            check=False,
        ).returncode == 0
        if active and new_pid and new_pid != old_pid:
            result = {
                "ok": True,
                "stage": "complete",
                "terminated_turns": turn_ids,
                "old_pid": old_pid,
                "new_pid": new_pid,
            }
            record_status(args.database, result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        time.sleep(1)

    record_status(
        args.database,
        {"ok": False, "stage": "restart-timeout", "old_pid": old_pid},
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
