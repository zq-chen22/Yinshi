from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import httpx

from .codex_client import CodexAppServer
from .config import DEFAULT_CONFIG_DIR, BridgeConfig, load_config
from .db import BridgeDB
from .feishu import FeishuGateway
from .models import ThreadSummary
from .service import BridgeService, generate_pairing_code


LOG = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="codex-feishu-bridge",
        description="用因时飞书组织监督和操控本机 Codex 对话",
    )
    result.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_DIR / "config.toml",
        help="配置文件路径（默认：%(default)s）",
    )
    result.add_argument("--verbose", action="store_true", help="输出调试日志（不会输出 Secret）")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建本地配置目录并登记最近 3 个 Codex 对话")
    commands.add_parser("doctor", help="检查 Codex、飞书凭据、配对和 Ubuntu 沙箱前置条件")
    commands.add_parser("recent", help="显示滚动最近 3 个 Codex 对话及绑定状态")
    commands.add_parser("pair-code", help="生成 15 分钟有效的 Codex 机器人配对码")
    commands.add_parser("bootstrap", help="凭据和配对就绪后创建待绑定的飞书对话群")
    commands.add_parser("run", help="以前台常驻方式运行长连接、执行和同步服务")
    return result


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        code = asyncio.run(_main(args))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


async def _main(args: argparse.Namespace) -> int:
    if args.command == "init":
        _ensure_local_files(args.config)
    config = load_config(args.config)
    db = BridgeDB(config.database_path)
    try:
        if args.command == "init":
            return await _init(config, db)
        if args.command == "doctor":
            return await _doctor(config, db)
        if args.command == "recent":
            return await _recent(config, db)
        if args.command == "pair-code":
            code, expires = generate_pairing_code(db, config.feishu.pairing_code_ttl_seconds)
            print(f"配对码：{code}")
            print(f"有效期至 Unix 时间 {expires}（约 {config.feishu.pairing_code_ttl_seconds // 60} 分钟）。")
            print("请只在因时组织中，私聊 Codex 机器人发送：")
            print(f"  配对 {code}")
            print("不要把 App Secret 粘贴到任何聊天中。")
            return 0
        if args.command == "bootstrap":
            return await _bootstrap(config, db)
        if args.command == "run":
            return await _run(config, db)
        raise AssertionError(args.command)
    finally:
        db.close()


async def _init(config: BridgeConfig, db: BridgeDB) -> int:
    codex = CodexAppServer(config.codex_bin)
    try:
        await codex.start()
        threads = await codex.list_threads(
            limit=max(20, config.initial_thread_count),
            source_kinds=config.source_kinds,
            sort_key="recency_at",
        )
        threads = _filter_internal_threads(threads, db, config)
        initial = threads[: config.initial_thread_count]
        for thread in initial:
            db.upsert_thread(thread, title=_suggest_title(thread))
    except Exception as error:
        print(f"初始化目录完成，但读取 Codex 对话失败：{error}", file=sys.stderr)
        return 1
    finally:
        await codex.close()
    print(f"配置：{config.config_path}")
    print(f"状态库：{config.database_path}")
    print("已登记最近的 Codex 对话：")
    _print_bindings(db)
    print("下一步：在 config.toml 填写 Codex App ID，并只在本机 secrets.env 填写 Secret。")
    return 0


async def _recent(config: BridgeConfig, db: BridgeDB) -> int:
    codex = CodexAppServer(config.codex_bin)
    try:
        await codex.start()
        threads = await codex.list_threads(
            limit=max(20, config.initial_thread_count),
            source_kinds=config.source_kinds,
            sort_key="recency_at",
        )
        threads = _filter_internal_threads(threads, db, config)
        for thread in threads[: config.initial_thread_count]:
            db.upsert_thread(thread, title=_suggest_title(thread))
    finally:
        await codex.close()
    _print_bindings(db)
    return 0


async def _bootstrap(config: BridgeConfig, db: BridgeDB) -> int:
    missing = _missing_feishu_config(config)
    if missing:
        print("无法 bootstrap：" + "；".join(missing), file=sys.stderr)
        print("先运行 pair-code，再启动 run，并在 Codex 机器人私聊中完成配对。", file=sys.stderr)
        return 2
    codex = CodexAppServer(config.codex_bin)
    gateway = FeishuGateway(config, db)
    service = BridgeService(config, db, codex, gateway)
    try:
        await codex.start()
        bindings = await service.reconcile_once()
    finally:
        await codex.close()
    ready = [item for item in db.list_bindings() if item.chat_id]
    print(f"bootstrap 完成：{len(ready)} 个飞书对话群已绑定，滚动最近集合为 {len(bindings)} 个。")
    _print_bindings(db)
    return 0


