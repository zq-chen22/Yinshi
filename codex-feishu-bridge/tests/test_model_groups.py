import asyncio
import json

import pytest

from codex_feishu_bridge.groups import FORMULA_RULE, action, append_journal, registry
from codex_feishu_bridge.model_profiles import profile_command
from codex_feishu_bridge.models import ActiveTurn, TurnJob
from codex_feishu_bridge.service import ScheduledMessage, _split_utf8
from test_profile_commands import service_for_test
from test_service import incoming, stage


def job_for(s, d, root, mid, text, parent=None):
    directory = root / "work"
    directory.mkdir(exist_ok=True)
    d._conn.execute(
        "UPDATE bindings SET cwd=? WHERE thread_id='thread-1'", (str(directory),)
    )
    d._conn.commit()
    m = incoming(mid, text=text)
    m.parent_id = parent
    it = stage(d, m)
    binding = d.get_binding_by_thread("thread-1")
    return ScheduledMessage(it, binding, "card-" + mid, m.app_role, m.chat_id)


@pytest.mark.parametrize("prefix", ["", "全部"])
@pytest.mark.parametrize("letter", ["c", "C"])
@pytest.mark.parametrize("tier", ["默认", "高级"])
def test_keyword_exact(prefix, letter, tier):
    word = prefix + letter + "组" + tier
    assert profile_command(word) == (prefix + "C组" + tier, "")
    for s in [
        "请" + word,
        word + "。",
        "讨论" + word + "的效果",
        "交叉印证",
        "高级交叉验证",
    ]:
        assert profile_command(s) is None


@pytest.mark.asyncio
async def test_scoped_settings_and_idempotency(tmp_path):
    s, d, c, g = service_for_test(tmp_path)
    s.claude.available = lambda: True
    s._runtime_compatibility_error = "offline"
    first = stage(d, incoming("k1", text="全部c组默认"))
    await s._route_incoming(first)
    assert [s._group(n) for n in ("thread-1", "random-thread", "admin")] == ["c"] * 3
    assert s._group_level("random-thread") == "default"
    await s._route_incoming(stage(d, incoming("k2", text="C组高级")))
    await s._try_model_profile_command(first)
    assert s._group_level("thread-1") == "advanced"
    assert not c.started_threads and not c.setting_updates
    s._runtime_compatibility_error = None
    await s._route_incoming(stage(d, incoming("k3", text="默认")))
    assert s._group("thread-1") == "codex" and s._group("admin") == "c"
    await s._route_incoming(stage(d, incoming("k4", text="全部默认")))
    assert s._runtime_settings("random-thread").service_tier == "priority"
    d.close()


@pytest.mark.asyncio
async def test_global_rejects_collaborator_and_missing_key(tmp_path):
    s, d, c, g = service_for_test(tmp_path)
    s.claude.available = lambda: True
    await s._try_model_profile_command(
        stage(d, incoming("k", text="全部C组高级", open_id="not-owner"))
    )
    assert s._group("admin") == "codex"
    s.claude.available = lambda: False
    await s._try_model_profile_command(stage(d, incoming("k2", text="c组默认")))
    assert s._group("thread-1") == "codex"
    d.close()


@pytest.mark.asyncio
async def test_parallel_blind_scoped_original_once(tmp_path):
    s, d, c, g = service_for_test(tmp_path)
    old = job_for(s, d, tmp_path, "orig", "  原题\n\n逐字！  ")
    orig, _ = s._question_inputs(old, "thread-1", [])
    d.complete_incoming("orig")
    job = job_for(s, d, tmp_path, "follow", "做高级交叉验证", parent="orig")
    ctx, parts = s._question_inputs(job, "thread-1", [])
    assert FORMULA_RULE in parts[-1]["text"]
    assert action("这不是口令", ctx) is None
    answer = json.dumps(
        dict(
            yinshi_consult=ctx["nonce"], target="all", tier="advanced", source="quoted"
        )
    )
    active = ActiveTurn("thread-1", "real-id", "oc_thread", final_text=answer)
    d.upsert_turn_job(
        TurnJob(
            "follow",
            "thread-1",
            "real-id",
            "conversation",
            "oc_thread",
            None,
            "accepted",
        )
    )
    started = []
    gate = asyncio.Event()

    async def fake(group, preset, question, source, turn):
        started.append((group, preset.model, preset.effort, question))
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        return " 独立答案\n\n结束  "

    s._blind_answer = fake
    assert await s._group_final(active, "completed")
    assert {(v[0], v[2]) for v in started} == {("codex", "max"), ("c", "high")}
    assert all(v[-1] == orig["text"] for v in started)
    assert active.final_text.count(" 独立答案\n\n结束  ") == 2
    active.final_text = answer
    assert await s._group_final(active, "completed") and len(started) == 2
    assert s._group("thread-1") == "codex"
    assert len(list((tmp_path / "work" / "vibe_log").glob("*.md"))) == 1
    assert d.group_question("orig", "another-chat", "conversation") is None
    d.close()


def test_long_answer_newlines_and_log_symlink(tmp_path):
    s = "原题\n\n  保留\n" * 18000
    assert "".join(_split_utf8(s, 8192)) == s
    d = tmp_path / "vibe_log"
    d.mkdir()
    target = tmp_path / "keep"
    target.write_text("OK")
    (d / "x.md").symlink_to(target)
    with pytest.raises(OSError):
        append_journal(d / "x.md", "m", "c", "model", "q", "a")
    assert target.read_text() == "OK"
    assert "d" in registry(
        {
            "d": {
                "default": {"model": "claude-test", "effort": "low"},
                "advanced": {"model": "claude-test", "effort": "high"},
            }
        }
    )


@pytest.mark.asyncio
async def test_listener_starts_even_when_codex_binary_missing(tmp_path):
    s, d, c, g = service_for_test(tmp_path)
    started = []
    attempted = asyncio.Event()
    g.start_receivers = lambda: started.append(True)
    g.stop_receivers = lambda: None

    async def unavailable():
        attempted.set()
        raise FileNotFoundError("codex")

    async def close():
        pass

    c.start = unavailable
    c.close = close
    await s.start()
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        assert started and not s._codex_online
        s.claude.available = lambda: True
        await s._route_incoming(stage(d, incoming("online-c", text="C组高级")))
        assert s._group("thread-1") == "c" and s._group_level("thread-1") == "advanced"
    finally:
        await s.stop()
        d.close()
