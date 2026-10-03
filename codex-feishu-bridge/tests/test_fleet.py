from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import pytest

from codex_feishu_bridge.db import BridgeDB
from codex_feishu_bridge.fleet import apply_policy, enqueue_notice, update_bridge_config, work_counts
from codex_feishu_bridge.config import BridgeConfig, load_config
from codex_feishu_bridge.models import ThreadSummary
from codex_feishu_bridge.service import BridgeService

POLICY = {"model": "gpt-6.1-sol", "effort": "xhigh", "random_model": "gpt-6-astra", "random_effort": "ultra"}


def test_title_policy_and_explicit_overrides(tmp_path: Path):
    class Client:
        def add_notification_handler(self, handler):
            pass
        def set_server_request_handler(self, handler):
            pass
        def configure_thread_defaults(self, **kwargs):
            self.defaults = kwargs["config_overrides"]
    cfg = BridgeConfig(config_path=tmp_path / "config.toml", state_dir=tmp_path,
                       model="gpt-6.1-sol", model_reasoning_effort="xhigh", service_tier="priority",
                       random_model="gpt-6-astra", random_reasoning_effort="ultra", random_service_tier="priority")
    db = BridgeDB(tmp_path / "bridge.sqlite")
    db.upsert_thread(ThreadSummary("random", "RaNdOm", "", str(tmp_path), 1, 2, "cli"), title="RaNdOm")
    client = Client()
    service = BridgeService(cfg, db, client, object())
    assert service._runtime_settings("random").model == "gpt-6-astra"
    assert service._runtime_settings("random").effort == "ultra"
    assert service._runtime_settings("admin").effort == "xhigh"
    assert client.defaults["service_tier"] == "standard"
    db.set_runtime_config("random", "model", "gpt-6.1-sol", message_id="user")
    assert service._runtime_settings("random").model == "gpt-6.1-sol"
    db.close()


def test_config_edit_preserves_application_identity_and_comments(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[bridge]\n# keep this\nmodel = "old"\nallowed_workspace_roots = ["~"]\n\n[feishu.conversation]\napp_id = "test-app"\napp_secret_env = "TEST_SECRET"\n')
    update_bridge_config(path, {"model": "gpt-6.1-sol", "model_reasoning_effort": "xhigh"})
    cfg = load_config(path)
    assert cfg.model == "gpt-6.1-sol"
    assert cfg.model_reasoning_effort == "xhigh"
    assert cfg.feishu.conversation.app_id == "test-app"
    assert cfg.feishu.conversation.app_secret_env == "TEST_SECRET"
    assert "# keep this" in path.read_text()


def test_policy_preserves_permissions_history_and_applies_once(tmp_path: Path):
    db = BridgeDB(tmp_path / "bridge.sqlite")
    for tid, title in [("one", "Project"), ("two", "TennisRaNdOm")]:
        db.upsert_thread(ThreadSummary(tid, title, "", str(tmp_path), 1, 2, "cli"), title=title)
    db.set_runtime_config("one", "approval_policy", "on-request", message_id="test")
    counts = apply_policy(db, POLICY, "r1")
    assert counts == {"regular": 2, "random": 1}  # includes admin
    assert db.get_setting("runtime:one:model") == "gpt-6.1-sol"
    assert db.get_setting("runtime:two:model") == "gpt-6-astra"
    assert db.get_setting("runtime:two:effort") == "ultra"
    assert db.get_setting("runtime:one:approval_policy") == "on-request"
    db.set_runtime_config("one", "effort", "low", message_id="user")
    apply_policy(db, POLICY, "r1")
    assert db.get_setting("runtime:one:effort") == "low"
    assert not any(work_counts(db.path).values())
    db.close()


def test_deployment_notice_is_durable_and_idempotent(tmp_path: Path):
    db = BridgeDB(tmp_path / "bridge.sqlite")
    db.set_setting("owner_open_id:conversation", "test-owner")
    cfg = BridgeConfig(config_path=tmp_path / "config.toml")
    enqueue_notice(db, cfg, "a" * 40, "deployed")
    enqueue_notice(db, cfg, "a" * 40, "deployed")
    with sqlite3.connect(db.path) as c:
        assert c.execute("SELECT COUNT(*) FROM outbox_messages").fetchone()[0] == 1
        assert json.loads(c.execute("SELECT content_json FROM outbox_messages").fetchone()[0]) == {"text": "deployed"}
    db.close()


def test_auxiliary_jobs_follow_release_without_changing_timers(tmp_path: Path, monkeypatch):
    from codex_feishu_bridge import fleet
    units = tmp_path / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "codex-feishu-daily-stats.service").write_text("[Service]\n")
    timer = units / "codex-feishu-daily-stats.timer"
    timer.write_text("[Timer]\nOnCalendar=*-*-* *:10:00\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dropins = fleet.auxiliary_dropins(tmp_path / "release/codex-feishu-bridge")
    assert len(dropins) == 1
    assert "sync-daily-stats" in next(iter(dropins.values()))
    assert "release/codex-feishu-bridge/.venv" in next(iter(dropins.values()))
    assert timer.read_text().endswith("OnCalendar=*-*-* *:10:00\n")


def test_remote_bootstrap_generates_real_newlines():
    import ast
    script = Path(__file__).parents[1] / "scripts/fleet-controller.py"
    tree = ast.parse(script.read_text())
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "BOOTSTRAP" for t in n.targets))
    bootstrap = ast.literal_eval(assignment.value)
    parsed = ast.parse(bootstrap)
    contents = [ast.literal_eval(n) for n in ast.walk(parsed)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("[Unit]")]
    assert contents
    assert all("\n[Service]\n" in c or "\n[Timer]\n" in c for c in contents)


