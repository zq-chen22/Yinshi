from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from codex_feishu_bridge.config import BridgeConfig, FeishuConfig
from codex_feishu_bridge.db import BridgeDB
from codex_feishu_bridge.models import (
    ActiveTurn,
    AppRole,
    Attachment,
    InboxItem,
    IncomingMessage,
    PendingApproval,
    ThreadSummary,
    TurnJob,
)
from codex_feishu_bridge.service import (
    BridgeService,
    RuntimeSettings,
    ScheduledMessage,
    generate_pairing_code,
)


class FakeCodex:
    def __init__(self) -> None:
        self.notification_handlers: list[Any] = []
        self.server_request_handler: Any = None
        self.steered: list[dict[str, Any]] = []
        self.responses: list[tuple[str, dict[str, Any]]] = []
        self.pending_requests: dict[str, dict[str, Any]] = {}
        self.errors: list[tuple[str, int, str]] = []
        self.setting_updates: list[dict[str, Any]] = []
        self.started_threads: list[dict[str, Any]] = []
        self.archived_threads: list[str] = []
        self.unsubscribed_threads: list[str] = []
        self.models = [
            {
                "id": "gpt-test",
                "model": "gpt-test",
                "displayName": "GPT Test",
                "isDefault": True,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "high"},
                ],
                "serviceTiers": [{"id": "priority", "name": "Fast"}],
            }
        ]

    def add_notification_handler(self, handler: Any) -> None:
        self.notification_handlers.append(handler)

    def set_server_request_handler(self, handler: Any) -> None:
        self.server_request_handler = handler

    async def list_threads(self, **_: Any) -> list[ThreadSummary]:
        return []

    async def read_thread(self, thread_id: str, **_: Any) -> dict[str, Any]:
        return {"id": thread_id, "status": {"type": "idle"}, "turns": [], "path": None}

    async def list_models(self) -> list[dict[str, Any]]:
        return self.models

    async def update_thread_settings(self, thread_id: str, **kwargs: Any) -> dict[str, Any]:
        value = {"thread_id": thread_id, **kwargs}
        self.setting_updates.append(value)
        return value

    async def start_thread(self, **kwargs: Any) -> dict[str, Any]:
        self.started_threads.append(kwargs)
        return {"id": f"thread-probe-{len(self.started_threads)}"}

    async def archive_thread(self, thread_id: str) -> None:
        self.archived_threads.append(thread_id)

    async def unsubscribe_thread(self, thread_id: str) -> None:
        self.unsubscribed_threads.append(thread_id)

    async def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        inputs: list[dict[str, Any]],
        *,
        client_message_id: str,
    ) -> None:
        self.steered.append(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "inputs": inputs,
                "client_message_id": client_message_id,
            }
        )

    def pending_server_request(self, rpc_id: str) -> dict[str, Any] | None:
        return self.pending_requests.get(str(rpc_id))

    async def respond_server_request(self, rpc_id: str, result: dict[str, Any]) -> None:
        self.responses.append((str(rpc_id), result))
        self.pending_requests.pop(str(rpc_id), None)

    async def respond_server_error(self, rpc_id: str, code: int, message: str) -> None:
        self.errors.append((str(rpc_id), code, message))


