from __future__ import annotations

import subprocess

import pytest

from codex_feishu_bridge.heartbeat import (
    HeartbeatError,
    bridge_unit_active,
    failure_ping_url,
    heartbeat_once,
    send_ping,
    validate_ping_url,
)


HOST_URL = "https://hc-ping.com/11111111-2222-3333-4444-555555555555"
BRIDGE_URL = "https://hc-ping.com/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_validate_ping_url_requires_secret_https_path() -> None:
    assert validate_ping_url(HOST_URL) == HOST_URL
    for invalid in (
        "",
        "http://hc-ping.com/token",
        "https://hc-ping.com/",
        "https://user:pass@hc-ping.com/token",
        "https://hc-ping.com/token#fragment",
    ):
        with pytest.raises(HeartbeatError):
            validate_ping_url(invalid)


def test_failure_ping_url_preserves_query_without_exposing_it() -> None:
    value = HOST_URL + "?rid=123"
    assert failure_ping_url(value) == HOST_URL + "/fail?rid=123"


def test_host_probe_sends_success_url() -> None:
    sent: list[str] = []
    state = heartbeat_once(
        "host",
        environ={"HEALTHCHECKS_HOST_PING_URL": HOST_URL},
        sender=sent.append,
    )
    assert state == "up"
    assert sent == [HOST_URL]


def test_bridge_probe_sends_success_only_when_unit_is_active() -> None:
    sent: list[str] = []
    state = heartbeat_once(
        "bridge",
        environ={"HEALTHCHECKS_BRIDGE_PING_URL": BRIDGE_URL},
        unit_checker=lambda _unit: True,
        sender=sent.append,
    )
    assert state == "up"
    assert sent == [BRIDGE_URL]


def test_bridge_probe_sends_explicit_failure_when_unit_is_inactive() -> None:
    sent: list[str] = []
    state = heartbeat_once(
        "bridge",
        environ={"HEALTHCHECKS_BRIDGE_PING_URL": BRIDGE_URL},
        unit_checker=lambda _unit: False,
        sender=sent.append,
    )
    assert state == "down"
    assert sent == [BRIDGE_URL + "/fail"]


def test_missing_configuration_never_attempts_delivery() -> None:
    with pytest.raises(HeartbeatError, match="尚未配置"):
        heartbeat_once("host", environ={}, sender=lambda _url: pytest.fail())


def test_bridge_unit_active_uses_quiet_user_systemd_probe() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    assert bridge_unit_active("bridge.service", runner=runner)
    assert calls == [
        ["systemctl", "--user", "is-active", "--quiet", "bridge.service"]
    ]


def test_delivery_error_never_contains_secret_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(HOST_URL)

    monkeypatch.setattr("codex_feishu_bridge.heartbeat.urlopen", explode)
    with pytest.raises(HeartbeatError) as raised:
        send_ping(HOST_URL)
    assert HOST_URL not in str(raised.value)
