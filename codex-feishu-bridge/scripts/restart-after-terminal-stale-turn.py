#!/usr/bin/env python3
"""Activate a release when one bridge turn is proven terminal but stuck in memory.

This is a narrow recovery tool, not a normal updater. It waits for every other
durable job and outbox item to drain, independently verifies the named Codex
turn as terminal, then replaces the stale bridge process and removes only that
thread's dead-process lease.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import time
from pathlib import Path

from codex_feishu_bridge.codex_client import CodexAppServer


TERMINAL = {"completed", "failed", "interrupted"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--database", type=Path, required=True)
    result.add_argument("--thread-id", required=True)
    result.add_argument("--turn-id", required=True)
    result.add_argument("--codex-bin", default="codex")
    result.add_argument("--service", default="codex-feishu-bridge.service")
    result.add_argument("--max-wait-seconds", type=float, default=21_600.0)
    return result


def durable_other_work(
    database: Path, stale_turn_id: str
) -> tuple[int, int, int]:
    uri = f"file:{database.expanduser().resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        row = connection.execute(
            "SELECT message_id FROM turn_jobs WHERE turn_id=?", (stale_turn_id,)
        ).fetchone()
        stale_message_id = str(row[0]) if row else ""
        turns = int(
            connection.execute(
                "SELECT COUNT(*) FROM turn_jobs "
                "WHERE state IN ('accepted', 'running') AND turn_id<>?",
                (stale_turn_id,),
            ).fetchone()[0]
        )
        inbox = int(
            connection.execute(
                "SELECT COUNT(*) FROM inbox_messages "
                "WHERE state IN ('processing', 'queued', 'dispatching') "
                "AND message_id<>?",
                (stale_message_id,),
            ).fetchone()[0]
        )
        outbox = int(
            connection.execute(
                "SELECT COUNT(*) FROM outbox_messages "
                "WHERE state IN ('pending', 'retry', 'sending')"
            ).fetchone()[0]
        )
    return turns, inbox, outbox


async def terminal_status(codex_bin: str, thread_id: str, turn_id: str) -> str:
    async with CodexAppServer(codex_bin) as client:
        cursor: str | None = None
        for _ in range(5):
            page = await client.list_turns(
                thread_id,
                limit=100,
                items_view="notLoaded",
                sort_direction="desc",
                cursor=cursor,
            )
            for turn in page.get("data") or []:
                if str(turn.get("id") or "") == turn_id:
                    status = str(turn.get("status") or "")
                    if status not in TERMINAL:
                        raise RuntimeError(
                            f"refusing restart: target turn is {status or 'unknown'}"
                        )
                    return status
            cursor = page.get("nextCursor")
            if not cursor:
                break
    raise RuntimeError("refusing restart: target turn was not found")


def service_pid(service: str) -> int:
    result = subprocess.run(
        ["systemctl", "--user", "show", service, "-p", "MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip() or "0")


def clear_dead_lease(database: Path, thread_id: str) -> None:
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("DELETE FROM thread_leases WHERE thread_id=?", (thread_id,))


def record_status(database: Path, value: dict[str, object]) -> None:
    now = int(time.time())
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            (
                "terminal_stale_turn_restart",
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )


def main() -> int:
    args = parser().parse_args()
    deadline = time.monotonic() + max(0.0, args.max_wait_seconds)
    idle_since: float | None = None
    last_counts: tuple[int, int, int] | None = None
    while time.monotonic() < deadline:
        counts = durable_other_work(args.database, args.turn_id)
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

    status = asyncio.run(
        terminal_status(args.codex_bin, args.thread_id, args.turn_id)
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
    clear_dead_lease(args.database, args.thread_id)
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
                "terminal_status": status,
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
