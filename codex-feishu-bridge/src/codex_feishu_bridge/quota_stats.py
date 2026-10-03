from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .codex_client import CodexAppServer

WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


class QuotaStatsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QuotaSample:
    sampled_at: int
    remaining_percent: float
    resets_at: int | None
    account_id: str
    limit_id: str


@dataclass(frozen=True, slots=True)
class DailyQuota:
    day: date
    remaining_percent: float
    used_percent: float


def _weekly_window(bucket: dict[str, Any]) -> dict[str, Any] | None:
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if isinstance(window, dict) and window.get("windowDurationMins") == WEEKLY_WINDOW_MINUTES:
            return window
    return None


def parse_weekly_quota(result: dict[str, Any], sampled_at: int) -> QuotaSample:
    buckets = result.get("rateLimitsByLimitId")
    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(buckets, dict):
        candidates.extend(
            (str(identifier), bucket)
            for identifier, bucket in buckets.items()
            if isinstance(bucket, dict)
        )
    fallback = result.get("rateLimits")
    if isinstance(fallback, dict):
        candidates.append((str(fallback.get("limitId") or "codex"), fallback))
    weekly = [
        (identifier, window)
        for identifier, bucket in candidates
        if (window := _weekly_window(bucket)) is not None
    ]
    selected = next((item for item in weekly if item[0] == "codex"), None)
    if selected is None and len(weekly) == 1:
        selected = weekly[0]
    if selected is None:
        raise QuotaStatsError("Codex CLI did not return one unambiguous weekly quota window")
    limit_id, window = selected
    used = window.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise QuotaStatsError("Codex CLI weekly usedPercent is missing")
    used_percent = float(used)
    if not math.isfinite(used_percent) or not 0 <= used_percent <= 100:
        raise QuotaStatsError("Codex CLI weekly usedPercent is invalid")
    resets_at = window.get("resetsAt")
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, type(None))):
        resets_at = None
    return QuotaSample(
        sampled_at=sampled_at,
        remaining_percent=round(100 - used_percent, 4),
        resets_at=resets_at,
        account_id=str(result.get("accountId") or ""),
        limit_id=limit_id,
    )


async def read_weekly_quota(codex_bin: str) -> QuotaSample:
    async with CodexAppServer(codex_bin, request_timeout=20) as client:
        result = await client.request("account/rateLimits/read")
    return parse_weekly_quota(result, int(time.time()))


def load_samples(path: Path) -> list[QuotaSample]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != 1
            or not isinstance(payload.get("samples"), list)
        ):
            raise ValueError("unexpected schema")
        samples = [QuotaSample(**item) for item in payload["samples"]]
        for sample in samples:
            if (
                not math.isfinite(sample.remaining_percent)
                or not 0 <= sample.remaining_percent <= 100
            ):
                raise ValueError("invalid remaining percentage")
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise QuotaStatsError(f"cannot read quota sample state: {path}") from error
    return sorted(samples, key=lambda item: item.sampled_at)


def save_sample(path: Path, sample: QuotaSample, timezone_info: ZoneInfo) -> list[QuotaSample]:
    samples = load_samples(path)
    if samples and (
        samples[-1].limit_id != sample.limit_id
        or (
            samples[-1].account_id
            and sample.account_id
            and samples[-1].account_id != sample.account_id
        )
    ):
        raise QuotaStatsError("Codex account or weekly quota bucket changed; refusing to mix usage")
    cutoff_day = datetime.fromtimestamp(sample.sampled_at, timezone_info).date() - timedelta(days=2)
    samples = [
        item
        for item in samples
        if datetime.fromtimestamp(item.sampled_at, timezone_info).date() >= cutoff_day
    ]
    samples.append(sample)
    samples.sort(key=lambda item: item.sampled_at)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema": 1, "samples": [asdict(item) for item in samples]}) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
    return samples


def daily_quota(
    samples: list[QuotaSample], targets: set[date], timezone_info: ZoneInfo
) -> dict[date, DailyQuota]:
    result: dict[date, DailyQuota] = {}
    ordered = sorted(samples, key=lambda item: item.sampled_at)
    for index, sample in enumerate(ordered):
        day = datetime.fromtimestamp(sample.sampled_at, timezone_info).date()
        if day not in targets:
            continue
        previous = ordered[index - 1] if index else None
        increment = 0.0
        if (
            previous
            and previous.limit_id == sample.limit_id
            and previous.account_id == sample.account_id
        ):
            reset = sample.remaining_percent > previous.remaining_percent
            increment = (
                100 - sample.remaining_percent
                if reset
                else previous.remaining_percent - sample.remaining_percent
            )
        accumulated = result[day].used_percent if day in result else 0.0
        result[day] = DailyQuota(day, sample.remaining_percent, accumulated + increment)
    return result


def format_percent(value: float) -> str:
    return f"{value:.0f}%" if value.is_integer() else f"{value:.1f}%"
