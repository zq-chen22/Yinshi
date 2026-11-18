from __future__ import annotations

import asyncio
import time

import pytest

from codex_feishu_bridge.account_status import AccountQuotaStatus, format_account_quota


def quota(used: float = 75, count: int = 2, resets_at: float = 1795007850) -> dict:
    return {
        "rateLimits": {
            "limitId": "codex",
            "primary": {
                "usedPercent": used,
                "windowDurationMins": 10080,
                "resetsAt": resets_at,
            },
        },
        "rateLimitResetCredits": {"availableCount": count, "credits": []},
    }


def test_weekly_quota_countdown_and_authoritative_credit_count() -> None:
    result = quota(count=7)
    result["rateLimitResetCredits"]["credits"] = [{"id": "not-displayed"}]
    rendered = format_account_quota(result, now=1795007850 - 90120)
    assert "Codex 7天额度：剩余 25%" in rendered
    assert "2026-11-18 21:17:30 CST +0800" in rendered
    assert "1天1小时2分钟" in rendered
    assert "可用重置券：7张" in rendered
    assert "not-displayed" not in rendered
    assert "账号共享" in rendered


def test_multiple_windows_and_buckets_are_distinct_and_not_duplicated() -> None:
    result = quota(used=100, count=0)
    standard = result["rateLimits"]
    standard["secondary"] = {
        "usedPercent": 20.5,
        "windowDurationMins": 300,
        "resetsAt": None,
    }
    result["rateLimitsByLimitId"] = {
        "codex": standard,
        "codex_spark": {"primary": {"usedPercent": 0, "windowDurationMins": 60}},
    }
    rendered = format_account_quota(result, now=0)
    assert rendered.count("Codex 7天额度") == 1
    assert "剩余 0%" in rendered
    assert "Codex 5小时额度：剩余 79.5%" in rendered
    assert "codex_spark 1小时额度：剩余 100%" in rendered
    assert "可用重置券：0张" in rendered


@pytest.mark.parametrize("used", [True, -1, 101, float("nan"), "25", None])
def test_invalid_percent_is_unknown(used) -> None:
    rendered = format_account_quota(quota(used=used), now=0)
    assert "剩余 未知" in rendered


@pytest.mark.parametrize("count", [None, True, -1, "2"])
def test_missing_or_invalid_credit_count_does_not_become_zero(count) -> None:
    rendered = format_account_quota(quota(count=count), now=0)
    assert "可用重置券：未知" in rendered


@pytest.mark.parametrize("reset", [None, True, -1, float("inf"), "tomorrow", 10**30])
def test_invalid_reset_time_is_unknown(reset) -> None:
    rendered = format_account_quota(quota(resets_at=reset), now=0)
    assert "下次重置：未知" in rendered


def test_past_reset_does_not_render_negative_countdown_or_claim_reset_succeeded() -> (
    None
):
    rendered = format_account_quota(quota(resets_at=100), now=200, timezone_name="UTC")
    assert "已到重置时间，等待刷新" in rendered
    assert "剩余 25%" in rendered
    assert "-100" not in rendered


def test_absent_fields_and_invalid_timezone_are_safe() -> None:
    rendered = format_account_quota({}, now=0)
    assert "剩余额度：未知" in rendered
    assert "可用重置券：未知" in rendered
    assert "UTC +0000" in format_account_quota(
        quota(), now=0, timezone_name="not/a/zone"
    )


@pytest.mark.asyncio
async def test_concurrent_status_reads_share_one_rpc_and_cache() -> None:
    calls = []

    async def read():
        calls.append(True)
        await asyncio.sleep(0.01)
        return quota(resets_at=time.time() + 3600)

    status = AccountQuotaStatus(read)
    results = await asyncio.gather(*(status.render() for _ in range(20)))
    assert len(calls) == 1
    assert all("剩余 25%" in rendered for rendered in results)
    assert "最多缓存30秒" in results[0]
    status._expires_at = 0
    await status.render()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_error_is_redacted_backed_off_and_keeps_stale_data_labelled() -> None:
    calls = []

    async def read():
        calls.append(True)
        if len(calls) > 1:
            raise RuntimeError("private-token-value-must-not-appear")
        return quota()

    status = AccountQuotaStatus(read)
    await status.render()
    status._expires_at = 0
    rendered = await status.render()
    assert "额度刷新失败" in rendered
    assert "缓存，非实时" in rendered
    assert "剩余 25%" in rendered
    assert "private-token" not in rendered
    await status.render()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_first_failure_and_timeout_preserve_unknown_credit_count() -> None:
    async def read():
        await asyncio.Event().wait()

    status = AccountQuotaStatus(read, request_timeout=0.01)
    rendered = await status.render()
    assert "暂时无法读取" in rendered
    assert "可用重置券：未知" in rendered
    assert "可用重置券：0" not in rendered


@pytest.mark.asyncio
async def test_cache_expires_no_later_than_reset_boundary() -> None:
    async def read():
        return quota(resets_at=time.time() + 0.1)

    status = AccountQuotaStatus(read)
    await status.render()
    assert status._expires_at - time.monotonic() <= 0.1


@pytest.mark.asyncio
async def test_account_change_does_not_reuse_old_account_data() -> None:
    calls = []

    async def read():
        calls.append(True)
        return quota(count=len(calls))

    status = AccountQuotaStatus(read)
    assert "可用重置券：1张" in await status.render()
    status.invalidate()
    assert "可用重置券：2张" in await status.render()


@pytest.mark.asyncio
async def test_account_change_during_rpc_drops_inflight_old_result() -> None:
    started = asyncio.Event()
    complete = asyncio.Event()

    async def read():
        started.set()
        await complete.wait()
        return quota(count=99)

    status = AccountQuotaStatus(read)
    task = asyncio.create_task(status.render())
    await started.wait()
    status.invalidate()
    complete.set()
    rendered = await task
    assert "登录状态已变化" in rendered
    assert "99张" not in rendered
    assert status._cached is None
