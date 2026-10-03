"""Idle-only activation of immutable Yinshi releases; never copy host state."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import sqlite3
import subprocess
import tempfile
import time

from .codex_client import CodexAppServer
from .config import load_config
from .db import BridgeDB
from .models import OutboxItem

STATE = Path.home() / ".local/share/codex-feishu-bridge"
CONFIG = Path.home() / ".config/codex-feishu-bridge/config.toml"
UNIT = "codex-feishu-bridge.service"


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def work_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as c:
        return {
            "turns": c.execute("SELECT COUNT(*) FROM turn_jobs WHERE state IN ('accepted','running')").fetchone()[0],
            "inbox": c.execute("SELECT COUNT(*) FROM inbox_messages WHERE state IN ('processing','queued','dispatching')").fetchone()[0],
            "outbox": c.execute("SELECT COUNT(*) FROM outbox_messages WHERE state IN ('pending','retry','sending')").fetchone()[0],
        }


def update_bridge_config(path: Path, values: dict) -> None:
    """Edit only requested bridge fields, preserving credential references and tables."""
    original = path.read_text()
    match = re.search(r"(?m)^\[bridge\]\s*$", original)
    if not match:
        raise ValueError("config is missing [bridge]")
    tail = original[match.end():]
    next_table = re.search(r"(?m)^\[", tail)
    end = match.end() + (next_table.start() if next_table else len(tail))
    section = original[match.end():end]
    for key, value in values.items():
        rendered = json.dumps(value, ensure_ascii=False)
        pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
        line = f"{key} = {rendered}"
        if re.search(pattern, section):
            section = re.sub(pattern, lambda _: line, section)
        else:
            section = section.rstrip() + "\n" + line + "\n\n"
    result = original[:match.end()] + section + original[end:]
    temporary = path.with_suffix(".fleet.tmp")
    temporary.write_text(result)
    temporary.chmod(0o600)
    os.replace(temporary, path)


async def verify_catalog(codex_bin: str, policy: dict) -> dict:
    # The node may need a local proxy that is configured on its main unit.
    # Reuse it in memory only; do not log, persist, or transfer its values.
    names = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy", "CODEX_HOME"}
    raw = subprocess.check_output(["systemctl", "--user", "show", UNIT, "-p", "Environment", "--value"], text=True)
    env = {key: value for item in shlex.split(raw) if "=" in item
           for key, value in [item.split("=", 1)] if key in names}
    async with CodexAppServer(codex_bin, request_timeout=45, env=env) as client:
        models = await client.list_models()
        for name, effort in [(policy["model"], policy["effort"]),
                             (policy["random_model"], policy["random_effort"])]:
            model = next((m for m in models if name in (m.get("id"), m.get("model"))), None)
            if not model:
                raise ValueError(f"requested model unavailable: {name}")
            efforts = [e["reasoningEffort"] for e in model.get("supportedReasoningEfforts", [])]
            tiers = [t["id"] for t in model.get("serviceTiers", [])]
            if effort not in efforts or "priority" not in tiers:
                raise ValueError(f"requested effort/Fast unavailable for {name}: {effort}")
            thread = await client.start_thread(
                cwd=str(Path.home()), approval_policy="on-request", sandbox="read-only",
                model=name, service_tier="priority", ephemeral=True,
            )
            try:
                await client.update_thread_settings(
                    thread["id"], approval_policy="on-request", sandbox="read-only",
                    model=name, effort=effort, service_tier="priority",
                )
            finally:
                await client.unsubscribe_thread(thread["id"])
        return {"cli_version": client.cli_version, "models_checked": True, "settings_rpc_checked": True}


def current_status() -> dict:
    cfg = load_config(CONFIG)
    marker = STATE / "deployment.json"
    value = json.loads(marker.read_text()) if marker.exists() else {}
    value["queues"] = work_counts(cfg.database_path)
    value["service_active"] = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", UNIT], check=False
    ).returncode == 0
    return value


def apply_policy(db: BridgeDB, policy: dict, revision: str) -> dict[str, int]:
    if db.get_setting("fleet_policy_revision", "") == revision:
        return {"unchanged": 1}
    scopes = {}
    # Preserve orphaned/admin setting scopes without changing any task history.
    with sqlite3.connect(db.path.as_uri() + "?mode=ro", uri=True) as c:
        scopes.update(c.execute("SELECT thread_id,title FROM bindings"))
        for (key,) in c.execute("SELECT key FROM settings WHERE key LIKE 'runtime:%'"):
            scopes.setdefault(key[8:].rsplit(":", 1)[0], "")
    scopes.setdefault("admin", "")
    counts = {"regular": 0, "random": 0}
    for scope, title in scopes.items():
        random = "random" in title.casefold()
        values = {"model": policy["random_model"] if random else policy["model"],
                  "effort": policy["random_effort"] if random else policy["effort"],
                  "service_tier": "priority"}
        for name, value in values.items():
            db.set_runtime_config(scope, name, value, message_id=f"fleet-policy:{revision}")
        counts["random" if random else "regular"] += 1
    db.set_setting("fleet_policy_revision", revision)
    return counts


def enqueue_notice(db: BridgeDB, cfg, sha: str, text: str) -> None:
    owner = db.get_setting("owner_open_id:conversation", "") or cfg.feishu.owner_conversation_open_id
    if owner:
        db.enqueue_outbox(OutboxItem(
            outbox_key=f"fleet-deploy:{sha}", app_role="conversation",
            receive_id=owner, receive_id_type="open_id", msg_type="text",
            content={"text": text},
        ))


def wait_loaded_release(root: Path, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = subprocess.check_output(["systemctl", "--user", "show", UNIT, "-p", "MainPID", "--value"], text=True).strip()
        if pid.isdigit() and int(pid):
            path = Path("/proc") / pid / "cmdline"
            if path.exists() and str(root).encode() in path.read_bytes():
                time.sleep(5)
                if subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0:
                    return
        time.sleep(1)
    raise RuntimeError("new bridge did not become healthy")


def auxiliary_dropins(root: Path) -> dict[Path, str]:
    """Point installed auxiliary jobs at the same release; keep their timers/env."""
    units = Path.home() / ".config/systemd/user"
    commands = {
        "codex-feishu-daily-stats.service": f'"{root / ".venv/bin/codex-feishu-bridge"}" --config "{CONFIG}" sync-daily-stats',
        "codex-feishu-bridge-heartbeat.service": f'"{root / ".venv/bin/python"}" -m codex_feishu_bridge.heartbeat bridge',
    }
    return {
        units / (name + ".d") / "50-yinshi-release.conf":
        f'[Service]\nWorkingDirectory={root}\nExecStart=\nExecStart={command}\n'
        for name, command in commands.items() if (units / name).exists()
    }


def bridge_dropin(root: Path) -> str:
    executable = root / ".venv/bin/codex-feishu-bridge"
    return f'[Service]\nWorkingDirectory={root}\nExecStart=\nExecStart="{executable}" --config "{CONFIG}" run\nTimeoutStopSec=21630\nKillMode=mixed\n'


def validate_units(root: Path, auxiliaries: dict[Path, str]) -> None:
    """Use the real systemd parser before stopping any existing process."""
    with tempfile.TemporaryDirectory(prefix="unit-check-", dir=STATE) as temporary:
        paths = []
        for i, content in enumerate([bridge_dropin(root), *auxiliaries.values()]):
            path = Path(temporary) / f"yinshi-check-{i}.service"
            path.write_text("[Unit]\nDescription=Yinshi preflight validation\n" + content)
            paths.append(str(path))
        result = subprocess.run(["systemd-analyze", "--user", "verify", *paths],
                                check=True, capture_output=True, text=True, timeout=30)
        if result.stderr and result.stderr.strip():
            raise RuntimeError("systemd preflight emitted unit diagnostics")


def activate(request_path: Path) -> int:
    request = json.loads(request_path.read_text())
    sha = request["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("deployment must pin a full Git commit")
    release = Path(request["release_dir"]).resolve()
    cfg = load_config(CONFIG)
    marker = STATE / "deployment.json"
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    if (previous.get("sha") == sha
            and previous.get("policy_revision") == request["policy_revision"]
            and current_status()["service_active"]
            and subprocess.check_output([request["codex_bin"], "--version"], text=True).strip().split()[-1] == request["codex_version"]):
        print("already active: " + sha)
        return 0
    counts = work_counts(cfg.database_path)
    if any(counts.values()):
        print(json.dumps({"deferred": True, "queues": counts}))
        return 0
    # Ensure idle remains stable. The old service's own drain protects the
    # remaining arrival race; new/pending messages stay in its durable inbox.
    time.sleep(3)
    if any(work_counts(cfg.database_path).values()):
        print("new work arrived; deferred")
        return 0
    policy = request["policy"]
    probe = asyncio.run(verify_catalog(request["codex_bin"], policy))
    if probe["cli_version"] != request["codex_version"]:
        raise ValueError("Codex version differs from the promoted release")
    if importlib.metadata.version("codex-feishu-bridge") != request["bridge_version"]:
        raise ValueError("installed bridge differs from promoted release")
    actual_sha = subprocess.check_output(["git", "-C", str(release), "rev-parse", "HEAD"], text=True).strip()
    if actual_sha != sha:
        raise ValueError("release checkout differs from pinned SHA")
    backup = STATE / "deployments" / sha
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    dropin = Path.home() / ".config/systemd/user" / (UNIT + ".d") / "50-yinshi-release.conf"
    original_dropin = dropin.read_text() if dropin.exists() else None
    auxiliaries = auxiliary_dropins(release / "codex-feishu-bridge")
    validate_units(release / "codex-feishu-bridge", auxiliaries)
    original_auxiliaries = {p: p.read_text() if p.exists() else None for p in auxiliaries}
    original_config = CONFIG.read_text()
    shutil.copy2(CONFIG, backup / "config.toml")
    (backup / "config.toml").chmod(0o600)
    atomic_json(backup / "previous.json", {"deployment": previous, "dropin": original_dropin,
                                         "auxiliaries": {str(p): v for p, v in original_auxiliaries.items()}})
    with sqlite3.connect(cfg.database_path.as_uri() + "?mode=ro", uri=True) as c:
        old_policy = dict(c.execute("SELECT key,value FROM settings WHERE key LIKE 'runtime:%' OR key='fleet_policy_revision'"))
    atomic_json(backup / "runtime-settings.json", old_policy)
    # Research servers may outlive their originating turn. Do not let a
    # routine upgrade kill these grandchild processes with the service cgroup.
    preserve = dropin.parent / "90-yinshi-preserve-workers.conf"
    preserve.parent.mkdir(parents=True, exist_ok=True)
    preserve.write_text("[Service]\nKillMode=process\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "stop", UNIT], check=True, timeout=21640)
    if any(work_counts(cfg.database_path).values()):
        subprocess.run(["systemctl", "--user", "start", UNIT], check=True)
        preserve.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        raise RuntimeError("service stopped with unsafe work; no changes applied")
    try:
        with sqlite3.connect(cfg.database_path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup / "bridge.sqlite") as target:
                source.backup(target)
        (backup / "bridge.sqlite").chmod(0o600)
        update_bridge_config(CONFIG, {
            "codex_bin": request["codex_bin"], "model": policy["model"],
            "model_reasoning_effort": policy["effort"], "new_thread_reasoning_effort": policy["effort"],
            "service_tier": "priority", "random_model": policy["random_model"],
            "random_reasoning_effort": policy["random_effort"], "random_service_tier": "priority",
            # Existing private installations keep history; do not introduce
            # a new automatic deletion policy during migration.
            "data_retention_days": 0,
            "show_workspace_path": True,
        })
        updated = load_config(CONFIG)
        db = BridgeDB(updated.database_path)
        try:
            policy_counts = apply_policy(db, policy, request["policy_revision"])
        finally:
            db.close()
        root = release / "codex-feishu-bridge"
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(bridge_dropin(root))
        dropin.chmod(0o600)
        for path, content in auxiliaries.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o600)
        preserve.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "start", UNIT], check=True)
        wait_loaded_release(root)
        deployed = {k: request[k] for k in ["sha", "tag", "bridge_version", "codex_version", "policy_revision", "policy"]}
        deployed.update({"repository": "https://github.com/zq-chen22/Yinshi", "activated_at": int(time.time()), "release_dir": str(release), "policy_counts": policy_counts})
        atomic_json(marker, deployed)
        db = BridgeDB(updated.database_path)
        try:
            enqueue_notice(db, updated, sha, f"✅ Yinshi 飞书桥统一部署完成\n版本 {request['bridge_version']} · {sha[:12]}\nCodex {request['codex_version']}\n普通对话：{policy['model']} / {policy['effort']} / Fast\nRandom：{policy['random_model']} / {policy['random_effort']} / Fast\n历史、配对及群绑定已保留。")
        finally:
            db.close()
        print(json.dumps(deployed, ensure_ascii=False))
        return 0
    except Exception:
        # Keep the live DB/WAL and received messages. Never roll back SQLite
        # to a pre-deploy snapshot and silently lose post-backup messages.
        preserve.write_text("[Service]\nKillMode=process\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=False, timeout=21640)
        CONFIG.write_text(original_config)
        CONFIG.chmod(0o600)
        with sqlite3.connect(cfg.database_path) as c:
            # Only undo our own latest setting events, not choices the user
            # may have made during the failed start attempt.
            rows = c.execute("SELECT scope,name FROM runtime_config_events WHERE message_id=?", (f"fleet-policy:{request['policy_revision']}",)).fetchall()
            for scope, name in rows:
                latest = c.execute("SELECT message_id FROM runtime_config_events WHERE scope=? AND name=? ORDER BY id DESC LIMIT 1", (scope, name)).fetchone()
                if not latest or latest[0] != f"fleet-policy:{request['policy_revision']}":
                    continue
                key = f"runtime:{scope}:{name}"
                if key in old_policy:
                    c.execute("UPDATE settings SET value=? WHERE key=?", (old_policy[key], key))
                else:
                    c.execute("DELETE FROM settings WHERE key=?", (key,))
            if "fleet_policy_revision" in old_policy:
                c.execute("UPDATE settings SET value=? WHERE key='fleet_policy_revision'", (old_policy["fleet_policy_revision"],))
            else:
                c.execute("DELETE FROM settings WHERE key='fleet_policy_revision'")
        if original_dropin is None:
            dropin.unlink(missing_ok=True)
        else:
            dropin.write_text(original_dropin)
        for path, content in original_auxiliaries.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content)
        preserve.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "start", UNIT], check=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["activate", "status"])
    parser.add_argument("--request", type=Path, default=STATE / "deployment-request.json")
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(current_status(), ensure_ascii=False))
        return 0
    with (STATE / "deployment.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return activate(args.request)


if __name__ == "__main__":
    raise SystemExit(main())
