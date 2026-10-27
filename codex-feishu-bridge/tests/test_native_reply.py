from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from codex_feishu_bridge.db import BridgeDB, SCHEMA
from codex_feishu_bridge.feishu import FeishuAPIError, FeishuGateway
from codex_feishu_bridge.models import ActiveTurn, OutboxItem, TurnJob
from codex_feishu_bridge.service import BridgeService
from test_service import FakeCodex, FakeGateway, bind_thread, incoming, make_config, stage


def gateway_fixture(*, deleted=False, parent_chat="oc_group", reply_error=None):
    calls = {"get": [], "reply": [], "create": []}

    def response(data, code=0):
        return NS(data=data, code=code, msg="message is recalled" if code else "ok",
                  success=lambda: code == 0, get_log_id=lambda: "test-log")

    async def get(request):
        calls["get"].append(request)
        return response(NS(items=[NS(chat_id=parent_chat, deleted=deleted)]))

    async def reply(request):
        calls["reply"].append(request)
        if isinstance(reply_error, BaseException):
            raise reply_error
        return response(NS(message_id="om_reply"), reply_error or 0)

    async def create(request):
        calls["create"].append(request)
        return response(NS(message_id="om_plain"))

    gateway = FeishuGateway.__new__(FeishuGateway)
    gateway._clients = {"conversation": NS(im=NS(v1=NS(message=NS(aget=get, areply=reply, acreate=create))))}
    return gateway, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,content", [("text", {"text": "answer"}), ("interactive", {"elements": []}), ("image", {"image_key": "img_test"})])
async def test_native_reply_is_a_reference_with_stable_retry_uuid(kind, content):
    gateway, calls = gateway_fixture()
    for _ in range(2):
        assert await gateway.send_message("conversation", "oc_group", kind, content,
                    reply_to_message_id="om_question", idempotency_key="same-outbox-item") == "om_reply"
    assert len(calls["get"]) == 1
    assert not calls["create"]
    first, retry = calls["reply"]
    assert first.message_id == retry.message_id == "om_question"
    assert first.request_body.reply_in_thread is False
    assert json.loads(first.request_body.content) == content
    assert first.request_body.uuid == retry.request_body.uuid


@pytest.mark.asyncio
async def test_deleted_source_falls_back_without_losing_answer():
    gateway, calls = gateway_fixture(deleted=True)
    assert await gateway.send_text("conversation", "oc_group", "answer", reply_to_message_id="om_question") == "om_plain"
    assert not calls["reply"]
    assert json.loads(calls["create"][0].request_body.content) == {"text": "answer"}


@pytest.mark.asyncio
async def test_source_withdrawn_between_validation_and_send_falls_back():
    gateway, calls = gateway_fixture(reply_error=230011)
    assert await gateway.send_card("conversation", "oc_group", {"elements": []}, reply_to_message_id="om_question") == "om_plain"
    assert len(calls["reply"]) == len(calls["create"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("unknown send outcome"), 2200])
async def test_ambiguous_or_transient_error_does_not_send_duplicate_plain_message(error):
    gateway, calls = gateway_fixture(reply_error=error)
    with pytest.raises((TimeoutError, FeishuAPIError)):
        await gateway.send_text("conversation", "oc_group", "answer", reply_to_message_id="om_question")
    assert not calls["create"]


