"""Opt-in metadata acceptance: real directories + ephemeral setting RPC, zero model turns."""

import os

import pytest

from codex_feishu_bridge.codex_client import CodexAppServer
from codex_feishu_bridge.config import DEFAULT_CONFIG_DIR, load_config
from test_bridge_model_menu import buttons, click, page, pick, route
from test_profile_commands import service_for_test
from test_service import bind_thread, incoming


@pytest.mark.skipif(
    os.environ.get("YINSHI_LIVE_MODEL_MENU_METADATA") != "1",
    reason="opt-in external directories; no inference",
)
async def test_real_catalogs_and_both_directions_without_model_turns(tmp_path):
    s, d, _, g = service_for_test(tmp_path)
    cfg = load_config(DEFAULT_CONFIG_DIR / "config.toml")
    s.claude.key_path = cfg.config_path.parent / "claude-4router.key"
    async with CodexAppServer(cfg.codex_bin, request_timeout=15) as c:
        native = await c.list_models()
        assert native
        start = await c.start_thread(
            cwd=str(tmp_path),
            ephemeral=True,
            approval_policy="on-request",
            sandbox="read-only",
            model=native[0]["model"],
        )
        thread = start["id"]
        calls = []
        update = c.update_thread_settings

        async def verified(*args, **kw):
            result = await update(*args, **kw)
            calls.append((args[0], kw["model"]))
            return result

        c.update_thread_settings = verified

        async def no_turn(*a, **kw):
            pytest.fail("a menu command started model inference")

        c.start_turn = s.claude.run = no_turn
        s.codex = c
        chat = "oc_live_metadata"
        bind_thread(d, thread_id=thread, chat_id=chat)
        try:
            await route(s, d, incoming("menu", chat_id=chat, text="/model"))
            main = g.cards[-1]["card"]
            assert len(buttons(main)) == 5
            cgroup = next(
                b["value"]
                for b in buttons(main)
                if b["value"]["model"] == "claude-opus-5-5"
            )
            await route(s, d, click("card-1", page(1), "expand", chat))
            assert len(buttons(g.patches[-1]["card"])) > 5
            await route(s, d, click("card-1", cgroup, "claude", chat))
            await route(s, d, incoming("status", chat_id=chat, text="/status"))
            assert s._group(thread) == "c" and "Opus 5.5" in g.texts[-1]["text"]
            await route(
                s, d, click("card-1", pick("codex", native[0]["model"]), "native", chat)
            )
            assert s._group(thread) == "codex" and calls == [
                (thread, native[0]["model"])
            ]
            ids, note = await s._picker_source("c")
            assert not note and "claude-sonnet-5-5" in ids
        finally:
            await c.unsubscribe_thread(thread)
            d.close()