def test_failed_activation_rolls_back_settings_without_losing_new_messages(tmp_path: Path, monkeypatch):
    from codex_feishu_bridge import fleet
    home = tmp_path / "home"
    home.mkdir()
    state = home / "state"
    state.mkdir()
    config = home / "config.toml"
    config.write_text(f'[bridge]\nstate_dir = "{state}"\nallowed_workspace_roots = ["{home}"]\nmodel = "old"\n')
    original = config.read_text()
    db = BridgeDB(state / "bridge.sqlite")
    db.upsert_thread(ThreadSummary("one", "Project", "", str(home), 1, 2, "cli"), title="Project")
    db.set_runtime_config("one", "model", "old", message_id="user")
    db.close()
    sha = "a" * 40
    request = state / "request.json"
    request.write_text(json.dumps({"sha": sha, "tag": "bridge-test", "release_dir": str(home / "release"),
                                  "bridge_version": "0.4.0", "codex_version": "0.160.0",
                                  "codex_bin": "codex", "policy": POLICY, "policy_revision": "r1"}))
    monkeypatch.setattr(fleet, "STATE", state)
    monkeypatch.setattr(fleet, "CONFIG", config)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(fleet.time, "sleep", lambda _: None)
    monkeypatch.setattr(fleet.importlib.metadata, "version", lambda _: "0.4.0")
    monkeypatch.setattr(fleet.subprocess, "check_output", lambda *a, **k: sha)
    monkeypatch.setattr(fleet.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    async def catalog(*a, **k):
        return {"cli_version": "0.160.0"}
    monkeypatch.setattr(fleet, "verify_catalog", catalog)
    def failed_start(root):
        d = BridgeDB(state / "bridge.sqlite")
        d.enqueue_outbox(__import__("codex_feishu_bridge.models", fromlist=["OutboxItem"]).OutboxItem(
            "new-message", "conversation", "owner", "open_id", "text", {"text": "received after backup"}))
        d.close()
        raise RuntimeError("injected start failure")
    monkeypatch.setattr(fleet, "wait_loaded_release", failed_start)
    with pytest.raises(RuntimeError, match="injected"):
        fleet.activate(request)
    assert config.read_text() == original
    db = BridgeDB(state / "bridge.sqlite")
    assert db.get_setting("runtime:one:model") == "old"
    assert db.get_setting("fleet_policy_revision") is None
    with sqlite3.connect(db.path) as c:
        assert c.execute("SELECT COUNT(*) FROM outbox_messages WHERE outbox_key='new-message'").fetchone()[0] == 1
    db.close()
