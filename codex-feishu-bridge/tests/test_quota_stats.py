from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from codex_feishu_bridge.quota_stats import (
    QuotaSample,
    QuotaStatsError,
    daily_quota,
    format_percent,
    load_samples,
    parse_weekly_quota,
    read_weekly_quota,
    save_sample,
)

TIMEZONE = ZoneInfo("Asia/Shanghai")


def _sample(day: int, hour: int, remaining: float, reset_at: int = 100) -> QuotaSample:
    sampled_at = int(datetime(2026, 10, day, hour, 5, tzinfo=TIMEZONE).timestamp())
    return QuotaSample(sampled_at, remaining, reset_at, "account", "codex")


def test_parse_weekly_window_ignores_short_window() -> None:
    result = {
        "accountId": "account",
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": 66, "windowDurationMins": 10080, "resetsAt": 123},
                "secondary": {"usedPercent": 9, "windowDurationMins": 300, "resetsAt": 111},
            }
        },
    }
    assert parse_weekly_quota(result, 55) == QuotaSample(55, 34.0, 123, "account", "codex")
    with pytest.raises(QuotaStatsError, match="weekly"):
        parse_weekly_quota(
            {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 300}}}, 55
        )


@pytest.mark.asyncio
async def test_quota_sampling_uses_only_official_read_method(monkeypatch) -> None:
    methods: list[str] = []

    class FakeCodex:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> FakeCodex:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def request(self, method: str) -> dict:
            methods.append(method)
            return {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 25,
                        "windowDurationMins": 10080,
                        "resetsAt": 123,
                    },
                }
            }

    monkeypatch.setattr("codex_feishu_bridge.quota_stats.CodexAppServer", FakeCodex)
    sample = await read_weekly_quota("codex")
    assert sample.remaining_percent == 75
    assert methods == ["account/rateLimits/read"]


def test_daily_usage_accumulates_drops_and_resets() -> None:
    samples = [
        _sample(1, 23, 80),
        _sample(2, 0, 75),
        _sample(2, 1, 70),
        _sample(2, 2, 100, reset_at=200),
        _sample(2, 3, 95, reset_at=200),
        _sample(2, 4, 95, reset_at=200),
        _sample(2, 5, 90, reset_at=200),
    ]
    quota = daily_quota(samples, {date(2026, 10, 2)}, TIMEZONE)[date(2026, 10, 2)]
    assert quota.remaining_percent == 90
    assert quota.used_percent == 20


def test_rising_balance_is_a_reset_even_without_reset_timestamp_change() -> None:
    samples = [_sample(2, 0, 30), _sample(2, 1, 95), _sample(2, 2, 90)]
    quota = daily_quota(samples, {date(2026, 10, 2)}, TIMEZONE)[date(2026, 10, 2)]
    assert quota.used_percent == 10


def test_reset_timestamp_jitter_does_not_inflate_daily_usage() -> None:
    samples = [
        _sample(2, 23, 26, reset_at=100),
        _sample(3, 0, 26, reset_at=100),
        _sample(3, 1, 26, reset_at=101),
        _sample(3, 2, 26, reset_at=100),
        _sample(3, 3, 25, reset_at=101),
        _sample(3, 6, 100, reset_at=200),
    ]
    quota = daily_quota(samples, {date(2026, 10, 3)}, TIMEZONE)[date(2026, 10, 3)]
    assert quota.remaining_percent == 100
    assert quota.used_percent == 1


def test_quota_samples_persist_privately(tmp_path) -> None:
    path = tmp_path / "daily-stats-quota.json"
    save_sample(path, _sample(1, 23, 80), TIMEZONE)
    samples = save_sample(path, _sample(2, 0, 75), TIMEZONE)
    assert len(samples) == 2
    assert load_samples(path) == samples
    assert path.stat().st_mode & 0o777 == 0o600
    assert format_percent(34.0) == "34%"
    assert format_percent(34.5) == "34.5%"
