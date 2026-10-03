"""Send privacy-preserving host and bridge heartbeats to a managed monitor."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import Request, urlopen

TARGET_ENV = {
    "host": "HEALTHCHECKS_HOST_PING_URL",
    "bridge": "HEALTHCHECKS_BRIDGE_PING_URL",
}
DEFAULT_BRIDGE_UNIT = "codex-feishu-bridge.service"
DEFAULT_TIMEOUT_SECONDS = 10.0


class HeartbeatError(RuntimeError):
    """A heartbeat could not be validated or delivered."""


def validate_ping_url(value: str) -> str:
    """Validate a secret ping URL without ever rendering it in an error."""

    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path in {"", "/"}
    ):
        raise HeartbeatError(
            "ping URL 必须是无账号信息、无 fragment 且包含私有路径的 HTTPS URL"
        )
    return candidate


def failure_ping_url(value: str) -> str:
    """Return Healthchecks.io's explicit failure endpoint for ``value``."""

    parsed = urlsplit(validate_ping_url(value))
    failed = SplitResult(
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/") + "/fail",
        parsed.query,
        "",
    )
    return urlunsplit(failed)


def bridge_unit_active(
    unit: str = DEFAULT_BRIDGE_UNIT,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bool:
    """Return whether the user-level bridge unit is currently active."""

    try:
        result = runner(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def send_ping(value: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """POST an empty heartbeat and reject non-success responses."""

    url = validate_ping_url(value)
    request = Request(
        url,
        data=b"",
        method="POST",
        headers={"User-Agent": "codex-feishu-bridge-heartbeat/1"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw_status = getattr(response, "status", None)
            if raw_status is None:
                raw_status = response.getcode()
            status = int(raw_status)
    except Exception as error:
        raise HeartbeatError(
            f"heartbeat 请求失败（{type(error).__name__}）"
        ) from None
    if not 200 <= status < 300:
        raise HeartbeatError(f"heartbeat 服务返回 HTTP {status}")


def heartbeat_once(
    target: str,
    *,
    environ: Mapping[str, str] | None = None,
    bridge_unit: str = DEFAULT_BRIDGE_UNIT,
    unit_checker: Callable[[str], bool] = bridge_unit_active,
    sender: Callable[[str], None] = send_ping,
) -> str:
    """Send one heartbeat and return ``up`` or ``down``."""

    if target not in TARGET_ENV:
        raise HeartbeatError(f"未知 heartbeat target：{target}")
    source = os.environ if environ is None else environ
    variable = TARGET_ENV[target]
    value = source.get(variable, "")
    if not value.strip():
        raise HeartbeatError(f"环境变量 {variable} 尚未配置")
    url = validate_ping_url(value)

    if target == "bridge" and not unit_checker(bridge_unit):
        sender(failure_ping_url(url))
        return "down"

    sender(url)
    return "up"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=sorted(TARGET_ENV))
    parser.add_argument("--bridge-unit", default=DEFAULT_BRIDGE_UNIT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证配置和本机状态，不发送网络请求",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        variable = TARGET_ENV[args.target]
        value = os.environ.get(variable, "")
        validate_ping_url(value)
        if args.dry_run:
            state = (
                "down"
                if args.target == "bridge"
                and not bridge_unit_active(args.bridge_unit)
                else "up"
            )
        else:
            state = heartbeat_once(args.target, bridge_unit=args.bridge_unit)
    except HeartbeatError as error:
        print(f"heartbeat 失败：{error}", file=sys.stderr)
        return 2
    print(f"heartbeat {args.target}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
