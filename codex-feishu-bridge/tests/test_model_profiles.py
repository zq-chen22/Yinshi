from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_feishu_bridge.db import BridgeDB
from codex_feishu_bridge.model_profiles import (
    ModelProfile,
    load_profiles,
    profile_changes,
    profile_command,
    profile_for_scope,
    validate_profile,
    initialize_profiles,
)


def models():
    return [
        {
            "model": "gpt-6.2-sol",
            "id": "gpt-6.2-sol",
            "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            "serviceTiers": [{"id": "priority"}],
        },
        {
            "model": "gpt-6.2-astra",
            "id": "gpt-6.2-astra",
            "supportedReasoningEfforts": [{"reasoningEffort": "max"}],
            "serviceTiers": [{"id": "priority"}],
        },
    ]


def presets():
    return {
        "default": ModelProfile("gpt-6.2-sol", "xhigh"),
        "advanced": ModelProfile("gpt-6.2-astra", "max"),
    }


@pytest.mark.parametrize("argument", ["gpt 6.2 sol xhigh", "gpt-6.2-sol xhigh"])
def test_model_name_and_effort_are_resolved_from_real_catalog(argument):
    assert validate_profile(argument, models(), advanced=False) == presets()["default"]


@pytest.mark.parametrize(
    "argument", ["gpt-6.2-unknown max", "gpt-6.2-sol ultra", "gpt-6.2-sol"]
)
def test_invalid_models_and_efforts_are_rejected(argument):
    with pytest.raises(ValueError):
        validate_profile(argument, models(), advanced=False)


def test_advanced_model_must_support_random_fast():
    catalog = models()
    catalog[1]["serviceTiers"] = []
    with pytest.raises(ValueError, match="Fast"):
        validate_profile("gpt-6.2-astra max", catalog, advanced=True)


@pytest.mark.parametrize(
    "text",
    ["默认", "高级", "全部默认", "全部高级", "模型档位", "默认模型 gpt 6.2 sol xhigh"],
)
def test_profile_commands_are_explicit(text):
    assert profile_command(text) is not None


@pytest.mark.parametrize(
    "text",
    ["默认情况下", "高级算法如何做", "请全部默认", "全部高级。", "默认模型的比较"],
)
def test_ordinary_questions_are_not_swallowed(text):
    assert profile_command(text) is None


@pytest.mark.parametrize("level", ["default", "advanced"])
@pytest.mark.parametrize("title", ["random", "RaNdOm Japan", "实验Random"])
def test_random_is_always_advanced_fast(level, title):
    changes = {
        name: value
        for _, name, value in profile_changes("thread", title, level, presets())
    }
    assert changes == {
        "model": "gpt-6.2-astra",
        "effort": "max",
        "service_tier": "priority",
        "model_profile": "advanced",
    }


def test_normal_profile_switch_turns_fast_off_without_touching_permissions():
    changes = {
        name: value
        for _, name, value in profile_changes("thread", "研究", "advanced", presets())
    }
    assert changes["model"] == "gpt-6.2-astra"
    assert changes["service_tier"] == "__off__"
    assert "sandbox" not in changes and "approval_policy" not in changes


def test_batch_profile_write_and_audit_are_atomic(tmp_path: Path):
    database = BridgeDB(tmp_path / "bridge.sqlite")
    try:
        database._conn.execute(
            "CREATE TRIGGER reject_effort BEFORE INSERT ON settings WHEN NEW.key LIKE '%:effort' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
        database._conn.commit()
        changes = [
            ("model_profiles", level, preset.encode())
            for level, preset in presets().items()
        ] + profile_changes("thread", "研究", "advanced", presets())
        with pytest.raises(sqlite3.IntegrityError):
            database.set_runtime_configs(changes, message_id="atomic-test")
        assert load_profiles(database) == {}
        assert database.get_setting("runtime:thread:model") is None
        assert database.runtime_config_history("thread") == []
        assert database.runtime_config_history("model_profiles") == []
    finally:
        database.close()


def test_definitions_survive_restart_and_manual_scope_can_opt_out(tmp_path: Path):
    path = tmp_path / "bridge.sqlite"
    database = BridgeDB(path)
    database.set_runtime_configs(
        [
            ("model_profiles", level, preset.encode())
            for level, preset in presets().items()
        ],
        message_id="setup",
    )
    database.close()
    database = BridgeDB(path)
    try:
        assert profile_for_scope(database, "new", "研究") == (
            "default",
            presets()["default"],
        )
        database.set_runtime_config("new", "model_profile", "", message_id="manual")
        assert profile_for_scope(database, "new", "研究") is None
        assert profile_for_scope(database, "new", "random") == (
            "advanced",
            presets()["advanced"],
        )
    finally:
        database.close()


def test_one_time_migration_preserves_later_user_choice_and_local_only_threads(
    tmp_path,
):
    from test_service import bind_thread

    database = BridgeDB(tmp_path / "bridge.sqlite")
    try:
        bind_thread(database)
        bind_thread(database, thread_id="random", chat_id="oc_random")
        database._conn.execute(
            "UPDATE bindings SET title='Random' WHERE thread_id='random'"
        )
        database._conn.commit()
        database.set_runtime_config(
            "local-only", "model", "untouched", message_id="local"
        )
        definitions = {
            name: {"model": value.model, "effort": value.effort}
            for name, value in presets().items()
        }
        result = initialize_profiles(
            database, definitions, level="advanced", revision="once"
        )
        assert result["scopes"] == 3
        assert database.get_setting("runtime:thread-1:model") == "gpt-6.2-astra"
        assert database.get_setting("runtime:random:service_tier") == "priority"
        assert database.get_setting("runtime:local-only:model") == "untouched"
        database.set_runtime_configs(
            profile_changes("thread-1", "研究", "default", presets()),
            message_id="later-user",
        )
        assert initialize_profiles(
            database, definitions, level="advanced", revision="once"
        ) == {"unchanged": True}
        assert database.get_setting("runtime:thread-1:model") == "gpt-6.2-sol"
    finally:
        database.close()
