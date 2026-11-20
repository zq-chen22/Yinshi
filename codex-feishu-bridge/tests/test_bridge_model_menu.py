from types import SimpleNamespace as N

import pytest

from codex_feishu_bridge.feishu import _normalize_card_action
from codex_feishu_bridge.model_menu import Choice, model_card
from codex_feishu_bridge.models import ActiveTurn
from test_model_groups import job_for
from test_profile_commands import service_for_test
from test_service import incoming, stage


def setup(tmp):
    s, d, c, g = service_for_test(tmp)
    c.models += [
        {
            "id": f"gpt-extra-{i}",
            "model": f"gpt-extra-{i}",
            "defaultReasoningEffort": "low",
            "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            "serviceTiers": [],
        }
        for i in range(12)
    ]
    s.claude.available = lambda: True

    async def directory():
        return [
            "claude-opus-5-5",
            "claude-fable-5-2",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
        ]

    s.claude.list_models = directory
    return s, d, c, g


def buttons(card, action="pick"):
    return [
        b
        for e in card["elements"]
        for b in e.get("actions", [])
        if b["value"]["action"] == action
    ]


def click(card, value, event_id, chat="oc_thread", owner="ou_conversation_owner"):
    event = N(
        header=N(
            app_id="cli_conversation",
            event_id=event_id,
            create_time="1700000000999",
            tenant_key="tenant-test",
        ),
        event=N(
            action=N(value=value),
            context=N(open_chat_id=chat, open_message_id=card),
            operator=N(
                open_id=owner,
                user_id="u_owner",
                union_id="on_owner",
                tenant_key="tenant-test",
            ),
        ),
    )
    return _normalize_card_action(event, "conversation", "cli_conversation")


def pick(group, model):
    return {"kind": "bridge_model", "action": "pick", "group": group, "model": model}


def page(n):
    return {"kind": "bridge_model", "action": "page", "page": n}


async def route(s, d, m):
    # Preserve the same app-scoped sender identity as the normal text fixture.
    if m.message_type == "card_action":
        seed = incoming("s")
        m.sender_union_id = seed.sender_union_id
        m.sender_user_id = seed.sender_user_id
    await s._route_incoming(stage(d, m))


async def test_five_quiet_buttons_navigation_is_patched_and_read_only(tmp_path):
    s, d, c, g = setup(tmp_path)
    await route(s, d, incoming("menu", text="/model"))
    card = g.cards[-1]["card"]
    assert {b["text"]["content"] for b in buttons(card)} == {
        "GPT-6.2 Sol",
        "GPT-6.2 Astra",
        "Opus 5.5",
        "Fable 5.2",
        "Sonnet 5.5",
    }
    assert "purple" in str(card) and "blue" in str(card)
    assert not d.runtime_config_history("thread-1")
    await route(s, d, click("card-1", page(1), "expand"))
    assert len(g.cards) == 1 and len(buttons(g.patches[-1]["card"])) == 13
    await route(s, d, click("card-1", page(2), "next"))
    assert any(
        b["value"].get("model") == "gpt-extra-11"
        for b in buttons(g.patches[-1]["card"])
    )
    await route(s, d, click("card-1", page(0), "collapse"))
    assert len(buttons(g.patches[-1]["card"])) == 5 and not c.setting_updates
    assert not d.runtime_config_history("thread-1") and not c.started_threads
    d.close()


async def test_click_cross_group_once_no_effort_dialog_and_actual_dispatch(tmp_path):
    s, d, c, g = setup(tmp_path)
    await route(s, d, incoming("menu", text="/model"))
    b = next(
        x
        for x in buttons(g.cards[-1]["card"])
        if x["value"]["model"] == "claude-fable-5-2"
    )
    await route(s, d, click("card-1", b["value"], "select-fable"))
    assert "Fable 5.2" in g.texts[-1]["text"] and len(g.cards) == 1
    assert s._group("thread-1") == "c" and not c.setting_updates
    seen = []

    async def native(turn, preset, blocks, **kw):
        seen.append((preset.model, preset.effort))
        return "synthetic receipt"

    s.claude.run = native
    await s._execute_job("thread-1", job_for(s, d, tmp_path, "q", "test"))
    assert seen == [("claude-fable-5-2", "high")] and not c.started_threads
    await route(s, d, incoming("state", text="/status"))
    assert all(
        w in g.texts[-1]["text"] for w in ["C 组", "Fable 5.2", "high", "不适用"]
    )
    assert c.rate_limit_reads == 0
    await route(s, d, click("card-1", pick("codex", "gpt-6.2-sol"), "back"))
    assert s._group("thread-1") == "codex" and len(g.cards) == 1
    assert c.setting_updates[-1]["model"] == "gpt-6.2-sol"
    # An event recovered again after a later choice cannot switch back.
    from codex_feishu_bridge.models import InboxItem

    replay = click("card-1", b["value"], "select-fable")
    await s._try_bridge_menu(InboxItem(replay))
    assert s._group("thread-1") == "codex"
    d.close()


async def test_custom_claude_turn_and_keyword_resets_override(tmp_path):
    s, d, c, g = setup(tmp_path)
    await route(s, d, incoming("custom", text="/model c:claude-sonnet-5-5"))
    assert (
        s._claude_profile("thread-1").model == "claude-sonnet-5-5"
        and s._claude_profile("thread-1").effort == ""
    )
    job = job_for(s, d, tmp_path, "q", "test")
    called = []

    async def backend(turn, preset, blocks, **kw):
        called.append((preset.model, preset.effort))
        return "OK"

    s.claude.run = backend
    await s._execute_job("thread-1", job)
    assert called == [("claude-sonnet-5-5", "")]
    await route(s, d, incoming("switch", text="C组高级"))
    assert s._claude_profile("thread-1").model == "claude-fable-5-2"
    await route(s, d, incoming("bad", text="/model c:claude-fictional-8"))
    assert (
        "未切换" in g.texts[-1]["text"]
        and s._claude_profile("thread-1").model == "claude-fable-5-2"
    )
    d.close()


