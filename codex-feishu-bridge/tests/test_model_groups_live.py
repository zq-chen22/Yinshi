"""Opt-in isolated acceptance. No production DB, Feishu posting or project data."""

import asyncio
import os
import secrets
from pathlib import Path

import pytest

from codex_feishu_bridge.service import BridgeService
from test_model_groups import job_for
from test_profile_commands import service_for_test
from test_service import incoming, stage

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("YINSHI_LIVE_CLAUDE_KEY_FILE"),
        reason="explicit billed Claude pilot only",
    ),
]


async def test_native_claude_two_tiers_restore_and_file(tmp_path):
    s, d, c, g = service_for_test(tmp_path)
    key = Path(os.environ["YINSHI_LIVE_CLAUDE_KEY_FILE"])
    s.claude.key_path = key
    s._codex_online = False
    s._runtime_compatibility_error = "Codex deliberately unavailable"

    async def forbidden(*a, **kw):
        pytest.fail("Claude-only path reached Codex")

    c.start = c.start_turn = c.read_thread = c.list_models = c.start_thread = forbidden
    nonce = secrets.token_hex(5)

    async def ask(s, mid, q):
        await asyncio.wait_for(
            s._execute_job("thread-1", job_for(s, d, tmp_path, mid, q)), 90
        )
        terminal = d.claude_terminal(mid)
        assert terminal and terminal["status"] == "completed", (
            terminal and terminal["text"][:160]
        )
        return terminal["text"]

    await s._route_incoming(stage(d, incoming("switch1", text="c组默认")))
    assert "OK" in await ask(s, "first", "记住测试编号 " + nonce + "，仅回复 OK。")
    # A new service object reopens the same native session by exact ID.
    s = BridgeService(s.config, d, c, g)
    s.claude.key_path = key
    s._codex_online = False
    assert nonce in await ask(s, "second", "只回复刚才的测试编号，不要读文件。")
    await s._route_incoming(stage(d, incoming("switch2", text="C组高级")))
    r = await ask(
        s,
        "file",
        "请使用 Write 工具在本轮指定的飞书交付目录新建 result.txt，内容为 LIVE_OK，最终链接该文件。",
    )
    import hashlib

    f = s.config.outbox_dir / hashlib.sha256(b"file").hexdigest()[:20] / "result.txt"
    assert f.is_file() and "LIVE_OK" in f.read_text() and "result.txt" in r
    assert (
        d._conn.execute(
            "SELECT COUNT(*) FROM outbox_messages WHERE msg_type='local_file'"
        ).fetchone()[0]
        == 1
    )
    assert all(
        Path(v).is_file()
        for (v,) in d._conn.execute(
            "SELECT json_extract(context_json,'$.log') FROM group_questions"
        )
    )
    assert not c.started_threads
    d.close()


async def test_real_blind_answers_at_selected_tier(tmp_path):
    """Explicitly opt in to one Codex and one Claude neutral, independent answer."""
    import json
    from codex_feishu_bridge.models import ActiveTurn, TurnJob

    s, d, c, g = service_for_test(tmp_path)
    s.claude.key_path = Path(os.environ["YINSHI_LIVE_CLAUDE_KEY_FILE"])
    object.__setattr__(s.config, "codex_bin", str(Path.home() / ".local/bin/codex"))
    j = job_for(s, d, tmp_path, "source", "Reply with only PAIR_OK.")
    s._question_inputs(j, "thread-1", [])
    d.complete_incoming("source")
    j = job_for(s, d, tmp_path, "control", "请做高级交叉验证", parent="source")
    ctx, _ = s._question_inputs(j, "thread-1", [])
    active = ActiveTurn(
        "thread-1",
        "demo",
        "oc_thread",
        final_text=json.dumps(
            dict(
                yinshi_consult=ctx["nonce"],
                target="all",
                tier="advanced",
                source="quoted",
            )
        ),
    )
    d.upsert_turn_job(
        TurnJob(
            "control", "thread-1", "demo", "conversation", "oc_thread", None, "accepted"
        )
    )
    assert await s._group_final(active, "completed")
    assert active.final_text.count("PAIR_OK") == 2, active.final_text
    assert (
        "claude-fable-5-2 / high" in active.final_text
        and "gpt-6.2-astra / max" in active.final_text
    )
    d.close()


