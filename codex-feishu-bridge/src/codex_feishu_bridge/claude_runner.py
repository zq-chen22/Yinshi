"""Official Claude Code only. No HTTP impersonation, fallback model or Codex turn."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

import httpx

from .model_profiles import ModelProfile


class ClaudeError(RuntimeError):
    pass


def load_key(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
            or st.st_size > 1024
        ):
            raise ClaudeError("Claude 密钥文件必须是本人所有的私有普通文件")
        raw = f.read().decode("utf-8-sig").strip()
    if not re.fullmatch(r"sk-[a-zA-Z0-9_-]{12,512}", raw):
        raise ClaudeError("Claude 密钥格式无效")
    return raw


def content_blocks(inputs: list[dict], image_root: Path) -> list[dict]:
    blocks = []
    for item in inputs:
        if item["type"] == "text":
            blocks.append({"type": "text", "text": item["text"]})
        elif item["type"] == "localImage":
            p = Path(item["path"])
            if (
                p.is_symlink()
                or not p.resolve(strict=True).is_relative_to(image_root.resolve())
                or p.stat().st_size > 5_000_000
            ):
                raise ClaudeError("只允许已验证的图片代理")
            data = p.read_bytes()
            if not data.startswith(b"\xff\xd8"):
                raise ClaudeError("图片代理不是 JPEG")
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(data).decode(),
                    },
                }
            )
        else:
            raise ClaudeError("不支持的输入类型")
    return blocks


class ClaudeRun:
    def __init__(self):
        self.process = None
        self.pending: list[dict] = []
        self.extra_mounts: set[Path] = set()
        self.stopped = self.closed = self.initialized = False

    def terminate(self):
        if self.process and self.process.returncode is None:
            proc = self.process
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

            def kill_if_needed():
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()

            asyncio.get_running_loop().call_later(5, kill_if_needed)

    def stop(self):
        self.stopped = True
        self.pending.clear()
        self.terminate()

    def steer(self, blocks: list[dict], mounts=()) -> bool:
        if (
            self.closed
            or self.stopped
            or (self.process and self.process.returncode is not None)
        ):
            return False
        self.pending.extend(blocks)
        self.extra_mounts.update(mounts)
        if self.initialized:
            self.terminate()
        return True


class ClaudeBackend:
    def __init__(self, config):
        self.config = config
        self.key_path = config.config_path.parent / "claude-4router.key"
        self.binary = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        self.running: dict[str, ClaudeRun] = {}

    async def list_models(self) -> list[str]:
        """Metadata only, same fixed host as the native client. Never follow redirects."""
        key = load_key(self.key_path)
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            response = await client.get(
                "https://4router.net/v1/models",
                headers={"Authorization": "Bearer " + key},
            )
            if response.status_code != 200:
                raise ClaudeError("当前无法读取 C 组目录")
            entries = response.json().get("data")
            if not isinstance(entries, list):
                raise ClaudeError("C 组目录格式无效")
            return list(
                dict.fromkeys(
                    x["id"]
                    for x in entries
                    if isinstance(x, dict)
                    and isinstance(x.get("id"), str)
                    and re.fullmatch(r"claude-[a-z0-9-]{1,80}", x["id"])
                )
            )

    def available(self) -> bool:
        try:
            return bool(load_key(self.key_path)) and os.access(self.binary, os.X_OK)
        except (OSError, ValueError, ClaudeError):
            return False

    async def run(
        self,
        run_id,
        preset: ModelProfile,
        blocks,
        *,
        cwd: Path,
        session: str,
        resume: bool,
        persistent: bool,
        profile: str,
        mounts: list[Path],
        outbox: Path | None,
        on_event,
        on_session,
        system: str = "",
    ) -> str:
        key = load_key(self.key_path)
        control = ClaudeRun()
        self.running[run_id] = control
        # Per-conversation native state. No plugins, OAuth credentials or settings
        # from a terminal user's Claude profile enter this process.
        with tempfile.TemporaryDirectory(prefix="yinshi-cc-") as tmp:
            home = Path(tmp)
            native = (
                self.config.state_dir
                / "claude-sessions"
                / hashlib.sha256(session.encode()).hexdigest()[:24]
                if persistent
                else home / "native"
            )
            native.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                while not control.stopped:
                    env = {
                        k: v
                        for k, v in os.environ.items()
                        if k.lower()
                        in {"https_proxy", "all_proxy", "no_proxy", "http_proxy"}
                    }
                    env.update(
                        PATH="/usr/local/bin:/usr/bin:/bin",
                        HOME=str(home),
                        LANG="C.UTF-8",
                        CLAUDE_CONFIG_DIR=str(native),
                        ANTHROPIC_BASE_URL="https://4router.net",
                        ANTHROPIC_API_KEY=key,
                        DISABLE_AUTOUPDATER="1",
                        DISABLE_TELEMETRY="1",
                        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                    )
                    # Use the genuine native client. Some Claude-Code-only gateways
                    # reject even its official bare/SDK profile for specific models.
                    # Isolate user config, plugin/skill commands and MCP separately.
                    args = [
                        self.binary,
                        "-p",
                        "--setting-sources",
                        "",
                        "--strict-mcp-config",
                        "--disable-slash-commands",
                        "--model",
                        preset.model,
                        "--input-format",
                        "stream-json",
                        "--output-format",
                        "stream-json",
                        "--verbose",
                        "--include-partial-messages",
                    ]
                    if preset.effort:
                        args += ["--effort", preset.effort]
                    if persistent:
                        args += ["--resume" if resume else "--session-id", session]
                    elif resume:
                        args += ["--resume", session]
                    else:
                        args += ["--session-id", session]
                    if system:
                        args += ["--append-system-prompt", system]
                    if profile == "blind":
                        args += [
                            "--tools",
                            "",
                            "--max-budget-usd",
                            "0.30",
                            "--no-session-persistence",
                        ]
                    elif profile == "full":
                        args += [
                            "--tools",
                            "Read,Glob,Grep,Write,Edit,Bash",
                            "--permission-mode",
                            "bypassPermissions",
                        ]
                    else:
                        args = self._sandbox(
                            args,
                            env,
                            cwd,
                            home,
                            native,
                            [*mounts, *control.extra_mounts],
                            outbox,
                            profile,
                        )
                    result = await self._one(
                        control,
                        args,
                        env,
                        cwd,
                        blocks,
                        on_event,
                        on_session,
                        preset.model,
                    )
                    if control.stopped:
                        break
                    if control.pending:
                        # Interrupt now, not after the old answer; resume the same
                        # native conversation and keep one bridge job/card/final.
                        if not control.initialized:
                            raise ClaudeError("Claude 尚未建立可续接会话；补充未执行")
                        blocks, control.pending = control.pending, []
                        blocks = [
                            {
                                "type": "text",
                                "text": "继续同一项未完成的任务。以下是用户刚追加的信息：",
                            },
                            *blocks,
                        ]
                        resume = True
                        control.initialized = False
                        on_event("正在同一 Claude 会话中处理追加信息", "")
                        continue
                    if not result:
                        raise ClaudeError(
                            "Claude Code 未返回成功结果，请检查渠道、模型权限或余额。"
                        )
                    return result
                raise ClaudeError("已按要求停止当前任务")
            finally:
                control.closed = True
                self.running.pop(run_id, None)

    def _sandbox(self, args, env, cwd, home, native, mounts, outbox, profile):
        # Do not translate a restricted Codex sandbox into host-wide Bash/edit.
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise ClaudeError("此权限模式需要 bubblewrap；没有降级到主机全权限")
        cmd = [
            bwrap,
            "--die-with-parent",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/etc",
            "/etc",
            "--tmpfs",
            "/tmp",
        ]
        resolver = Path("/etc/resolv.conf").resolve()
        if resolver.is_file() and not resolver.is_relative_to("/etc"):
            cmd += ["--ro-bind", str(resolver), str(resolver)]
        for name in ("bin", "lib", "lib64", "sbin"):
            p = Path("/") / name
            if p.is_symlink():
                cmd += ["--symlink", os.readlink(p), str(p)]
            elif p.exists():
                cmd += ["--ro-bind", str(p), str(p)]
        writable = profile == "workspace-write"
        cmd += ["--bind" if writable else "--ro-bind", str(cwd), str(cwd)]
        for private in (
            Path.home() / ".ssh",
            Path.home() / ".codex",
            Path.home() / ".claude",
            Path.home() / ".gnupg",
            Path.home() / ".aws",
            Path.home() / ".local/share/keyrings",
            Path.home() / ".config",
            self.config.state_dir,
        ):
            if private.is_dir() and private.is_relative_to(cwd):
                cmd += ["--tmpfs", str(private)]
        for private in (
            Path.home() / ".git-credentials",
            Path.home() / ".netrc",
            Path.home() / ".claude.json",
        ):
            if private.is_file() and private.is_relative_to(cwd):
                cmd += ["--ro-bind", "/dev/null", str(private)]
        cmd += [
            "--ro-bind",
            str(Path(self.binary).resolve()),
            "/opt/bridge-claude",
            "--bind",
            str(native),
            "/opt/bridge-state",
            "--dir",
            "/tmp/bridge-home",
        ]
        for m in mounts:
            cmd += ["--ro-bind", str(m), str(m)]
        if writable and outbox:
            cmd += ["--bind", str(outbox), str(outbox)]
        env.update(HOME="/tmp/bridge-home", CLAUDE_CONFIG_DIR="/opt/bridge-state")
        tools = "Read,Glob,Grep,Write,Edit" if writable else "Read,Glob,Grep"
        return [
            *cmd,
            "--chdir",
            str(cwd),
            "--",
            "/opt/bridge-claude",
            *args[1:],
            "--permission-mode",
            "dontAsk",
            "--tools",
            tools,
            "--allowedTools",
            tools,
        ]

    async def _one(self, control, args, env, cwd, blocks, on_event, on_session, model):
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=12 * 1024 * 1024,
        )
        control.process = proc
        try:
            if control.stopped:
                control.terminate()
                return None
            frame = {
                "type": "user",
                "message": {"role": "user", "content": blocks},
                "parent_tool_use_id": None,
            }
            proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            answer = None
            seen_models = set()
            native_error = None
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "system" and event.get("subtype") == "init":
                    control.initialized = True
                    on_session(str(event.get("session_id", "")))
                    if control.pending or control.stopped:
                        control.terminate()
                if kind == "assistant":
                    msg = event.get("message") or {}
                    if msg.get("model") and msg["model"] != "<synthetic>":
                        seen_models.add(msg["model"])
                    for part in msg.get("content") or []:
                        if part.get("type") == "tool_use":
                            on_event("工具：" + str(part.get("name", "")), "")
                        if part.get("type") == "text":
                            val = str(part.get("text", ""))
                            if msg.get("model") == "<synthetic>":
                                status = re.match(r"API Error:\s*(\d{3})\b", val)
                                if status:
                                    native_error = (
                                        "Claude 渠道拒绝本次请求（HTTP "
                                        + status[1]
                                        + "）；未重试或替换模型"
                                    )
                            else:
                                on_event("", val[-1800:])
                if kind == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        on_event("正在输出回复", None)
                if (
                    kind == "result"
                    and event.get("subtype") == "success"
                    and not event.get("is_error")
                ):
                    answer = event.get("result")
                    seen_models.update((event.get("modelUsage") or {}).keys())
            rc = await proc.wait()
            if native_error and not control.pending and not control.stopped:
                raise ClaudeError(native_error)
            if (
                answer
                and model not in seen_models
                and not control.pending
                and not control.stopped
            ):
                raise ClaudeError("Claude 返回型号和所选档位不一致，未静默替换")
            return answer if rc == 0 and isinstance(answer, str) else None
        finally:
            if proc.returncode is None:
                control.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            control.process = None