@pytest.mark.asyncio
async def test_reply_target_in_another_conversation_cannot_receive_answer():
    gateway, calls = gateway_fixture(parent_chat="oc_other")
    with pytest.raises(FeishuAPIError, match="destination conversation"):
        await gateway.send_text("conversation", "oc_group", "private answer", reply_to_message_id="om_question")
    assert not calls["reply"] and not calls["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "card:synthetic-action"])
async def test_notifications_and_synthetic_controls_keep_normal_send(source):
    gateway, calls = gateway_fixture()
    await gateway.send_text("conversation", "oc_group", "notice", reply_to_message_id=source)
    assert not calls["get"] and not calls["reply"]
    assert len(calls["create"]) == 1


def test_old_database_migrates_and_preserves_quote_after_restart(tmp_path):
    path = tmp_path / "bridge.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA.replace("    reply_to_message_id TEXT,\n", ""))
    db = BridgeDB(path)
    item = OutboxItem(outbox_key="delivery", app_role="conversation", receive_id="oc_group",
                     receive_id_type="chat_id", msg_type="text", content={"text": "answer"},
                     reply_to_message_id="om_first")
    assert db.enqueue_outbox(item)
    item.reply_to_message_id = "om_wrong_duplicate"
    assert not db.enqueue_outbox(item)
    db.close()
    db = BridgeDB(path)
    try:
        claimed = db.claim_outbox("after-restart")
        assert claimed and claimed.reply_to_message_id == "om_first"
        assert claimed.content == {"text": "answer"}
    finally:
        db.close()


def add_job(db, *, source="om_first", turn="turn-1", chat="oc_thread"):
    db.upsert_turn_job(TurnJob(message_id=source, thread_id="thread-1", turn_id=turn,
                             app_role="conversation", chat_id=chat, progress_message_id=None,
                             state="accepted"))


def test_all_long_answer_parts_and_artifacts_keep_original_quote(tmp_path):
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    try:
        service = BridgeService(config, db, FakeCodex(), FakeGateway())
        add_job(db)
        artifact = config.outbox_dir / "result.txt"
        artifact.write_text("generated result")
        service._enqueue_outbound_result(app_role="conversation", chat_id="oc_thread",
                text="界" * 100_001, base_key="final:turn-1", thread_id="thread-1", turn_id="turn-1",
                artifact_paths=[artifact])
        seen = []
        while (item := db.claim_outbox("delivery")) is not None:
            seen.append(item)
            assert item.reply_to_message_id == "om_first"
            db.complete_outbox(item.outbox_key)
        text_parts = [p for p in seen if p.msg_type == "text"]
        assert len(text_parts) > 1
        assert all(p.turn_id is None for p in text_parts[:-1])
        assert text_parts[-1].turn_id == "turn-1"
        assert seen[-1].msg_type == "local_file"
        assert not db.is_turn_synced("thread-1", "turn-1")
    finally:
        db.close()


def test_same_turn_id_cannot_resolve_source_in_another_chat(tmp_path):
    db = BridgeDB(tmp_path / "bridge.sqlite")
    try:
        add_job(db)
        assert db.reply_target_for_turn("turn-1", "oc_thread", "conversation") == "om_first"
        assert db.reply_target_for_turn("turn-1", "oc_other", "conversation") is None
        assert db.reply_target_for_turn("turn-1", "oc_thread", "admin") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_queued_users_receive_cards_anchored_to_each_own_question(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)
    bind_thread(db)

    async def wait_only(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_thread_worker", wait_only)
    try:
        for index, sender in enumerate(("ou_first", "ou_second")):
            item = stage(db, incoming(f"om_question{index}", open_id=sender))
            await service._queue_thread_message(item, db.get_binding_by_thread("thread-1"))
        assert [c["reply_to_message_id"] for c in gateway.cards] == ["om_question0", "om_question1"]
    finally:
        for task in service._thread_workers.values():
            task.cancel()
        await asyncio.gather(*service._thread_workers.values(), return_exceptions=True)
        db.close()


@pytest.mark.asyncio
async def test_durable_outbox_passes_stored_quote_to_transport(tmp_path):
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)
    original = gateway.send_text

    async def send(*args, **kwargs):
        result = await original(*args, **kwargs)
        service._stop.set()
        return result

    gateway.send_text = send
    try:
        db.enqueue_outbox(OutboxItem(outbox_key="final", app_role="conversation", receive_id="oc_thread",
                         receive_id_type="chat_id", msg_type="text", content={"text": "answer"},
                         reply_to_message_id="om_original"))
        await asyncio.wait_for(service._outbox_loop(), timeout=2)
        assert gateway.texts[0]["reply_to_message_id"] == "om_original"
        assert db.outbox_counts() == {"done": 1}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_private_chat_progress_also_quotes_its_question(tmp_path):
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)
    try:
        item = stage(db, incoming("om_private", chat_id="oc_private", chat_type="p2p"))
        await service._queue_admin_message(item)
        assert gateway.cards[0]["reply_to_message_id"] == "om_private"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_steering_ack_and_final_use_their_correct_sources(tmp_path):
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)
    bind_thread(db)
    add_job(db)
    active = ActiveTurn(thread_id="thread-1", turn_id="turn-1", chat_id="oc_thread",
                        app_role="conversation", progress_message_id=None,
                        started_monotonic=0, final_text="final answer")
    try:
        item = stage(db, incoming("om_followup", text="supplement"))
        assert await service._steer(item, active)
        assert gateway.texts[-1]["reply_to_message_id"] == "om_followup"
        await service._finalize_turn(active, {"status": "completed", "items": []})
        final = db.claim_outbox("final-delivery")
        assert final and final.reply_to_message_id == "om_first"
    finally:
        db.close()
