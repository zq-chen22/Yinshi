from __future__ import annotations

import asyncio
import stat
import textwrap

import pytest

from codex_feishu_bridge.codex_client import (
    CodexAppServer,
    extract_agent_messages,
    latest_final_from_thread,
)


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import sys


def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:
        continue
    if method == "initialize":
        send({"id": request_id, "result": {
            "serverInfo": {"name": "fake"},
            "userAgent": "codex_feishu_bridge/0.144.1 (test)",
        }})
    elif method == "thread/list":
        if params.get("cursor") == "page-2":
            send({
                "id": request_id,
                "result": {
                    "data": [{
                        "id": "thread-2",
                        "name": "第二个对话",
                        "preview": "ignored",
                        "cwd": "/work/two",
                        "createdAt": 2,
                        "updatedAt": 22,
                        "sourceKind": "vscode",
                    }],
                    "nextCursor": None,
                },
            })
        else:
            send({
                "id": request_id,
                "result": {
                    "data": [
                        {
                            "id": "thread-1",
                            "name": None,
                            "preview": "第一个对话\n详情",
                            "cwd": "/work/one",
                            "createdAt": 1,
                            "updatedAt": 21,
                            "source": {"kind": "cli"},
                        },
                        {
                            "id": "ephemeral",
                            "preview": "临时",
                            "cwd": "/tmp",
                            "createdAt": 3,
                            "updatedAt": 23,
                            "ephemeral": True,
                        },
                        {
                            "id": "child",
                            "preview": "子代理",
                            "cwd": "/tmp",
                            "createdAt": 4,
                            "updatedAt": 24,
                            "parentThreadId": "thread-1",
                        },
                    ],
                    "nextCursor": "page-2",
                },
            })
    elif method == "thread/read":
        send({
            "id": request_id,
            "result": {"thread": {
                "id": params["threadId"],
                "turns": [],
                "largePayload": "x" * (17 * 1024 * 1024),
            }},
        })
    elif method == "thread/turns/list":
        send({
            "id": request_id,
            "result": {
                "data": [{
                    "id": "turn-summary",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "summary only"}],
                    "itemsView": "summary",
                }],
                "nextCursor": None,
                "received": params,
            },
        })
    elif method == "thread/resume":
        send({"id": request_id, "result": {
            "thread": {"id": params["threadId"]}, "received": params
        }})
    elif method == "thread/compact/start":
        if set(params) != {"threadId"}:
            send({"id": request_id, "error": {"message": "unexpected compact params"}})
        else:
            send({"id": request_id, "result": {}})
    elif method == "thread/settings/update":
        send({"id": request_id, "result": {"applied": params}})
    elif method == "model/list":
        send({"id": request_id, "result": {"data": [{
            "id": "gpt-test", "model": "gpt-test", "displayName": "GPT Test"
        }], "nextCursor": None}})
    elif method == "turn/start":
        send({
            "method": "turn/started",
            "params": {"threadId": params["threadId"], "turn": {"id": "turn-1"}},
        })
        send({
            "id": 777,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": params["threadId"], "turnId": "turn-1"},
        })
        send({"id": request_id, "result": {"turn": {"id": "turn-1", "received": params}}})
    elif method == "account/rateLimits/read":
        send({"id": request_id, "result": {"primary": {"usedPercent": 12}}})
    elif method == "account/usage/read":
        send({"id": request_id, "result": {"plan": "test"}})
    else:
        send({"id": request_id, "result": {}})
'''


def make_fake_codex(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


@pytest.mark.asyncio
async def test_jsonl_client_filters_threads_and_dispatches_events(tmp_path):
    notifications = asyncio.Event()
    approvals = asyncio.Event()
    approval_method: list[str] = []
    client = CodexAppServer(
        codex_bin=str(make_fake_codex(tmp_path)),
        request_timeout=2,
    )

    async def on_notification(message):
        if message.get("method") == "turn/started":
            notifications.set()

    async def on_server_request(message):
        approval_method.append(message["method"])
        await client.respond_server_request(message["id"], {"decision": "decline"})
        approvals.set()

    client.add_notification_handler(on_notification)
    client.set_server_request_handler(on_server_request)

    async with client:
        assert client.cli_version == "0.144.1"
        threads = await client.list_threads(
            limit=2,
            source_kinds=["cli", "vscode"],
            sort_key="recency_at",
        )
        assert [item.thread_id for item in threads] == ["thread-1", "thread-2"]
        assert threads[0].display_name == "第一个对话"
        assert [item.source_kind for item in threads] == ["cli", "vscode"]

        read = await client.read_thread("thread-1")
        assert read["id"] == "thread-1"
        assert len(read["largePayload"]) == 17 * 1024 * 1024

        turns = await client.list_turns(
            "thread-1", limit=1, items_view="summary", sort_direction="desc"
        )
        assert turns["data"][0]["id"] == "turn-summary"
        assert turns["received"] == {
            "threadId": "thread-1",
            "limit": 1,
            "itemsView": "summary",
            "sortDirection": "desc",
        }

        resumed = await client.resume_thread(
            "thread-1", cwd="/work/migrated", exclude_turns=True
        )
        assert resumed["received"]["cwd"] == "/work/migrated"
        await client.compact_thread("thread-1")

        turn = await client.start_turn(
            "thread-1",
            [{"type": "text", "text": "继续"}],
            client_message_id="om_unique",
            approval_policy="on-request",
            sandbox="workspace-write",
            model="gpt-test",
            effort="high",
            service_tier="priority",
        )
        assert turn["id"] == "turn-1"
        assert turn["received"]["model"] == "gpt-test"
        assert turn["received"]["effort"] == "high"
        assert turn["received"]["serviceTier"] == "priority"
        assert turn["received"]["sandboxPolicy"]["type"] == "workspaceWrite"
        models = await client.list_models()
        assert models[0]["model"] == "gpt-test"
        updated = await client.update_thread_settings(
            "thread-1",
            approval_policy="never",
            sandbox="danger-full-access",
            model="gpt-test",
            effort="high",
            service_tier="priority",
        )
        assert updated["applied"]["sandboxPolicy"] == {"type": "dangerFullAccess"}
        assert "thread-1" in client._resumed_threads
        await client.unsubscribe_thread("thread-1")
        assert "thread-1" not in client._resumed_threads
        await asyncio.wait_for(notifications.wait(), timeout=1)
        await asyncio.wait_for(approvals.wait(), timeout=1)
        assert approval_method == ["item/commandExecution/requestApproval"]
        assert client.pending_server_request(777) is None

        quota = await client.quota()
        assert quota["rate_limits"]["primary"]["usedPercent"] == 12
        assert quota["usage"]["plan"] == "test"


def test_agent_message_extractors_prefer_final_answer():
    turn = {
        "id": "turn-1",
        "status": "completed",
        "items": [
            {"type": "agentMessage", "phase": "commentary", "text": "处理中"},
            {"type": "commandExecution", "command": "pytest"},
            {"type": "agentMessage", "phase": "final_answer", "text": "已完成"},
        ],
    }
    commentary, final = extract_agent_messages(turn)
    assert commentary == ["处理中"]
    assert final == ["已完成"]
    assert latest_final_from_thread({"turns": [turn]}) == ("turn-1", "已完成")


def test_latest_message_falls_back_to_completed_commentary():
    thread = {
        "turns": [
            {
                "id": "turn-2",
                "status": "failed",
                "items": [
                    {"type": "agentMessage", "phase": "commentary", "text": "执行失败"}
                ],
            }
        ]
    }
    assert latest_final_from_thread(thread) == ("turn-2", "执行失败")
