#!/usr/bin/env python3
"""Restart the user bridge only after its durable work queues become idle.

This helper is mainly for activating the first graceful-drain-aware release:
the already running older process cannot learn the new SIGTERM behavior until
it has restarted once.  Future normal ``systemctl --user restart`` operations
are protected by the service's own drain path as well.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import time
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--database", type=Path, required=True)
    result.add_argument("--service", default="codex-feishu-bridge.service")
    result.add_argument("--poll-seconds", type=float, default=1.0)
    result.add_argument("--idle-window-seconds", type=float, default=3.0)
    result.add_argument("--max-wait-seconds", type=float, default=21_600.0)
    result.add_argument("--active-wait-seconds", type=float, default=60.0)
    return result


def durable_work_counts(database: Path) -> tuple[int, int, int]:
    uri = f"file:{database.expanduser().resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        active_turns = int(
            connection.execute(
                "SELECT COUNT(*) FROM turn_jobs WHERE state IN ('accepted', 'running')"
            ).fetchone()[0]
        )
        unsafe_inbox = int(
            connection.execute(
                "SELECT COUNT(*) FROM inbox_messages "
                "WHERE state IN ('processing', 'queued', 'dispatching')"
            ).fetchone()[0]
        )
        pending_outbox = int(
            connection.execute(
                "SELECT COUNT(*) FROM outbox_messages "
                "WHERE state IN ('pending', 'retry', 'sending')"
            ).fetchone()[0]
        )
    return active_turns, unsafe_inbox, pending_outbox


def main() -> int:
    args = parser().parse_args()
    deadline = time.monotonic() + max(0.0, args.max_wait_seconds)
    idle_since: float | None = None
    last_counts: tuple[int, int, int] | None = None
    while time.monotonic() < deadline:
        counts = durable_work_counts(args.database)
        if counts != last_counts:
            print(
                "等待桥空闲："
                f"active_turns={counts[0]} unsafe_inbox={counts[1]} "
                f"pending_outbox={counts[2]}",
                flush=True,
            )
            last_counts = counts
        if counts == (0, 0, 0):
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= max(0.0, args.idle_window_seconds):
                break
        else:
            idle_since = None
        time.sleep(max(0.1, args.poll_seconds))
    else:
        print("等待桥空闲超时；为避免打断任务，本次不重启。", flush=True)
        return 2

    subprocess.run(
        ["systemctl", "--user", "restart", args.service],
        check=True,
    )
    active_deadline = time.monotonic() + max(0.0, args.active_wait_seconds)
    while time.monotonic() < active_deadline:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", args.service],
            check=False,
        )
        if result.returncode == 0:
            print(f"{args.service} 已重启并恢复 active。", flush=True)
            return 0
        time.sleep(1)
    print(f"{args.service} 重启后未在时限内恢复 active。", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