async def _run(config: BridgeConfig, db: BridgeDB) -> int:
    missing = _missing_app_credentials(config)
    if missing:
        print("无法启动：" + "；".join(missing), file=sys.stderr)
        return 2
    lock = _SingleInstanceLock(config.state_dir / "service.lock")
    try:
        lock.acquire()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 3
    codex = CodexAppServer(config.codex_bin)
    gateway = FeishuGateway(config, db)
    service = BridgeService(config, db, codex, gateway)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        await service.start()
        print("Codex 飞书桥已运行；按 Ctrl+C 停止。", flush=True)
        manual_stop = asyncio.create_task(stop_event.wait())
        service_stop = asyncio.create_task(service.wait_stopped())
        done, pending = await asyncio.wait(
            {manual_stop, service_stop}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if service_stop in done and service.fatal_error:
            LOG.error("Bridge stopped because a critical worker failed: %s", service.fatal_error)
            return_code = 1
        else:
            return_code = 0
    finally:
        await service.stop()
        lock.release()
    return return_code


async def _doctor(config: BridgeConfig, db: BridgeDB) -> int:
    checks: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        checks.append((level, name, detail))

    if config.config_path.exists():
        add("OK", "配置文件", str(config.config_path))
    else:
        add("FAIL", "配置文件", "不存在；先运行 init")
    codex_path = shutil.which(config.codex_bin)
    if codex_path:
        add("OK", "Codex CLI", codex_path)
        codex = CodexAppServer(config.codex_bin)
        try:
            await codex.start()
            threads = await codex.list_threads(limit=3, source_kinds=config.source_kinds)
            add("OK", "Codex App Server", f"可读取 {len(threads)} 个最近对话")
        except Exception as error:
            add("FAIL", "Codex App Server", str(error))
        finally:
            await codex.close()
    else:
        add("FAIL", "Codex CLI", f"找不到 {config.codex_bin}")

    app = config.feishu.conversation
    if not app.app_id:
        add("FAIL", "飞书 Codex App ID", "未填写")
    elif not app.secret():
        add("FAIL", "飞书 Codex Secret", f"环境变量 {app.app_secret_env} 未加载")
    else:
        ok, detail = await _verify_feishu_app(app.app_id, app.secret())
        add("OK" if ok else "FAIL", "飞书 Codex 凭据", detail)

    owner = (
        db.get_setting("owner_open_id:conversation", "")
        or config.feishu.owner_conversation_open_id
    )
    add("OK" if owner else "WARN", "Codex owner", "已配对" if owner else "尚未配对")

    if shutil.which("bwrap"):
        add("OK", "bubblewrap", shutil.which("bwrap") or "")
    else:
        add("FAIL", "bubblewrap", "未安装")
    profile = Path("/etc/apparmor.d/bwrap-userns-restrict")
    loaded_profiles = Path("/sys/kernel/security/apparmor/profiles")
    loaded = False
    with contextlib.suppress(OSError):
        profile_set = loaded_profiles.read_text(encoding="utf-8", errors="ignore")
        loaded = "bwrap (" in profile_set and "unpriv_bwrap (" in profile_set
    if not loaded and profile.exists() and shutil.which("aa-exec"):
        # Ubuntu can deny unprivileged reads of the loaded profile set even
        # though the sysfs file is world-readable.  A successful transition
        # is a non-privileged, side-effect-free way to confirm the profile is
        # actually loaded.
        with contextlib.suppress(OSError):
            loaded = subprocess.run(
                ["aa-exec", "-p", "bwrap", "--", "/usr/bin/true"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
    if profile.exists() and loaded:
        add("OK", "AppArmor profile", f"{profile}（已加载）")
    elif profile.exists():
        add("FAIL", "AppArmor profile", f"{profile} 存在但尚未加载")
    else:
        add(
            "FAIL",
            "AppArmor profile",
            "缺少 bwrap-userns-restrict；按 README/OpenAI 官方 sandbox 前置条件安装",
        )
    if config.approval_policy != "on-request":
        add("WARN", "审批策略", f"当前为 {config.approval_policy}；建议 on-request")
    else:
        add("OK", "审批策略", "on-request + reviewer=user")
    add("OK", "数据库", f"{config.database_path}；inbox={db.inbox_counts() or {'empty': 0}}")

    for level, name, detail in checks:
        print(f"[{level:<4}] {name}: {detail}")
    failures = sum(level == "FAIL" for level, _, _ in checks)
    warnings = sum(level == "WARN" for level, _, _ in checks)
    print(f"\n结果：{failures} 个失败，{warnings} 个提醒。")
    return 1 if failures else 0


AppRoleName = str


async def _verify_feishu_app(app_id: str, secret: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": secret},
            )
        payload = response.json()
        if response.is_success and payload.get("code") == 0:
            return True, "App ID/Secret 有效"
        return False, f"code={payload.get('code')} msg={payload.get('msg', '')}"
    except Exception as error:
        return False, f"无法验证：{error}"


def _missing_app_credentials(config: BridgeConfig) -> list[str]:
    missing: list[str] = []
    app = config.feishu.conversation
    if not app.app_id:
        missing.append("Codex App ID 未填写")
    if not app.secret():
        missing.append(f"{app.app_secret_env} 未加载")
    return missing


def _missing_feishu_config(config: BridgeConfig) -> list[str]:
    missing = _missing_app_credentials(config)
    db = BridgeDB(config.database_path)
    try:
        conversation = (
            db.get_setting("owner_open_id:conversation", "")
            or config.feishu.owner_conversation_open_id
        )
    finally:
        db.close()
    if not conversation:
        missing.append("Codex owner 尚未配对")
    return missing


def _ensure_local_files(config_path: Path) -> None:
    config_path = config_path.expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not config_path.exists():
        example = PROJECT_ROOT / "config.example.toml"
        if not example.exists():
            raise RuntimeError(f"找不到配置模板 {example}")
        config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        config_path.chmod(0o600)
    secret_path = config_path.parent / "secrets.env"
    if not secret_path.exists():
        secret_path.write_text(
            "FEISHU_CONVERSATION_APP_SECRET=\n",
            encoding="utf-8",
        )
        secret_path.chmod(0o600)


def _suggest_title(thread: ThreadSummary) -> str:
    if thread.thread_id in DEPLOYMENT_INITIAL_THREADS:
        return DEPLOYMENT_INITIAL_THREADS[thread.thread_id]
    if thread.name and thread.name.strip():
        return thread.name.strip()
    preview = thread.preview.lower()
    if "飞书" in preview and ("codex" in preview or "手机" in preview):
        return "飞书监督与操控 Codex"
    environment_terms = ("账户", "账号", "额度", "模型", "权限", "目录")
    if "codex" in preview and sum(term in preview for term in environment_terms) >= 2:
        return "Codex 账户与环境检查"
    return thread.display_name


def _filter_internal_threads(
    threads: list[ThreadSummary], db: BridgeDB, config: BridgeConfig
) -> list[ThreadSummary]:
    admin_scratch = config.admin_scratch_dir.expanduser().resolve(strict=False)
    return [
        thread
        for thread in threads
        if not (thread.name or "").startswith("因时管理员临时-")
        and db.get_setting(f"exclude_thread:{thread.thread_id}", "0") != "1"
        and Path(thread.cwd).expanduser().resolve(strict=False) != admin_scratch
    ]


def _print_bindings(db: BridgeDB) -> None:
    bindings = db.list_bindings()
    if not bindings:
        print("（暂无）")
        return
    for index, binding in enumerate(bindings, 1):
        state = f"飞书 chat={binding.chat_id}" if binding.chat_id else f"{binding.sync_state}/待创建"
        print(f"{index}. {binding.title}")
        print(f"   thread={binding.thread_id}")
        print(f"   cwd={binding.cwd}")
        print(f"   {state}")


class _SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: object | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise RuntimeError("已有一个 codex-feishu-bridge run 实例正在运行") from error
        self.handle = handle

    def release(self) -> None:
        if not self.handle:
            return
        handle = self.handle
        if os.name != "nt":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self.handle = None


if __name__ == "__main__":
    main()
