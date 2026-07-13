from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "codex-feishu-bridge"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "codex-feishu-bridge"


@dataclass(slots=True)
class FeishuAppConfig:
    app_id: str = ""
    app_secret_env: str = ""

    def secret(self) -> str:
        return os.environ.get(self.app_secret_env, "") if self.app_secret_env else ""

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.secret())


@dataclass(slots=True)
class FeishuConfig:
    admin: FeishuAppConfig = field(
        default_factory=lambda: FeishuAppConfig(app_secret_env="FEISHU_ADMIN_APP_SECRET")
    )
    conversation: FeishuAppConfig = field(
        default_factory=lambda: FeishuAppConfig(app_secret_env="FEISHU_CONVERSATION_APP_SECRET")
    )
    owner_open_id: str = ""
    owner_admin_open_id: str = ""
    owner_conversation_open_id: str = ""
    owner_user_id: str = ""
    owner_union_id: str = ""
    pairing_code_ttl_seconds: int = 900


@dataclass(slots=True)
class BridgeConfig:
    config_path: Path
    state_dir: Path = DEFAULT_STATE_DIR
    database_path: Path = DEFAULT_STATE_DIR / "bridge.sqlite"
    inbox_dir: Path = DEFAULT_STATE_DIR / "inbox"
    outbox_dir: Path = DEFAULT_STATE_DIR / "outbox"
    admin_scratch_dir: Path = DEFAULT_STATE_DIR / "admin-scratch"
    managed_workspaces_dir: Path = DEFAULT_STATE_DIR / "workspaces"
    codex_bin: str = "codex"
    initial_thread_count: int = 3
    sync_interval_seconds: int = 15
    progress_update_seconds: float = 2.0
    group_suffix: str = "-Codex主机"
    auto_discover_new_threads: bool = True
    source_kinds: list[str] = field(
        default_factory=lambda: ["cli", "vscode", "appServer", "unknown"]
    )
    model: str | None = None
    model_reasoning_effort: str | None = None
    service_tier: str | None = None
    approval_policy: str = "on-request"
    sandbox: str = "workspace-write"
    allowed_workspace_roots: list[Path] = field(default_factory=lambda: [Path.home()])
    max_download_bytes: int = 50 * 1024 * 1024
    max_upload_bytes: int = 30 * 1024 * 1024
    feishu: FeishuConfig = field(default_factory=FeishuConfig)

    def prepare_dirs(self) -> None:
        for path in (
            self.state_dir,
            self.inbox_dir,
            self.outbox_dir,
            self.admin_scratch_dir,
            self.managed_workspaces_dir,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)


def _path(value: str | Path | None, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default


def load_config(path: str | Path | None = None) -> BridgeConfig:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_DIR / "config.toml"
    raw: dict = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

    bridge = raw.get("bridge", {})
    feishu = raw.get("feishu", {})
    admin = feishu.get("admin", {})
    conversation = feishu.get("conversation", {})
    state_dir = _path(bridge.get("state_dir"), DEFAULT_STATE_DIR)

    cfg = BridgeConfig(
        config_path=config_path,
        state_dir=state_dir,
        database_path=_path(bridge.get("database_path"), state_dir / "bridge.sqlite"),
        inbox_dir=_path(bridge.get("inbox_dir"), state_dir / "inbox"),
        outbox_dir=_path(bridge.get("outbox_dir"), state_dir / "outbox"),
        admin_scratch_dir=_path(
            bridge.get("admin_scratch_dir"), state_dir / "admin-scratch"
        ),
        managed_workspaces_dir=_path(
            bridge.get("managed_workspaces_dir"), state_dir / "workspaces"
        ),
        codex_bin=str(bridge.get("codex_bin", "codex")),
        initial_thread_count=int(bridge.get("initial_thread_count", 3)),
        sync_interval_seconds=int(bridge.get("sync_interval_seconds", 15)),
        progress_update_seconds=float(bridge.get("progress_update_seconds", 2.0)),
        group_suffix=str(bridge.get("group_suffix", "-Codex主机")),
        auto_discover_new_threads=bool(bridge.get("auto_discover_new_threads", True)),
        source_kinds=list(
            bridge.get("source_kinds", ["cli", "vscode", "appServer", "unknown"])
        ),
        model=str(bridge["model"]) if bridge.get("model") else None,
        model_reasoning_effort=(
            str(bridge["model_reasoning_effort"])
            if bridge.get("model_reasoning_effort")
            else None
        ),
        service_tier=str(bridge["service_tier"]) if bridge.get("service_tier") else None,
        approval_policy=str(bridge.get("approval_policy", "on-request")),
        sandbox=str(bridge.get("sandbox", "workspace-write")),
        allowed_workspace_roots=[
            _path(item, Path.home()) for item in bridge.get("allowed_workspace_roots", [str(Path.home())])
        ],
        max_download_bytes=int(bridge.get("max_download_bytes", 50 * 1024 * 1024)),
        max_upload_bytes=int(bridge.get("max_upload_bytes", 30 * 1024 * 1024)),
        feishu=FeishuConfig(
            admin=FeishuAppConfig(
                app_id=str(admin.get("app_id", "")),
                app_secret_env=str(admin.get("app_secret_env", "FEISHU_ADMIN_APP_SECRET")),
            ),
            conversation=FeishuAppConfig(
                app_id=str(conversation.get("app_id", "")),
                app_secret_env=str(
                    conversation.get("app_secret_env", "FEISHU_CONVERSATION_APP_SECRET")
                ),
            ),
            owner_open_id=str(feishu.get("owner_open_id", "")),
            owner_admin_open_id=str(
                feishu.get("owner_admin_open_id", feishu.get("owner_open_id", ""))
            ),
            owner_conversation_open_id=str(
                feishu.get("owner_conversation_open_id", feishu.get("owner_open_id", ""))
            ),
            owner_user_id=str(feishu.get("owner_user_id", "")),
            owner_union_id=str(feishu.get("owner_union_id", "")),
            pairing_code_ttl_seconds=int(feishu.get("pairing_code_ttl_seconds", 900)),
        ),
    )
    cfg.prepare_dirs()
    return cfg