async def test_natural_request_from_claude_consults_codex_once(tmp_path):
    s, d, c, g = service_for_test(tmp_path)
    s.claude.key_path = Path(os.environ["YINSHI_LIVE_CLAUDE_KEY_FILE"])
    object.__setattr__(s.config, "codex_bin", str(Path.home() / ".local/bin/codex"))
    await s._route_incoming(stage(d, incoming("switch", text="c组默认")))
    await asyncio.wait_for(
        s._execute_job(
            "thread-1",
            job_for(
                s,
                d,
                tmp_path,
                "please",
                "请让另一组独立回答下面这一句原题，把它的原话给我：Reply only NL_OK.",
            ),
        ),
        100,
    )
    rows = d._conn.execute(
        "SELECT model_group,result FROM group_consults WHERE message_id='please'"
    ).fetchall()
    assert [(r[0], r[1].splitlines()[-1].strip()) for r in rows] == [("codex", "NL_OK")]
    assert s._group("thread-1") == "c" and not c.started_threads
    d.close()


async def test_natural_request_from_codex_consults_claude_once(tmp_path):
    from codex_feishu_bridge.group_service import blind_codex
    from codex_feishu_bridge.model_profiles import load_profiles
    from codex_feishu_bridge.models import ActiveTurn, TurnJob

    s, d, _, _ = service_for_test(tmp_path)
    s.claude.key_path = Path(os.environ["YINSHI_LIVE_CLAUDE_KEY_FILE"])
    q = "请让 C 组独立回答这句原题，原样转达它的回答：Reply only REVERSE_OK."
    j = job_for(s, d, tmp_path, "reverse", q)
    ctx, inputs = s._question_inputs(j, "thread-1", [{"type": "text", "text": q}])
    output = await asyncio.wait_for(
        blind_codex(
            str(Path.home() / ".local/bin/codex"),
            load_profiles(d)["default"],
            inputs,
            tmp_path,
        ),
        100,
    )
    active = ActiveTurn("thread-1", "isolated-codex", "oc_thread", final_text=output)
    d.upsert_turn_job(
        TurnJob(
            "reverse",
            "thread-1",
            active.turn_id,
            "conversation",
            "oc_thread",
            None,
            "accepted",
        )
    )
    assert await s._group_final(active, "completed")
    assert (
        active.final_text.rstrip().endswith("REVERSE_OK")
        and "claude-opus-5-5 / medium" in active.final_text
    )
    assert s._group("thread-1") == "codex"
    d.close()


@pytest.mark.parametrize("operation", ["stop", "steer"])
async def test_native_control_while_tool_runs(tmp_path, operation):
    import uuid
    from codex_feishu_bridge.claude_runner import ClaudeError
    from codex_feishu_bridge.groups import C_GROUP

    s, d, _, _ = service_for_test(tmp_path)
    s.claude.key_path = Path(os.environ["YINSHI_LIVE_CLAUDE_KEY_FILE"])
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    used = []
    session = str(uuid.uuid4())
    sessions = []

    def event(op, _delta):
        if op == "工具：Bash" and not used:
            used.append(op)
            ctrl = s.claude.running["control"]
            if operation == "stop":
                ctrl.stop()
            else:
                assert ctrl.steer(
                    [{"type": "text", "text": "不要再运行命令。立刻只回复 STEER_OK。"}]
                )

    task = s.claude.run(
        "control",
        C_GROUP["default"],
        [
            {
                "type": "text",
                "text": "这是定时工具测试，务必用 Bash 执行 sleep 9；完成后只回复 OLD。",
            }
        ],
        cwd=work,
        session=session,
        resume=False,
        persistent=True,
        profile="full",
        mounts=[],
        outbox=None,
        on_event=event,
        on_session=sessions.append,
    )
    if operation == "stop":
        with pytest.raises(ClaudeError, match="按要求停止"):
            await asyncio.wait_for(task, 70)
    else:
        assert (await asyncio.wait_for(task, 70)).strip() == "STEER_OK"
        assert sessions == [session, session]
    assert used and not s.claude.running
    d.close()
