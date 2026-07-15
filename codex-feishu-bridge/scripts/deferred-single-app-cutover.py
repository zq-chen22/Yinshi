#!/usr/bin/env python3
"""Restart the bridge after a delivered turn and verify one Feishu receiver."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
from pathlib import Path


def _turn_state(database: Path, turn_id: str) -> tuple[str, int]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state FROM turn_jobs WHERE turn_id=?", (turn_id,)
        ).fetchone()
        busy = connection.execute(
            "SELECT count(*) FROM turn_jobs WHERE state IN ('accepted', 'running')"
        ).fetchone()[0]
    return (str(row[0]) if row else "missing", int(busy))


def _service_main_pid(unit: str) -> int:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip() or "0")


def _receiver_count(main_pid: int) -> int:
    children_path = Path(f"/proc/{main_pid}/task/{main_pid}/children")
    if not children_path.exists():
        return 0
    count = 0
    for child in children_path.read_text().split():
        try:
            command = Path(f"/proc/{child}/cmdline").read_bytes().replace(b"\0", b" ")
        except FileNotFoundError:
            continue
        if b"multiprocessing.spawn" in command:
            count += 1
    return count


def _record(database: Path, value: dict[str, object]) -> None:
    now = int(time.time())
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at""",
            ("single_app_cutover_status", json.dumps(value, ensure_ascii=False), now),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--unit", default="codex-feishu-bridge.service")
    parser.add_argument("--wait-seconds", type=int, default=3600)
    args = parser.parse_args()

    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        state, busy = _turn_state(args.database, args.turn_id)
        if state == "delivered" and busy == 0:
            break
        time.sleep(2)
    else:
        _record(
            args.database,
            {"ok": False, "stage": "wait", "turn_id": args.turn_id},
        )
        return 1

    time.sleep(2)
    subprocess.run(
        ["systemctl", "--user", "restart", args.unit],
        check=True,
    )

    deadline = time.monotonic() + 45
    status: dict[str, object] = {
        "ok": False,
        "stage": "verify",
        "turn_id": args.turn_id,
    }
    while time.monotonic() < deadline:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", args.unit],
            check=False,
        ).returncode == 0
        main_pid = _service_main_pid(args.unit) if active else 0
        receivers = _receiver_count(main_pid) if main_pid else 0
        status.update(
            {"active": active, "main_pid": main_pid, "receiver_count": receivers}
        )
        if active and receivers == 1:
            status.update({"ok": True, "stage": "complete"})
            _record(args.database, status)
            return 0
        time.sleep(1)

    _record(args.database, status)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
