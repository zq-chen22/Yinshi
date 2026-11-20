"""Model group integration; the Feishu listener and outbox have only one owner."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .claude_runner import ClaudeBackend, ClaudeError, content_blocks
from .groups import (
    FORMULA_RULE,
    SECRET,
    action,
    append_journal,
    log_name,
    registry,
    rules,
)
from .model_profiles import ModelProfile, load_profiles, profile_for_scope
from .models import ActiveTurn, TurnJob
from .privacy import redact_log


class GroupService:
    def _init_groups(self):
        self.groups = registry(self.config.group_definitions)
        self.claude = ClaudeBackend(self.config)
        self._codex_online = True
        self._consult_tasks = {}
        self._picker_cache = {}
        self._picker_locks = {}

    def _group(self, scope):
        return self.db.get_setting(f"runtime:{scope}:model_group", "codex")

    def _group_level(self, scope):
        return self.db.get_setting(f"runtime:{scope}:group_level", "default")

    def _claude_profile(self, scope):
        group = self._group(scope)
        base = self.groups[group][self._group_level(scope)]
        model = self.db.get_setting(f"runtime:{scope}:group_model", "")
        return ModelProfile(model, "") if model and group == "c" else base

    def _group_description(self, scope):
        group = self._group(scope)
        if group not in self.groups:
            return "Codex"
        preset = self._claude_profile(scope)
        return f"{group.upper()}组 · {preset.model} / {preset.effort or '模型默认'}"

    def _question_model(self, job, scope, rt):
        """Freeze the selected backend before asynchronous attachment preparation."""
        group = self._group(scope)
        if group == "codex":
            preset = profile_for_scope(
                self.db, scope, job.binding.title if job.binding else ""
            )
            level = (
                preset[0]
                if preset
                else self.db.get_setting(f"runtime:{scope}:picker_level", "default")
            )
            model = rt.model or "Codex"
            effort = rt.effort or ""
        else:
            level = self._group_level(scope)
            profile = self._claude_profile(scope)
            model, effort = profile.model, profile.effort
        return group, level, model, effort

    def _question_inputs(self, job, scope, inputs, *, selected=None):
        m = job.inbox.message
        group, level, model, effort = selected or self._question_model(
            job, scope, self._runtime_settings(scope)
        )
        cwd = Path(job.binding.cwd) if job.binding else self.config.admin_scratch_dir
        owned = (
            self._allowed_workspace(cwd.resolve())
            or cwd == self.config.admin_scratch_dir
        )
        if not owned and group != "codex":
            raise ValueError("工作目录不在授权范围内")
        log = (
            log_name(
                self.db,
                cwd,
                scope if job.binding else "dm:" + m.chat_id,
                job.binding.title if job.binding else "私聊临时对话",
            )
            if owned
            else ""
        )
        ctx = self.db.save_question(
            m,
            dict(
                nonce=secrets.token_hex(12),
                group=group,
                level=level,
                model=model,
                effort=effort,
                temporary=not bool(job.binding),
                text=m.source_text if m.source_text is not None else m.text,
                parent=m.parent_id,
                cwd=str(cwd),
                log=str(log),
                images=[i["path"] for i in inputs if i["type"] == "localImage"],
                files=[
                    str(a.local_path)
                    for a in m.attachments
                    if a.kind != "image" and a.local_path
                ],
            ),
        )
        # Keep protocol rules after the delivery constraint, outside the stored
        # source question (including for daily-length accounting and consultations).
        instruction = rules(ctx, ["codex", *self.groups])
        return ctx, [
            *inputs,
            {"type": "text", "text": instruction, "text_elements": []},
        ]

    async def _execute_claude(self, scope, job):
        m = job.inbox.message
        if self.db.inbox_state(m.message_id) not in {"queued", "processing"}:
            return
        rt = self._runtime_settings(scope)
        selected = self._question_model(job, scope, rt)
        inputs = await self.artifacts.prepare_inputs(m)
        ctx, inputs = self._question_inputs(job, scope, inputs, selected=selected)
        group, level = ctx["group"], ctx["level"]
        if group not in self.groups:
            raise ValueError("模型组已改变，请重试")
        session_key = f"claude-session:{group}:{scope}"
        previous = self.db.get_setting(session_key, "") if job.binding else ""
        session = previous or str(uuid.uuid4())
        turn_id = "cc:" + hashlib.sha256(m.message_id.encode()).hexdigest()[:32]
        thread = job.binding.thread_id if job.binding else "cc-dm:" + turn_id[3:]
        if not self.db.mark_incoming_dispatching(m.message_id):
            return
        self.db.upsert_turn_job(
            TurnJob(
                m.message_id,
                thread,
                turn_id,
                m.app_role,
                m.chat_id,
                job.progress_message_id,
                "accepted",
            )
        )
        active = ActiveTurn(
            thread,
            turn_id,
            m.chat_id,
            m.app_role,
            job.progress_message_id,
            started_monotonic=time.monotonic(),
        )
        self._register_active(active)
        self.db.complete_incoming(m.message_id)

        def on_event(operation, delta):
            active.last_event_monotonic = time.monotonic()
            if operation:
                active.current_operation = operation
            if delta and ctx["nonce"] not in delta:
                active.commentary_text = delta

        def on_session(s):
            if s != session:
                raise ClaudeError("Claude 会话标识不符，拒绝合并历史")
            if job.binding:
                self.db.set_setting(session_key, s)

        full = (
            rt.sandbox == "danger-full-access"
            and rt.approval_policy == "never"
            and m.sender_open_id == self._owner(m.app_role)
        )
        profile = (
            "full"
            if full
            else ("workspace-write" if rt.sandbox == "workspace-write" else "read-only")
        )
        outbox = (
            self.config.outbox_dir
            / hashlib.sha256(m.message_id.encode()).hexdigest()[:20]
        )
        try:
            blocks = content_blocks(inputs, self.config.visual_proxy_dir)
            result = await self.claude.run(
                turn_id,
                ModelProfile(
                    ctx["model"], ctx.get("effort", self.groups[group][level].effort)
                ),
                blocks,
                cwd=Path(ctx["cwd"]),
                session=session,
                resume=bool(previous),
                persistent=bool(job.binding),
                profile=profile,
                mounts=[Path(p) for p in ctx["files"]],
                outbox=outbox,
                on_event=on_event,
                on_session=on_session,
            )
            status = "completed"
        except (ClaudeError, OSError, ValueError, RuntimeError) as exc:
            status = "interrupted" if "按要求停止" in str(exc) else "failed"
            result = "Claude：" + redact_log(exc)
        terminal = self.db.claude_terminal(
            m.message_id, {"status": status, "text": result}
        )
        active.final_text = terminal["text"]
        await self._finalize_turn(
            active, {"id": turn_id, "items": [], "status": terminal["status"]}
        )

    async def _recover_claude(self, job):
        if job.turn_id in self._active_by_turn or job.turn_id in self._completed_turns:
            return
        terminal = self.db.claude_terminal(job.message_id) or {
            "text": "桥在 Claude 执行过程中退出；结果未确认，为避免重复操作未重放。下一条消息可继续原会话。",
            "status": "interrupted",
        }
        active = ActiveTurn(
            job.thread_id,
            job.turn_id,
            job.chat_id,
            job.app_role,
            job.progress_message_id,
            final_text=terminal["text"],
            started_monotonic=time.monotonic(),
        )
        self._register_active(active)
        await self._notification_final(
            active, {"id": job.turn_id, "items": [], "status": terminal["status"]}
        )

    async def _interrupt_engine(self, active):
        consult = self._consult_tasks.get(active.turn_id)
        if consult:
            consult.cancel()
            return
        if active.turn_id.startswith("cc:"):
            control = self.claude.running.get(active.turn_id)
            if control:
                control.stop()
            return
        await self.codex.interrupt_turn(active.thread_id, active.turn_id)

    def _journal(self, ctx, mid, group, model, answer):
        if not ctx["log"]:
            return ""
        try:
            path = Path(ctx["log"])
            if path.parent != Path(ctx["cwd"]) / "vibe_log":
                raise ValueError("日志路径无效")
            append_journal(path, mid, group, model, ctx["text"], answer)
        except (OSError, ValueError) as e:
            # A read-only repo or unsafe log file must not swallow a model answer.
            return f"日志未写入（{type(e).__name__}）"
        return ""

    async def _group_final(self, active, status):
        mid = self.db.reply_target_for_turn(
            active.turn_id, active.chat_id, active.app_role
        )
        pending = self._pending_jobs.get(active.thread_id)
        if not mid and pending:
            mid = pending.inbox.message.message_id
        ctx = (
            self.db.group_question(mid, active.chat_id, active.app_role)
            if mid
            else None
        )
        if not ctx:
            return False
        plan = action(active.final_text, ctx) if status == "completed" else None
        if plan is None:
            warning = self._journal(
                ctx, mid, ctx["group"], ctx["model"], active.final_text
            )
            if warning:
                active.final_text += "\n\n" + warning
            return False
        source = ctx
        if plan["source"] != "current":
            if plan["source"] == "quoted" and not ctx["parent"]:
                raise ValueError("没有引用的用户原题，请引用或重发问题")
            source = self.db.referenced_question(
                ctx["parent"] if plan["source"] == "quoted" else mid,
                active.chat_id,
                active.app_role,
                previous=plan["source"] == "previous",
            )
            if not source:
                raise ValueError("找不到本对话中可核验的原题，请引用或重发问题")
        question = source["text"]
        if "span" in plan:
            if not plan["span"].strip() or question.count(plan["span"]) != 1:
                raise ValueError("指定的题面不是原题中唯一的原文，未外发")
            question = plan["span"]
        if SECRET.search(question):
            raise ValueError("题面含凭据特征，不向其他组外发")
        order = ["codex", *self.groups]
        target = plan["target"].casefold()
        if target not in ["all", "others", *order]:
            raise ValueError("未知模型组")
        tier = "advanced" if plan["tier"] == "advanced" else ctx["level"]
        targets = (
            order
            if target == "all"
            else [g for g in order if g != ctx["group"]]
            if target == "others"
            else [target]
        )

        async def ask(g):
            fresh, old = self.db.claim_consult(mid, g)
            if not fresh:
                return old if old is not None else f"【{g}】上次调用结果待确认，未重放"
            name = g
            try:
                profiles = load_profiles(self.db) if g == "codex" else self.groups[g]
                preset = profiles[tier]
                name = preset.model
                async with asyncio.timeout(180):
                    answer = await self._blind_answer(
                        g, preset, question, source, active.turn_id
                    )
                if SECRET.search(answer):
                    raise ValueError("答复含凭据特征，出于保密未外发")
                display = f"【{g} · {preset.model} / {preset.effort}】\n" + answer
            except Exception as exc:
                answer = "该组未取得回答：" + redact_log(exc)
                display = f"【{g} · {name}】{answer}"
            self.db.finish_consult(mid, g, display)
            warning = self._journal(
                dict(ctx, text=question), mid, "咨询-" + g, name, answer
            )
            return display + ("\n" + warning if warning else "")

        batch = asyncio.gather(*(ask(g) for g in targets))
        self._consult_tasks[active.turn_id] = batch
        try:
            responses = await batch
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            responses = ["已停止本次跨组咨询。"]
        finally:
            self._consult_tasks.pop(active.turn_id, None)
        active.final_text = "\n\n".join(responses)
        self._journal(
            ctx, mid, ctx["group"], ctx["model"], "发起跨组协作：" + ", ".join(targets)
        )
        return True

    async def _blind_answer(self, group, preset, question, source, turn):
        # Fresh context, no access to shared logs, no tool for recursive delegation.
        inputs = [{"type": "text", "text": question}]
        for path in source["images"]:
            if (
                Path(path).is_symlink()
                or not Path(path).is_file()
                or not Path(path)
                .resolve()
                .is_relative_to(self.config.visual_proxy_dir.resolve())
            ):
                raise ValueError("图片不属于桥的代理目录")
            inputs.append({"type": "localImage", "path": path})
        documents = []
        for path in source["files"]:
            f = self.artifacts._validate_staged(Path(path))
            if f.stat().st_size > 2_000_000:
                raise ValueError("附件过大，不自动转发")
            data = f.read_bytes()
            if data.startswith(b"%PDF") and group != "codex":
                documents.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode(),
                        },
                    }
                )
            else:
                try:
                    value = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise ValueError("该组不能无损接收此附件格式") from None
                if "\x00" in value or SECRET.search(value):
                    raise ValueError("附件不能安全转发")
                inputs.append({"type": "text", "text": value})
        with tempfile.TemporaryDirectory(prefix="yinshi-blind-") as td:
            cwd = Path(td)
            if group == "codex":
                return await blind_codex(self.config.codex_bin, preset, inputs, cwd)
            blocks = content_blocks(inputs, self.config.visual_proxy_dir) + documents
            return await self.claude.run(
                turn + ":" + group,
                preset,
                blocks,
                cwd=cwd,
                session=str(uuid.uuid4()),
                resume=False,
                persistent=False,
                profile="blind",
                mounts=[],
                outbox=None,
                on_event=lambda *_: None,
                on_session=lambda *_: None,
                system=FORMULA_RULE,
            )


async def blind_codex(
    binary: str, preset: ModelProfile, inputs: list[dict], cwd: Path
) -> str:
    args = [
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        preset.model,
        "-c",
        f'model_reasoning_effort="{preset.effort}"',
        "-c",
        'web_search="disabled"',
        "-c",
        "mcp_servers={}",
        "-c",
        "developer_instructions=" + json.dumps(FORMULA_RULE, ensure_ascii=False),
    ]
    for feature in (
        "shell_tool",
        "unified_exec",
        "code_mode",
        "code_mode_host",
        "multi_agent",
        "multi_agent_v2",
        "plugins",
        "image_generation",
    ):
        args += ["--disable", feature]
    # Do not expose project roots, other transcripts, plugins, user instructions
    # or Feishu credentials. Only the client's pre-existing login and the exact
    # authorized image proxies enter this empty, ephemeral environment.
    bwrap = shutil.which("bwrap")
    exe = Path(shutil.which(binary) or binary).resolve(strict=True)
    if not bwrap:
        raise ValueError("缺少隔离环境，未外发给 Codex")
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if not auth.is_file():
        raise ValueError("未找到 Codex 本机登录，未进行盲答")
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
        path = Path("/") / name
        if path.is_symlink():
            cmd += ["--symlink", os.readlink(path), str(path)]
        elif path.exists():
            cmd += ["--ro-bind", str(path), str(path)]
    package = exe.parent.parent
    # Native official standalone packages use bin/codex and sibling libexec.
    if exe.parent.name != "bin" or package in (Path("/"), Path.home()):
        raise ValueError("不支持的 Codex 安装布局，未扩大可读范围")
    cmd += [
        "--ro-bind",
        str(package),
        "/opt/codex",
        "--dir",
        "/tmp/home/.codex",
        "--ro-bind",
        str(auth),
        "/tmp/home/.codex/auth.json",
        "--dir",
        "/tmp/work",
    ]
    for idx, item in enumerate(inputs):
        if item["type"] == "localImage":
            dest = f"/tmp/attachment-{idx}.jpg"
            cmd += ["--ro-bind", item["path"], dest]
            args += ["--image", dest]
    env = {
        k: v
        for k, v in os.environ.items()
        if k.lower() in {"https_proxy", "http_proxy", "all_proxy", "no_proxy"}
    }
    env.update(
        HOME="/tmp/home",
        CODEX_HOME="/tmp/home/.codex",
        PATH="/usr/local/bin:/usr/bin:/bin",
        LANG="C.UTF-8",
    )
    p = await asyncio.create_subprocess_exec(
        *cmd,
        "--chdir",
        "/tmp/work",
        "--",
        "/opt/codex/bin/" + exe.name,
        *args,
        "--json",
        "-",
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        text = "\n\n".join(i["text"] for i in inputs if i["type"] == "text")
        out, _ = await p.communicate(text.encode())
        result = None
        for line in out.splitlines():
            event = json.loads(line)
            item = event.get("item") or {}
            if (
                event.get("type") == "item.completed"
                and item.get("type") == "agent_message"
            ):
                result = item.get("text")
        if p.returncode or not isinstance(result, str) or not result:
            raise ValueError("Codex 未返回完整回答（请检查额度或连接）")
        return result
    finally:
        if p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            await p.wait()
