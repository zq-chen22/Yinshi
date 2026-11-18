from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _duration(seconds: float) -> str:
    if seconds <= 0:
        return "已到重置时间，等待刷新"
    if seconds < 60:
        return "不足1分钟"
    minutes = math.ceil(seconds / 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return "".join(
        f"{value}{unit}"
        for value, unit in [(days, "天"), (hours, "小时"), (minutes, "分钟")]
        if value
    )


def _windows(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    raw = result.get("rateLimitsByLimitId")
    buckets = dict(raw) if isinstance(raw, dict) else {}
    fallback = result.get("rateLimits")
    if isinstance(fallback, dict):
        buckets.setdefault(str(fallback.get("limitId") or "codex"), fallback)
    windows = []
    for identifier, bucket in sorted(
        buckets.items(), key=lambda item: item[0] != "codex"
    ):
        if not isinstance(bucket, dict):
            continue
        label = "Codex" if identifier == "codex" else str(identifier)[:60]
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowDurationMins")
            if _number(minutes) and minutes > 0:
                if minutes % 1440 == 0:
                    period = f"{minutes / 1440:g}天"
                elif minutes % 60 == 0:
                    period = f"{minutes / 60:g}小时"
                else:
                    period = f"{minutes:g}分钟"
            else:
                period = "主窗口" if name == "primary" else "次窗口"
            windows.append((f"{label} {period}", window))
    return windows


def format_account_quota(
    result: dict[str, Any], *, now: float, timezone_name: str = "Asia/Shanghai"
) -> str:
    try:
        timezone_info = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_info = timezone.utc
    lines = ["账户额度（账号共享，非单个对话独享）："]
    windows = _windows(result)
    if not windows:
        lines.append("• 剩余额度：未知（官方CLI未提供额度窗口）")
        lines.append("• 下次重置：未知")
    for label, window in windows:
        used = window.get("usedPercent")
        remaining = f"{100 - used:g}%" if _number(used) and 0 <= used <= 100 else "未知"
        lines.append(f"• {label}额度：剩余 {remaining}")
        resets_at = window.get("resetsAt")
        try:
            reset_time = (
                datetime.fromtimestamp(resets_at, timezone_info)
                if _number(resets_at) and resets_at > 0
                else None
            )
        except (OverflowError, OSError, ValueError):
            reset_time = None
        if reset_time is None:
            lines.append("  下次重置：未知")
        else:
            countdown = _duration(resets_at - now)
            lines.append(
                f"  下次重置：{reset_time:%Y-%m-%d %H:%M:%S %Z %z}（{countdown}）"
            )
    reset = result.get("rateLimitResetCredits")
    count = reset.get("availableCount") if isinstance(reset, dict) else None
    quantity = (
        f"{count}张"
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        else "未知（官方CLI未提供数量）"
    )
    lines.append(f"• 可用重置券：{quantity}")
    return "\n".join(lines)


class AccountQuotaStatus:
    def __init__(
        self,
        read: Callable[[], Awaitable[dict[str, Any]]],
        *,
        timezone_name: str = "Asia/Shanghai",
        cache_seconds: float = 30,
        retry_seconds: float = 10,
        request_timeout: float = 8,
    ) -> None:
        self._read = read
        self._timezone_name = timezone_name
        self._cache_seconds = cache_seconds
        self._retry_seconds = retry_seconds
        self._request_timeout = request_timeout
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._sampled_at: float | None = None
        self._expires_at = 0.0
        self._failed = False
        self._generation = 0

    def invalidate(self) -> None:
        self._generation += 1
        self._cached = None
        self._sampled_at = None
        self._expires_at = 0.0
        self._failed = False

    async def render(self) -> str:
        async with self._lock:
            if time.monotonic() >= self._expires_at:
                generation = self._generation
                try:
                    result = await asyncio.wait_for(
                        self._read(), timeout=self._request_timeout
                    )
                    if not isinstance(result, dict):
                        raise ValueError("Invalid rate-limit response")
                except Exception:
                    self._failed = True
                    self._expires_at = time.monotonic() + self._retry_seconds
                else:
                    if generation != self._generation:
                        return "账户登录状态已变化，请重新发送status查询额度。"
                    self._cached = result
                    self._sampled_at = time.time()
                    self._failed = False
                    lifetime = self._cache_seconds
                    for _, window in _windows(result):
                        resets_at = window.get("resetsAt")
                        if _number(resets_at) and resets_at > self._sampled_at:
                            lifetime = min(lifetime, resets_at - self._sampled_at)
                    self._expires_at = time.monotonic() + lifetime
            if self._cached is None:
                return (
                    "账户额度暂时无法读取（官方CLI查询失败，未发起模型对话）。\n"
                    "• 剩余额度：未知\n• 下次重置：未知\n• 可用重置券：未知"
                )
            rendered = format_account_quota(
                self._cached, now=time.time(), timezone_name=self._timezone_name
            )
            sampled = datetime.fromtimestamp(self._sampled_at, timezone.utc)
            if self._failed:
                prefix = f"⚠️ 额度刷新失败；以下为{sampled:%Y-%m-%d %H:%M:%S UTC}的缓存，非实时。\n"
                return prefix + rendered
            return (
                rendered
                + f"\n• 额度采样：{sampled:%Y-%m-%d %H:%M:%S UTC}（最多缓存30秒）"
            )