async def test_switch_while_preparing_attachment_only_affects_next_task(tmp_path):
    s, d, c, g = setup(tmp_path)
    await route(s, d, incoming("select", text="/model c:claude-fable-5-2"))
    prepare = s.artifacts.prepare_inputs
    seen = []

    async def midflight(m):
        await route(s, d, incoming("next", text="/model gpt-6.2-sol"))
        return await prepare(m)

    async def native(turn, preset, blocks, **kw):
        seen.append((preset.model, preset.effort))
        return "OK"

    s.artifacts.prepare_inputs = midflight
    s.claude.run = native
    job = job_for(s, d, tmp_path, "inflight", "test")
    s._thread_worker_jobs["thread-1"] = job
    await s._execute_job("thread-1", job)
    assert seen == [("claude-fable-5-2", "high")]
    assert s._group("thread-1") == "codex" and not c.setting_updates
    d.close()


async def test_unavailable_sources_do_not_block_other_group_or_status(tmp_path):
    s, d, c, g = setup(tmp_path)
    s._codex_online = False

    async def broken():
        raise RuntimeError("private details")

    async def forbidden():
        pytest.fail("touched unavailable Codex")

    s.claude.list_models = broken
    c.list_models = forbidden
    c.read_rate_limits = forbidden
    await route(s, d, incoming("menu", text="/model"))
    cs = buttons(g.cards[-1]["card"])
    assert len(cs) == 2 and all(x["value"]["group"] == "c" for x in cs)
    await route(s, d, click("card-1", cs[0]["value"], "c"))
    await route(s, d, incoming("status", text="/status"))
    assert "C 组" in g.texts[-1]["text"] and "private" not in str(g.cards)
    await route(s, d, incoming("bad", text="/model gpt-6.2-sol"))
    assert "未切换" in g.texts[-1]["text"] and s._group("thread-1") == "c"
    d.close()


async def test_private_card_and_forwarded_or_typed_callback(tmp_path):
    s, d, c, g = setup(tmp_path)
    await route(s, d, incoming("dm", text="/model", chat_type="p2p", chat_id="oc_dm"))
    callback = pick("c", "claude-opus-5-5")
    await route(s, d, click("card-1", callback, "foreign", chat="oc_thread"))
    assert "不匹配" in g.texts[-1]["text"] and s._group("thread-1") == "codex"
    await route(
        s, d, incoming("spoof", text="/bridge-model-menu pick c claude-opus-5-5")
    )
    assert s._group("thread-1") == "codex"
    await route(s, d, click("card-1", callback, "legit", chat="oc_dm"))
    assert s._group("admin") == "c" and s._group("thread-1") == "codex"
    d.close()


async def test_choose_preserves_supported_user_settings_not_capability_flags(tmp_path):
    s, d, c, g = setup(tmp_path)
    c.models[1]["supportedReasoningEfforts"].append({"reasoningEffort": "xhigh"})
    old = s._runtime_settings("thread-1")
    await route(s, d, incoming("astra", text="/model gpt-6.2-astra"))
    new = s._runtime_settings("thread-1")
    assert new.effort == old.effort == "xhigh" and new.service_tier is None
    assert (new.sandbox, new.approval_policy) == (old.sandbox, old.approval_policy)
    await route(s, d, incoming("tiny", text="/model gpt-extra-0"))
    assert (
        s._runtime_settings("thread-1").effort is None
        and "模型默认值" in g.texts[-1]["text"]
    )
    assert len(g.cards) == 0
    d.close()


async def test_random_limits_only_codex_and_active_turn_not_interrupted(tmp_path):
    s, d, c, g = setup(tmp_path)
    s._register_active(ActiveTurn("random-thread", "existing", "oc_random"))
    await route(s, d, incoming("a", chat_id="oc_random", text="/model"))
    assert [
        x["value"]["model"]
        for x in buttons(g.cards[-1]["card"])
        if x["value"]["group"] == "codex"
    ] == ["gpt-6.2-astra"]
    await route(
        s, d, incoming("b", chat_id="oc_random", text="/model c:claude-sonnet-5-5")
    )
    await route(s, d, incoming("c", chat_id="oc_random", text="/model gpt-6.2-sol"))
    assert s._group("random-thread") == "c" and "固定" in g.texts[-1]["text"]
    await route(s, d, incoming("d", chat_id="oc_random", text="/model gpt-6.2-astra"))
    assert (
        s._runtime_settings("random-thread").service_tier == "priority"
        and s._group("random-thread") == "codex"
    )
    assert not c.interrupted and not c.setting_updates
    d.close()


@pytest.mark.parametrize(
    "bad",
    [
        page(True),
        page(-1),
        page(10001),
        pick("c", "claude evil"),
        pick("../", "value"),
        {"kind": "bridge_model", "action": "unknown"},
    ],
)
def test_bad_callback_schema(bad):
    assert click("card", bad, "ev") is None


def test_selected_uncommon_model_stays_on_first_screen():
    options = [Choice("codex", f"gpt-{i}", f"Model {i}") for i in range(12)]
    current = options[-1].key
    first = model_card(options, [o.key for o in options[:5]], current)
    assert len(buttons(first)) == 5
    assert [
        b["value"]["model"] for b in buttons(first) if b.get("type") == "primary"
    ] == [current[1]]
