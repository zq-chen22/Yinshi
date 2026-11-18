from __future__ import annotations

import json
from pathlib import Path

from codex_feishu_bridge import fleet


def request() -> dict:
    return {
        "codex_bin": "/verified/runtime/bin/codex",
        "policy": {
            "model": "model-next",
            "effort": "high",
            "random_model": "random-next",
            "random_effort": "ultra",
        },
        "policy_revision": "revision-next",
    }


def test_bridge_only_release_does_not_change_global_preferences(tmp_path: Path) -> None:
    deployment = {**request(), "preserve_runtime_settings": True}
    path = tmp_path / "config.toml"
    original = '[bridge]\ncodex_bin = "/verified/runtime/bin/codex"\nmodel = "user-chosen"\nservice_tier = "standard"\nnew_thread_reasoning_effort = "xhigh"\n\n[other]\nvalue = "preserve-me"\n'
    path.write_text(original)
    assert fleet.activation_config_updates(deployment) == {
        "codex_bin": deployment["codex_bin"]
    }
    fleet.update_bridge_config(path, fleet.activation_config_updates(deployment))
    assert path.read_text() == original


def test_bridge_only_release_does_not_apply_policy(monkeypatch) -> None:
    def forbidden(*args):
        raise AssertionError("User runtime choices must not be rewritten")

    monkeypatch.setattr(fleet, "apply_policy", forbidden)
    result = fleet.apply_deployment_policy(
        None, {**request(), "preserve_runtime_settings": True}
    )
    assert result == {"runtime_settings_preserved": True}


def test_legacy_release_policy_behavior_is_preserved(monkeypatch) -> None:
    deployment = request()
    calls = []

    def apply(database, policy, revision):
        calls.append((database, policy, revision))
        return {"scopes": 1}

    monkeypatch.setattr(fleet, "apply_policy", apply)
    assert fleet.apply_deployment_policy(None, deployment) == {"scopes": 1}
    assert calls == [(None, deployment["policy"], deployment["policy_revision"])]
    assert fleet.activation_config_updates(deployment)["model"] == "model-next"
    assert fleet.activation_config_updates(deployment)["service_tier"] == "priority"


def test_release_channel_keeps_verified_cli_version() -> None:
    root = Path(__file__).resolve().parents[2]
    channel = json.loads((root / "deploy/stable.json").read_text())
    assert channel["bridge_version"] == "0.4.5"
    assert channel["codex_version"] == "0.170.0"