class FastCompletingCodex(FakeCodex):
    def __init__(self) -> None:
        super().__init__()
        self.thread_names: list[tuple[str, str]] = []
        self.archived_threads: list[str] = []

    async def start_thread(self, **_: Any) -> dict[str, Any]:
        return {"id": "thread-fast-scratch"}

    async def set_thread_name(self, thread_id: str, name: str) -> None:
        self.thread_names.append((thread_id, name))

    async def archive_thread(self, thread_id: str) -> None:
        self.archived_threads.append(thread_id)

    async def start_turn(self, thread_id: str, inputs: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        turn = {
            "id": "turn-fast",
            "status": "completed",
            "items": [
                {
                    "type": "agentMessage",
                    "id": "item-final",
                    "phase": "final_answer",
                    "text": "快速完成",
                }
            ],
        }
        for handler in self.notification_handlers:
            await handler(
                {"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "turn-fast"}}}
            )
            await handler(
                {"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}}
            )
        return turn


class StaleInProgressCodex(FakeCodex):
    async def read_thread(self, thread_id: str, **_: Any) -> dict[str, Any]:
        return {
            "id": thread_id,
            "status": {"type": "notLoaded"},
            "path": None,
            "turns": [{"id": "turn-stale", "status": "inProgress", "items": []}],
        }


class ThreadStartCrashCodex(FakeCodex):
    def __init__(self, db: BridgeDB, message_id: str) -> None:
        super().__init__()
        self.db = db
        self.message_id = message_id
        self.state_at_rpc: str | None = None

    async def start_thread(self, **_: Any) -> dict[str, Any]:
        self.state_at_rpc = self.db.inbox_state(self.message_id)
        raise ConnectionError("connection lost across thread/start")


class TurnStartCrashCodex(FakeCodex):
    def __init__(self, db: BridgeDB, message_id: str) -> None:
        super().__init__()
        self.db = db
        self.message_id = message_id
        self.state_at_rpc: str | None = None

    async def start_turn(self, *_: Any, **__: Any) -> dict[str, Any]:
        self.state_at_rpc = self.db.inbox_state(self.message_id)
        raise ConnectionError("connection lost across turn/start")


class StaticThreadsCodex(FakeCodex):
    def __init__(self, threads: list[ThreadSummary]) -> None:
        super().__init__()
        self.threads = threads

    async def list_threads(self, **_: Any) -> list[ThreadSummary]:
        return self.threads


class ThreadHistoryCodex(FakeCodex):
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        super().__init__()
        self.turns = turns

    async def read_thread(self, thread_id: str, **_: Any) -> dict[str, Any]:
        return {"id": thread_id, "status": {"type": "idle"}, "turns": self.turns}


class FakeGateway:
    def __init__(self, *, configured_roles: set[AppRole] | None = None) -> None:
        self.configured_roles = configured_roles or set()
        self.texts: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.history: list[IncomingMessage] = []
        self.history_calls: list[dict[str, Any]] = []
        self.downloads: list[tuple[AppRole, Attachment]] = []
        self.chat_updates: list[dict[str, Any]] = []

    def configured(self, role: AppRole) -> bool:
        return role in self.configured_roles

    async def send_text(
        self,
        role: AppRole,
        receive_id: str,
        text: str,
        **kwargs: Any,
    ) -> str:
        self.texts.append(
            {"role": role, "receive_id": receive_id, "text": text, **kwargs}
        )
        return f"text-{len(self.texts)}"

    async def send_card(
        self,
        role: AppRole,
        receive_id: str,
        card: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        self.cards.append(
            {"role": role, "receive_id": receive_id, "card": card, **kwargs}
        )
        return f"card-{len(self.cards)}"

    async def download_attachment(
        self, role: AppRole, attachment: Attachment
    ) -> tuple[bytes, str]:
        self.downloads.append((role, attachment))
        return b"staged attachment", attachment.name

    async def find_conversation_chat(
        self, thread_id: str, owner_open_id: str
    ) -> tuple[str, str] | None:
        return None

    async def create_conversation_chat(
        self,
        thread_id: str,
        title: str,
        owner_open_id: str,
        cwd: str,
        created_at: int,
    ) -> tuple[str, str]:
        return f"oc-{thread_id}", title

    async def update_conversation_chat_description(
        self, chat_id: str, thread_id: str, cwd: str, created_at: int
    ) -> None:
        self.chat_updates.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "cwd": cwd,
                "created_at": created_at,
            }
        )

    async def list_chat_messages(
        self,
        role: AppRole,
        chat_id: str,
        *,
        start_time_seconds: int,
        end_time_seconds: int,
    ) -> list[IncomingMessage]:
        self.history_calls.append(
            {
                "role": role,
                "chat_id": chat_id,
                "start_time_seconds": start_time_seconds,
                "end_time_seconds": end_time_seconds,
            }
        )
        return list(self.history)


def make_config(tmp_path: Path, **feishu_values: str) -> BridgeConfig:
    state = tmp_path / "state"
    config = BridgeConfig(
        config_path=tmp_path / "config.toml",
        state_dir=state,
        database_path=state / "bridge.sqlite",
        inbox_dir=state / "inbox",
        outbox_dir=state / "outbox",
        admin_scratch_dir=state / "admin-scratch",
        managed_workspaces_dir=state / "workspaces",
        allowed_workspace_roots=[tmp_path],
        feishu=FeishuConfig(**feishu_values),
    )
    config.prepare_dirs()
    return config


def incoming(
    message_id: str,
    *,
    role: AppRole = "conversation",
    chat_id: str = "oc_thread",
    open_id: str = "ou_conversation_owner",
    text: str = "继续",
    tenant_key: str = "tenant-yinshi",
    create_time_ms: int = 1_000,
    chat_type: str | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type or ("group" if role == "conversation" else "p2p"),
        app_role=role,
        sender_open_id=open_id,
        sender_user_id=None,
        sender_union_id="on_same_human",
        text=text,
        message_type="text",
        create_time_ms=create_time_ms,
        tenant_key=tenant_key,
        app_id=f"cli_{role}",
        sender_type="user",
    )


def stage(db: BridgeDB, message: IncomingMessage) -> InboxItem:
    assert db.enqueue_incoming(message) is True
    claimed = db.claim_incoming("test-worker")
    assert claimed is not None
    assert claimed.message.message_id == message.message_id
    return claimed


def bind_thread(db: BridgeDB, *, thread_id: str = "thread-1", chat_id: str = "oc_thread") -> None:
    db.upsert_thread(
        ThreadSummary(
            thread_id=thread_id,
            name="受控对话",
            preview="",
            cwd="/home/tester",
            created_at=1,
            updated_at=2,
            source_kind="cli",
        )
    )
    db.bind_chat(thread_id, chat_id)


@pytest.mark.asyncio
async def test_pairing_keeps_app_scoped_open_ids_for_the_same_human(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    code, _ = generate_pairing_code(db, ttl_seconds=300)

    try:
        admin = incoming(
            "om-pair-admin",
            role="admin",
            chat_id="oc_admin_p2p",
            open_id="ou_admin_app_scope",
            text=f"配对 {code}",
        )
        conversation = incoming(
            "om-pair-conversation",
            chat_id="oc_conversation_p2p",
            open_id="ou_conversation_app_scope",
            text=f"pair {code}",
            create_time_ms=2_000,
            chat_type="p2p",
        )

        await service._route_incoming(stage(db, admin))
        await service._route_incoming(stage(db, conversation))

        assert service._owner("admin") == "ou_admin_app_scope"
        assert service._owner("conversation") == "ou_conversation_app_scope"
        assert db.get_setting("owner_chat_id:admin") == "oc_admin_p2p"
        assert db.get_setting("owner_chat_id:conversation") == "oc_conversation_p2p"
        assert [item["role"] for item in gateway.texts] == ["admin", "conversation"]
        assert db.inbox_counts() == {"done": 2}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pairing_rejects_second_app_from_another_tenant(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    service = BridgeService(config, db, FakeCodex(), FakeGateway())  # type: ignore[arg-type]
    code, _ = generate_pairing_code(db, ttl_seconds=300)
    try:
        await service._route_incoming(
            stage(
                db,
                incoming(
                    "pair-a",
                    role="admin",
                    open_id="ou-a",
                    text=f"配对 {code}",
                    tenant_key="tenant-a",
                ),
            )
        )
        await service._route_incoming(
            stage(
                db,
                incoming(
                    "pair-b",
                    open_id="ou-b",
                    text=f"配对 {code}",
                    tenant_key="tenant-b",
                    chat_type="p2p",
                ),
            )
        )
        assert service._owner("admin") == "ou-a"
        assert service._owner("conversation") == ""
        assert db.get_setting("paired_tenant_key") == "tenant-a"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_completed_before_turn_start_response_leaves_no_ghost_active(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FastCompletingCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)
    message = incoming("om-fast", text="快速回答")
    item = stage(db, message)
    job = ScheduledMessage(
        inbox=item,
        binding=db.get_binding_by_thread("thread-1"),
        progress_message_id="progress-fast",
        app_role="conversation",
        chat_id="oc_thread",
    )
    try:
        await service._execute_job("thread-1", job)
        assert service._active_by_thread == {}
        assert service._active_by_turn == {}
        assert db.inbox_state("om-fast") == "done"
        assert db.outbox_counts() == {"pending": 1}
        assert db.list_recoverable_turn_jobs() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_never_resumes_stale_in_progress_turn(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    service = BridgeService(config, db, StaleInProgressCodex(), FakeGateway())  # type: ignore[arg-type]
    db.upsert_turn_job(
        TurnJob(
            message_id="om-stale",
            thread_id="thread-stale",
            turn_id="turn-stale",
            app_role="conversation",
            chat_id="oc-thread",
            progress_message_id="om-progress",
            state="running",
        )
    )
    try:
        await service._recover_turn_jobs()
        assert db.get_setting("blocked_thread:thread-stale") == "turn-stale"
        assert db.list_recoverable_turn_jobs() == []
        assert service._active_by_thread == {}
        assert db.outbox_counts() == {"pending": 1}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unauthorized_sender_is_completed_without_reply_or_codex_dispatch(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        owner_conversation_open_id="ou_expected_owner",
    )
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    try:
        attacker = incoming("om-attacker", open_id="ou_someone_else")
        await service._route_incoming(stage(db, attacker))

        assert db.inbox_state("om-attacker") == "done"
        assert gateway.texts == []
        assert gateway.cards == []
        assert codex.steered == []
        assert service._thread_queues == {}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_same_thread_messages_are_fifo_and_steer_targets_the_active_turn(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        owner_conversation_open_id="ou_conversation_owner",
    )
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    # Occupy the worker slot without consuming the queue so routing order can
    # be asserted deterministically.
    blocker = asyncio.create_task(asyncio.Event().wait())
    service._thread_workers["thread-1"] = blocker
    try:
        first = incoming("om-first", text="第一条", create_time_ms=1_000)
        second = incoming("om-second", text="第二条", create_time_ms=2_000)
        await service._route_incoming(stage(db, first))
        await service._route_incoming(stage(db, second))

        queue = service._thread_queues["thread-1"]
        first_job = queue.get_nowait()
        second_job = queue.get_nowait()
        assert [first_job.inbox.message.message_id, second_job.inbox.message.message_id] == [
            "om-first",
            "om-second",
        ]
        assert db.inbox_state("om-first") == "queued"
        assert db.inbox_state("om-second") == "queued"
        assert "正在启动" in gateway.cards[0]["card"]["elements"][0]["content"]
        assert "前面还有 1 条" in gateway.cards[1]["card"]["elements"][0]["content"]

        active = ActiveTurn(
            thread_id="thread-1",
            turn_id="turn-live",
            chat_id="oc_thread",
            progress_message_id="card-live",
        )
        service._register_active(active)
        steering = incoming("om-steer", text="!steer 优先检查失败日志", create_time_ms=3_000)
        await service._route_incoming(stage(db, steering))

        assert len(codex.steered) == 1
        assert codex.steered[0]["thread_id"] == "thread-1"
        assert codex.steered[0]["turn_id"] == "turn-live"
        assert codex.steered[0]["client_message_id"] == "om-steer"
        assert codex.steered[0]["inputs"][0] == {
            "type": "text",
            "text": "优先检查失败日志",
            "text_elements": [],
        }
        assert "专用目录" in codex.steered[0]["inputs"][1]["text"]
        assert db.inbox_state("om-steer") == "done"
        assert gateway.texts[-1]["idempotency_key"] == "steered:om-steer"
        assert queue.empty()
    finally:
        blocker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await blocker
        db.close()


@pytest.mark.asyncio
async def test_admin_freeform_message_is_durably_queued(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_admin_open_id="ou_admin_owner")
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)  # type: ignore[arg-type]
    message = incoming(
        "om-admin-queued",
        role="admin",
        chat_id="oc_admin_p2p",
        open_id="ou_admin_owner",
        text="解释一下当前桥的状态",
    )
    try:
        await service._route_incoming(stage(db, message))
        queued = service._admin_queue.get_nowait()
        assert queued.inbox.message.message_id == message.message_id
        assert db.inbox_state(message.message_id) == "queued"
        assert gateway.cards[-1]["idempotency_key"] == "admin-progress:om-admin-queued"
        service._admin_queue.task_done()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_conversation_private_chat_uses_admin_control_plane(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)  # type: ignore[arg-type]
    message = incoming(
        "om-conversation-private-help",
        chat_id="oc_conversation_p2p",
        open_id="ou_conversation_owner",
        text="帮助",
        chat_type="p2p",
    )
    try:
        await service._route_incoming(stage(db, message))

        assert db.inbox_state(message.message_id) == "done"
        assert gateway.texts[-1]["role"] == "conversation"
        assert "新对话" in gateway.texts[-1]["text"]
        assert "额度" in gateway.texts[-1]["text"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_conversation_private_freeform_is_durably_queued(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)  # type: ignore[arg-type]
    message = incoming(
        "om-conversation-private-task",
        chat_id="oc_conversation_p2p",
        open_id="ou_conversation_owner",
        text="解释一下当前桥的状态",
        chat_type="p2p",
    )
    try:
        await service._route_incoming(stage(db, message))

        queued = service._admin_queue.get_nowait()
        assert queued.app_role == "conversation"
        assert queued.inbox.message.message_id == message.message_id
        assert db.inbox_state(message.message_id) == "queued"
        assert gateway.cards[-1]["role"] == "conversation"
        service._admin_queue.task_done()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_conversation_private_fast_task_keeps_codex_role_through_final(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FastCompletingCodex()
    gateway = FakeGateway(configured_roles={"conversation"})
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    message = incoming(
        "om-conversation-private-fast",
        chat_id="oc_conversation_p2p",
        open_id="ou_conversation_owner",
        text="快速完成一个临时任务",
        chat_type="p2p",
    )
    worker = asyncio.create_task(service._admin_worker())
    try:
        await service._route_incoming(stage(db, message))
        await asyncio.wait_for(service._admin_queue.join(), timeout=1)

        assert db.inbox_state(message.message_id) == "done"
        assert db.list_recoverable_turn_jobs() == []
        outbound = db.claim_outbox("test-outbox")
        assert outbound is not None
        assert outbound.app_role == "conversation"
        assert outbound.receive_id == "oc_conversation_p2p"
        assert outbound.receive_id_type == "chat_id"
        assert outbound.content == {"text": "快速完成"}
        assert codex.archived_threads == ["thread-fast-scratch"]
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        db.close()


@pytest.mark.asyncio
async def test_attachment_only_message_waits_for_next_codex_text(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    gateway = FakeGateway()
    service = BridgeService(config, db, FakeCodex(), gateway)  # type: ignore[arg-type]
    bind_thread(db)
    blocker = asyncio.create_task(asyncio.Event().wait())
    service._thread_workers["thread-1"] = blocker
    media = incoming("om-held-image", text="", create_time_ms=1_000)
    media.message_type = "image"
    media.attachments = [
        Attachment(
            kind="image",
            name="screen.png",
            message_id=media.message_id,
            image_key="img-key",
        )
    ]
    text = incoming("om-image-instruction", text="分析这张图", create_time_ms=2_000)
    try:
        await service._route_incoming(stage(db, media))

        assert db.inbox_state(media.message_id) == "held"
        assert len(gateway.downloads) == 1
        assert db.outbox_counts() == {"pending": 1}
        assert service._thread_queues == {}

        await service._route_incoming(stage(db, text))
        queued = service._thread_queues["thread-1"].get_nowait()
        assert queued.inbox.message.message_id == text.message_id
        assert [
            item.message_id for item in queued.inbox.message.attachments
        ] == [media.message_id]
        assert queued.inbox.message.attachments[0].local_path is not None
        assert db.inbox_state(media.message_id) == "done"
        service._thread_queues["thread-1"].task_done()
    finally:
        blocker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await blocker
        db.close()


@pytest.mark.asyncio
async def test_runtime_command_does_not_consume_held_attachment(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    service = BridgeService(config, db, FakeCodex(), FakeGateway())  # type: ignore[arg-type]
    bind_thread(db)
    media = incoming("om-held-before-status", text="", create_time_ms=1_000)
    media.message_type = "image"
    media.attachments = [
        Attachment(
            kind="image",
            name="screen.png",
            message_id=media.message_id,
            image_key="img-key",
        )
    ]
    try:
        await service._route_incoming(stage(db, media))
        await service._route_incoming(
            stage(db, incoming("om-runtime-status", text="/status", create_time_ms=2_000))
        )

        assert db.inbox_state(media.message_id) == "held"
        assert db.inbox_state("om-runtime-status") == "done"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_system_alert_prefers_conversation_private_bot(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        owner_admin_open_id="ou_admin_owner",
        owner_conversation_open_id="ou_conversation_owner",
    )
    db = BridgeDB(config.database_path)
    gateway = FakeGateway(configured_roles={"admin", "conversation"})
    service = BridgeService(config, db, FakeCodex(), gateway)  # type: ignore[arg-type]
    try:
        await service._notify_admin("测试告警")

        assert gateway.texts[-1]["role"] == "conversation"
        assert gateway.texts[-1]["receive_id"] == "ou_conversation_owner"
        assert gateway.texts[-1]["receive_id_type"] == "open_id"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_runtime_commands_persist_audit_and_apply_to_codex(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    try:
        commands = [
            ("runtime-permission", "/permissions full-access"),
            ("runtime-model", "/model gpt-test high"),
            ("runtime-speed", "/fast"),
            ("runtime-show", "/status"),
            ("runtime-history", "!配置记录"),
        ]
        for index, (message_id, command) in enumerate(commands, 1):
            await service._route_incoming(
                stage(db, incoming(message_id, text=command, create_time_ms=index * 1000))
            )

        settings = service._runtime_settings("thread-1")
        assert settings == RuntimeSettings(
            model="gpt-test",
            effort="high",
            service_tier="priority",
            approval_policy="never",
            sandbox="danger-full-access",
        )
        assert codex.setting_updates[-1] == {
            "thread_id": "thread-1",
            "approval_policy": "never",
            "sandbox": "danger-full-access",
            "model": "gpt-test",
            "effort": "high",
            "service_tier": "priority",
        }
        assert len(db.runtime_config_history("thread-1")) == 5
        assert "GPT" not in gateway.texts[-2]["text"] or "gpt-test" in gateway.texts[-2]["text"]
        assert "最近配置变更" in gateway.texts[-1]["text"]
        assert db.inbox_counts() == {"done": len(commands)}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cli_setting_commands_render_pickers_and_toggle_fast(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    try:
        await service._route_incoming(stage(db, incoming("pick-model", text="/model")))
        model_card = gateway.cards[-1]["card"]
        model_buttons = [
            action
            for element in model_card["elements"]
            for action in element.get("actions", [])
        ]
        assert model_buttons[0]["value"] == {
            "kind": "codex_setting",
            "setting": "model",
            "model": "gpt-test",
        }

        await service._route_incoming(
            stage(db, incoming("pick-permissions", text="/permissions"))
        )
        permission_card = gateway.cards[-1]["card"]
        profiles = [
            action["value"]["profile"]
            for element in permission_card["elements"]
            for action in element.get("actions", [])
        ]
        assert profiles == ["read-only", "default", "full-access"]

        await service._route_incoming(stage(db, incoming("toggle-fast-1", text="/fast")))
        assert service._runtime_settings("thread-1").service_tier == "priority"
        await service._route_incoming(stage(db, incoming("toggle-fast-2", text="/fast")))
        assert service._runtime_settings("thread-1").service_tier is None
        assert "Fast 模式已关闭" in gateway.texts[-1]["text"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cli_version_change_fails_closed_for_slash_settings(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    codex.cli_version = "9.9.9"
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    try:
        await service._probe_runtime_settings_compatibility()
        await service._route_incoming(stage(db, incoming("future-fast", text="/fast")))
        assert service._runtime_settings("thread-1").service_tier is None
        assert "兼容门禁已触发" in gateway.texts[-1]["text"]
        assert "9.9.9" in gateway.texts[-1]["text"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cli_version_change_sends_repair_card_to_codex_private_chat(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    codex.cli_version = "9.9.9"
    gateway = FakeGateway(configured_roles={"conversation"})
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]

    try:
        await service._probe_runtime_settings_compatibility()

        assert gateway.cards[-1]["role"] == "conversation"
        assert gateway.cards[-1]["receive_id"] == "ou_conversation_owner"
        actions = gateway.cards[-1]["card"]["elements"][-1]["actions"]
        assert [action["text"]["content"] for action in actions] == [
            "检测并修复",
            "暂不处理",
        ]
        assert actions[0]["value"] == {
            "kind": "codex_compatibility",
            "action": "repair",
            "version": "9.9.9",
        }
    finally:
        db.close()


@pytest.mark.asyncio
async def test_repair_card_exercises_protocol_and_unlocks_cli_settings(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    codex.cli_version = "9.9.9"
    gateway = FakeGateway(configured_roles={"conversation"})
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    try:
        await service._probe_runtime_settings_compatibility()
        repair = incoming(
            "compat-repair",
            text="/bridge-settings-compat repair 9.9.9",
            chat_type="p2p",
        )
        repair.message_type = "card_action"
        await service._route_incoming(stage(db, repair))

        assert service._runtime_compatibility_error is None
        assert db.get_setting("codex_settings_verified_version") == "9.9.9"
        assert codex.started_threads == [
            {
                "cwd": str(config.admin_scratch_dir),
                "approval_policy": "on-request",
                "sandbox": "workspace-write",
                "model": None,
                "service_tier": None,
                "ephemeral": True,
            }
        ]
        assert codex.setting_updates[0]["thread_id"] == "thread-probe-1"
        assert codex.unsubscribed_threads == ["thread-probe-1"]
        assert gateway.cards[-1]["card"]["header"]["template"] == "green"

        await service._route_incoming(stage(db, incoming("fast-after-repair", text="/fast")))
        assert service._runtime_settings("thread-1").service_tier == "priority"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_dismissed_cli_version_prompt_stays_gated_without_reprompt(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    codex.cli_version = "9.9.9"
    gateway = FakeGateway(configured_roles={"conversation"})
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]

    try:
        await service._probe_runtime_settings_compatibility()
        initial_cards = len(gateway.cards)
        dismiss = incoming(
            "compat-dismiss",
            text="/bridge-settings-compat dismiss 9.9.9",
            chat_type="p2p",
        )
        dismiss.message_type = "card_action"
        await service._route_incoming(stage(db, dismiss))
        assert db.get_setting("codex_settings_prompt_dismissed_version") == "9.9.9"

        result_cards = len(gateway.cards)
        assert result_cards == initial_cards + 1
        await service._probe_runtime_settings_compatibility()
        assert len(gateway.cards) == result_cards
        assert service._runtime_compatibility_error is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_global_runtime_defaults_cover_new_scopes_and_can_be_overridden(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    config.model = "gpt-test"
    config.model_reasoning_effort = "high"
    config.service_tier = "priority"
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)

    try:
        assert service._runtime_settings("future-thread") == RuntimeSettings(
            model="gpt-test",
            effort="high",
            service_tier="priority",
            approval_policy="on-request",
            sandbox="workspace-write",
        )
        assert service._runtime_settings("admin").model == "gpt-test"

        await service._route_incoming(stage(db, incoming("default-fast-off", text="/fast")))
        assert service._runtime_settings("thread-1").service_tier is None
        await service._route_incoming(
            stage(db, incoming("default-effort-off", text="/model gpt-test default"))
        )
        assert service._runtime_settings("thread-1").effort is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_new_thread_crosses_ambiguity_boundary_before_rpc(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_admin_open_id="ou_admin_owner")
    db = BridgeDB(config.database_path)
    message = incoming(
        "om-new-crash",
        role="admin",
        chat_id="oc_admin_p2p",
        open_id="ou_admin_owner",
        text="新对话 崩溃边界测试",
    )
    codex = ThreadStartCrashCodex(db, message.message_id)
    service = BridgeService(config, db, codex, FakeGateway())  # type: ignore[arg-type]
    item = stage(db, message)
    try:
        with pytest.raises(ConnectionError) as caught:
            await service._route_incoming(item)
        service._record_incoming_failure(item, caught.value)

        assert codex.state_at_rpc == "dispatching"
        assert db.inbox_state(message.message_id) == "ambiguous"
        assert db.outbox_counts() == {"pending": 1}
        assert db.recover_inbox_after_restart() == []
        assert db.claim_incoming("recovery-worker") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_helper_thread_crosses_ambiguity_boundary_before_rpc(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_admin_open_id="ou_admin_owner")
    db = BridgeDB(config.database_path)
    message = incoming(
        "om-helper-crash",
        role="admin",
        chat_id="oc_admin_p2p",
        open_id="ou_admin_owner",
        text="做一个零散解释",
    )
    codex = ThreadStartCrashCodex(db, message.message_id)
    service = BridgeService(config, db, codex, FakeGateway())  # type: ignore[arg-type]
    worker = asyncio.create_task(service._admin_worker())
    try:
        await service._route_incoming(stage(db, message))
        await asyncio.wait_for(service._admin_queue.join(), timeout=1)

        assert codex.state_at_rpc == "dispatching"
        assert db.inbox_state(message.message_id) == "ambiguous"
        assert db.outbox_counts() == {"pending": 1}
        assert db.recover_inbox_after_restart() == []
        assert db.claim_incoming("recovery-worker") is None
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        db.close()


@pytest.mark.asyncio
async def test_conversation_turn_start_crash_queues_durable_phone_alert(tmp_path: Path) -> None:
    config = make_config(tmp_path, owner_conversation_open_id="ou_conversation_owner")
    db = BridgeDB(config.database_path)
    message = incoming("om-turn-crash", text="执行一个任务")
    codex = TurnStartCrashCodex(db, message.message_id)
    service = BridgeService(config, db, codex, FakeGateway())  # type: ignore[arg-type]
    bind_thread(db)
    item = stage(db, message)
    db.mark_incoming_queued(message.message_id)
    job = ScheduledMessage(
        inbox=item,
        binding=db.get_binding_by_thread("thread-1"),
        progress_message_id="progress-crash",
        app_role="conversation",
        chat_id="oc_thread",
    )
    try:
        await service._execute_job("thread-1", job)
        assert codex.state_at_rpc == "dispatching"
        assert db.inbox_state(message.message_id) == "ambiguous"
        assert db.outbox_counts() == {"pending": 1}
        assert db.claim_incoming("recovery-worker") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reconcile_never_binds_orphan_from_admin_scratch(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    scratch = ThreadSummary(
        thread_id="thread-admin-orphan",
        name=None,
        preview="",
        cwd=str(config.admin_scratch_dir),
        created_at=2,
        updated_at=3,
        source_kind="appServer",
    )
    normal = ThreadSummary(
        thread_id="thread-normal",
        name="正常对话",
        preview="",
        cwd=str(tmp_path),
        created_at=1,
        updated_at=2,
        source_kind="cli",
    )
    service = BridgeService(
        config,
        db,
        StaticThreadsCodex([scratch, normal]),
        FakeGateway(),  # type: ignore[arg-type]
    )
    try:
        bindings = await service.reconcile_once()
        assert [item.thread_id for item in bindings] == ["thread-normal"]
        assert db.get_binding_by_thread("thread-admin-orphan") is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reconcile_updates_existing_chat_description_once(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        owner_conversation_open_id="ou_conversation_owner",
    )
    db = BridgeDB(config.database_path)
    summary = ThreadSummary(
        thread_id="thread-normal",
        name="正常对话",
        preview="",
        cwd=str(tmp_path / "project"),
        created_at=1_700_000_000,
        updated_at=1_700_000_100,
        source_kind="cli",
    )
    db.upsert_thread(summary)
    db.bind_chat(summary.thread_id, "oc-existing")
    gateway = FakeGateway(configured_roles={"conversation"})
    service = BridgeService(
        config,
        db,
        StaticThreadsCodex([summary]),
        gateway,  # type: ignore[arg-type]
    )
    try:
        await service.reconcile_once()
        await service.reconcile_once()
        assert gateway.chat_updates == [
            {
                "chat_id": "oc-existing",
                "thread_id": "thread-normal",
                "cwd": str(tmp_path / "project"),
                "created_at": 1_700_000_000,
            }
        ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_approval_command_returns_exact_permissions_payload(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        owner_conversation_open_id="ou_conversation_owner",
    )
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway()
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    requested = {"network": {"enabled": True}, "fileSystem": {"read": ["/tmp"]}}
    codex.pending_requests["rpc-permissions"] = {"id": "rpc-permissions"}
    db.add_approval(
        PendingApproval(
            short_id="permit-1",
            rpc_id="rpc-permissions",
            method="item/permissions/requestApproval",
            thread_id="thread-1",
            turn_id="turn-1",
            chat_id="oc_thread",
            params={"permissions": requested},
        )
    )

    try:
        allow = incoming("om-allow", text="批准 permit-1")
        await service._route_incoming(stage(db, allow))

        assert codex.responses == [
            ("rpc-permissions", {"permissions": requested, "scope": "turn"})
        ]
        assert db.get_approval("permit-1", "oc_thread") is None
        assert db.inbox_state("om-allow") == "done"
        assert gateway.texts[-1]["text"] == "✅ 已允许一次。"
    finally:
        db.close()


def test_all_approval_result_payload_shapes() -> None:
    assert BridgeService._allow_payload(
        "item/commandExecution/requestApproval", {}
    ) == {"decision": "accept"}
    assert BridgeService._allow_payload("applyPatchApproval", {}) == {
        "decision": "approved"
    }
    assert BridgeService._deny_payload(
        "item/fileChange/requestApproval", {}, cancel=False
    ) == {"decision": "decline"}
    assert BridgeService._deny_payload(
        "item/fileChange/requestApproval", {}, cancel=True
    ) == {"decision": "cancel"}
    assert BridgeService._deny_payload(
        "item/permissions/requestApproval", {"permissions": {"network": True}}
    ) == {"permissions": {}, "scope": "turn"}
    assert BridgeService._answer_payload(
        {"questions": [{"id": "branch"}, {"id": "mode"}]},
        "branch=main; mode=safe",
    ) == {
        "answers": {
            "branch": {"answers": ["main"]},
            "mode": {"answers": ["safe"]},
        }
    }


@pytest.mark.asyncio
async def test_unknown_server_request_uses_protocol_error_not_fake_approval(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    service = BridgeService(config, db, codex, FakeGateway())  # type: ignore[arg-type]
    try:
        await service._on_codex_request(
            {"id": 99, "method": "item/tool/call", "params": {"threadId": "t"}}
        )
        assert codex.responses == []
        assert codex.errors == [
            ("99", -32601, "unsupported bridge server request: item/tool/call")
        ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_history_backfill_overlaps_and_dedupes_by_message_id(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        owner_conversation_open_id="ou_conversation_owner",
    )
    db = BridgeDB(config.database_path)
    codex = FakeCodex()
    gateway = FakeGateway(configured_roles={"conversation"})
    service = BridgeService(config, db, codex, gateway)  # type: ignore[arg-type]
    bind_thread(db)
    replayed = incoming("om-history", text="断线期间的消息")
    # Simulate both overlap inside one page and the same message being seen on
    # the next recovery scan.
    gateway.history = [replayed, replayed]

    try:
        await service._backfill_once()
        await service._backfill_once()

        assert db.inbox_counts() == {"pending": 1}
        assert len(gateway.history_calls) == 2
        assert all(call["role"] == "conversation" for call in gateway.history_calls)
        assert all(call["chat_id"] == "oc_thread" for call in gateway.history_calls)
        assert gateway.history_calls[1]["start_time_seconds"] >= (
            gateway.history_calls[0]["start_time_seconds"]
        )
        claimed = db.claim_incoming("history-worker")
        assert claimed is not None
        assert claimed.message.message_id == "om-history"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_external_sync_baselines_history_before_delivering_new_turns(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    old_turn = {
        "id": "turn-old",
        "status": "completed",
        "items": [
            {
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "不应补发的历史回复",
            }
        ],
    }
    codex = ThreadHistoryCodex([old_turn])
    service = BridgeService(config, db, codex, FakeGateway())  # type: ignore[arg-type]
    bind_thread(db)
    summary = ThreadSummary(
        thread_id="thread-1",
        name="受控对话",
        preview="",
        cwd="/home/tester",
        created_at=1,
        updated_at=2,
        source_kind="cli",
    )

    try:
        await service._sync_external_updates([summary])

        assert db.is_turn_synced("thread-1", "turn-old") is True
        assert db.get_setting("external_sync_initialized:thread-1") == "1"
        assert db.outbox_counts() == {}

        codex.turns.append(
            {
                "id": "turn-new",
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "应当发送的新回复",
                    }
                ],
            }
        )
        await service._sync_external_updates([summary])

        assert db.is_turn_synced("thread-1", "turn-new") is False
        assert db.outbox_counts() == {"pending": 1}
        outbound = db.claim_outbox("test-worker")
        assert outbound is not None
        assert outbound.thread_id == "thread-1"
        assert outbound.turn_id == "turn-new"
        assert "应当发送的新回复" in outbound.content["text"]
    finally:
        db.close()


def test_automatic_file_delivery_only_accepts_dedicated_outbox(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    service = BridgeService(config, db, FakeCodex(), FakeGateway())  # type: ignore[arg-type]
    private = tmp_path / "private.txt"
    private.write_text("not for automatic upload", encoding="utf-8")
    deliverable = config.outbox_dir / "task" / "report.txt"
    deliverable.parent.mkdir(parents=True)
    deliverable.write_text("safe report", encoding="utf-8")
    try:
        text = f"[private]({private}) [report]({deliverable})"
        assert service.artifacts.outgoing_paths(text) == [deliverable]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_small_outbox_file_is_queued_without_second_approval(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = BridgeDB(config.database_path)
    service = BridgeService(config, db, FakeCodex(), FakeGateway())  # type: ignore[arg-type]
    deliverable = config.outbox_dir / "task" / "report.txt"
    deliverable.parent.mkdir(parents=True)
    deliverable.write_text("safe report", encoding="utf-8")
    active = ActiveTurn(
        thread_id="thread-1",
        turn_id="turn-file",
        chat_id="oc_thread",
        final_text=f"已完成：[report]({deliverable})",
    )
    try:
        await service._finalize_turn(active, {"status": "completed", "items": []})

        assert db.outbox_counts() == {"pending": 2}
        text_item = db.claim_outbox("test-worker")
        assert text_item is not None
        assert text_item.msg_type == "text"
        db.complete_outbox(text_item.outbox_key)
        file_item = db.claim_outbox("test-worker")
        assert file_item is not None
        assert file_item.msg_type == "local_file"
        assert file_item.content["path"] == str(deliverable)
    finally:
        db.close()
