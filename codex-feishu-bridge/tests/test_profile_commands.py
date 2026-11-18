from __future__ import annotations


import pytest

from codex_feishu_bridge.service import BridgeService
from test_service import (
    FakeCodex,
    FakeGateway,
    bind_thread,
    incoming,
    make_config,
    stage,
)
from test_model_profiles import models, presets


@pytest.mark.asyncio
async def test_replayed_global_command_cannot_overwrite_later_user_choice(tmp_path):
    service, database, codex, gateway = service_for_test(tmp_path)
    try:
        first = stage(database, incoming("global-once", text="全部高级"))
        await service._route_incoming(first)
        original_reply = gateway.texts[-1]["text"]
        await service._route_incoming(stage(database, incoming("later-choice", text="默认")))
        applied_count = len(codex.setting_updates)
        await service._try_model_profile_command(first)
        assert service._runtime_settings("thread-1").model == "gpt-6.2-sol"
        assert len(codex.setting_updates) == applied_count
        assert gateway.texts[-1]["text"] == original_reply
    finally:
        database.close()


def service_for_test(tmp_path):
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    from codex_feishu_bridge.db import BridgeDB

    database = BridgeDB(config.database_path)
    codex = FakeCodex()
    codex.models = models()
    gateway = FakeGateway()
    service = BridgeService(config, database, codex, gateway)
    database.set_runtime_configs(
        [
            ("model_profiles", level, preset.encode())
            for level, preset in presets().items()
        ],
        message_id="setup",
    )
    bind_thread(database)
    bind_thread(database, thread_id="random-thread", chat_id="oc_random")
    database._conn.execute(
        "UPDATE bindings SET title='Random Japan' WHERE thread_id='random-thread'"
    )
    database._conn.commit()
    return service, database, codex, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expected",
    [
        ("默认", "gpt-6.2-sol"),
        ("高级", "gpt-6.2-astra"),
        ("全部默认", "gpt-6.2-sol"),
        ("全部高级", "gpt-6.2-astra"),
    ],
)
async def test_individual_and_global_switches_preserve_permissions(
    tmp_path, command, expected
):
    service, database, codex, gateway = service_for_test(tmp_path)
    database.set_runtime_config(
        "thread-1", "sandbox", "read-only", message_id="permission"
    )
    try:
        await service._route_incoming(stage(database, incoming("switch", text=command)))
        settings = service._runtime_settings("thread-1")
        assert settings.model == expected
        assert settings.service_tier is None
        assert settings.sandbox == "read-only"
        random = service._runtime_settings("random-thread")
        assert (random.model, random.effort, random.service_tier) == (
            "gpt-6.2-astra",
            "max",
            "priority",
        )
        assert database.inbox_state("switch") == "done"
        assert not codex.started_threads and not codex.interrupted
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "默认",
        "全部默认",
        "!速度 普通",
        "/fast",
        "/model gpt-6.2-sol xhigh",
        "!推理 xhigh",
        "!配置重置",
    ],
)
async def test_random_cannot_fall_back_to_normal_model_or_speed(tmp_path, command):
    service, database, codex, gateway = service_for_test(tmp_path)
    try:
        message = incoming("random-switch", chat_id="oc_random", text=command)
        await service._route_incoming(stage(database, message))
        settings = service._runtime_settings("random-thread")
        assert (settings.model, settings.effort, settings.service_tier) == (
            "gpt-6.2-astra",
            "max",
            "priority",
        )
        assert not codex.started_threads
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/fast", "!速度 快速"])
async def test_normal_presets_cannot_accidentally_enable_fast(tmp_path, command):
    service, database, codex, gateway = service_for_test(tmp_path)
    try:
        await service._route_incoming(
            stage(database, incoming("no-fast", text=command))
        )
        assert service._runtime_settings("thread-1").service_tier is None
        assert "未开启Fast" in gateway.texts[-1]["text"]
    finally:
        database.close()


@pytest.mark.asyncio
async def test_definition_updates_linked_scopes_and_rejects_unknown_model(tmp_path):
    service, database, codex, gateway = service_for_test(tmp_path)
    codex.models.append(
        {
            "model": "next-sol",
            "id": "next-sol",
            "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            "serviceTiers": [],
        }
    )
    try:
        await service._route_incoming(
            stage(database, incoming("definition", text="默认模型 next-sol xhigh"))
        )
        assert service._runtime_settings("thread-1").model == "next-sol"
        assert service._runtime_settings("random-thread").model == "gpt-6.2-astra"
        await service._route_incoming(
            stage(
                database, incoming("bad-definition", text="高级模型 missing-model max")
            )
        )
        assert "未修改" in gateway.texts[-1]["text"]
        assert service._runtime_settings("random-thread").model == "gpt-6.2-astra"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_global_commands_are_owner_only_and_gate_fails_closed(tmp_path):
    service, database, codex, gateway = service_for_test(tmp_path)
    try:
        item = stage(
            database, incoming("not-owner", open_id="collaborator", text="全部高级")
        )
        assert await service._try_model_profile_command(item)
        assert "管理员" in gateway.texts[-1]["text"]
        assert service._runtime_settings("thread-1").model == "gpt-6.2-sol"
        service._runtime_compatibility_error = "new CLI unverified"
        await service._route_incoming(stage(database, incoming("gated", text="高级")))
        assert "门禁" in gateway.texts[-1]["text"]
        assert service._runtime_settings("thread-1").model == "gpt-6.2-sol"
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,expected,effort,tier",
    [
        ("新研究", "gpt-6.2-sol", "xhigh", None),
        ("Random new", "gpt-6.2-astra", "max", "priority"),
    ],
)
async def test_new_conversations_use_default_unless_random(
    tmp_path, title, expected, effort, tier
):
    service, database, codex, gateway = service_for_test(tmp_path)
    try:
        binding = await service.create_new_conversation(
            title, tmp_path, inbox_message_id="new"
        )
        settings = service._runtime_settings(binding.thread_id)
        assert (settings.model, settings.effort, settings.service_tier) == (
            expected,
            effort,
            tier,
        )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_manual_model_selection_detaches_preset_and_status_reports_profile(
    tmp_path,
):
    service, database, codex, gateway = service_for_test(tmp_path)
    try:
        await service._route_incoming(stage(database, incoming("high", text="高级")))
        assert "高级" in await service._format_runtime_status(
            "thread-1", database.get_binding_by_thread("thread-1")
        )
        await service._route_incoming(
            stage(database, incoming("manual", text="/model gpt-6.2-sol xhigh"))
        )
        assert service._runtime_settings("thread-1").model == "gpt-6.2-sol"
        assert database.get_setting("runtime:thread-1:model_profile") == ""
    finally:
        database.close()
