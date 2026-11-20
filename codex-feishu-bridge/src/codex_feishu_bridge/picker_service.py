"""Read-only bridge commands, scoped model selection, and same-card navigation."""

from __future__ import annotations

import asyncio
import time

from .model_menu import Choice, GROUP, ID, group_style, label, model_card
from .model_profiles import load_profiles, profile_for_scope


class ModelPickerService:
    async def _picker_source(self, name):
        if name == "codex" and (
            not self._codex_online or self._runtime_compatibility_error
        ):
            return [], "Codex 兼容门禁或连接未就绪，可选其他组。"
        if name == "c" and not self.claude.available():
            return [], "C 组未配置。"
        lock = self._picker_locks.setdefault(name, asyncio.Lock())
        async with lock:
            cached = self._picker_cache.get(name)
            if cached and cached[0] > time.monotonic():
                return cached[1:]
            try:
                fetch = (
                    self.codex.list_models
                    if name == "codex"
                    else self.claude.list_models
                )
                rows = await asyncio.wait_for(fetch(), 3.5)
            except Exception:
                rows, note, ttl = (
                    [],
                    (
                        "Codex 目录暂不可用。"
                        if name == "codex"
                        else "C 组目录暂不可用，仅显示已配置档位。"
                    ),
                    20,
                )
            else:
                note, ttl = "", (90 if name == "codex" else 600)
            self._picker_cache[name] = (time.monotonic() + ttl, rows, note)
            return rows, note

    def _picker_current(self, scope):
        group = self._group(scope)
        if group in self.groups:
            return group, self._claude_profile(scope).model
        return "codex", self._runtime_settings(scope).model or ""

    async def _picker_view(self, scope, binding, page):
        (native, note1), (other, note2) = await asyncio.gather(
            self._picker_source("codex"), self._picker_source("c")
        )
        notes = [v for v in (note1, note2) if v]
        profiles = load_profiles(self.db)
        locked = bool(
            binding and "random" in binding.title.casefold() and "advanced" in profiles
        )
        choices = []
        for m in native:
            mid = m.get("model") or m.get("id")
            if not isinstance(mid, str) or not ID.fullmatch(mid):
                continue
            if locked and mid != profiles["advanced"].model:
                continue
            choices.append(Choice("codex", mid, label(mid, m.get("displayName", ""))))
        if locked:
            notes.append("Random：Codex 固定高级档；其他组可自由选择。")
        if self.claude.available():
            for group, tiers in self.groups.items():
                for level, p in tiers.items():
                    if group == "c" and not note2 and p.model not in other:
                        continue
                    choices.append(Choice(group, p.model, label(p.model), level))
                if group == "c":
                    known = {p.model for p in tiers.values()}
                    choices.extend(
                        Choice(group, m, label(m)) for m in other if m not in known
                    )
        fav = [
            ("codex", profiles[k].model) if k in profiles else ("codex", v)
            for k, v in (("default", "gpt-6.2-sol"), ("advanced", "gpt-6.2-astra"))
        ]
        fav += [("c", self.groups["c"][k].model) for k in ("default", "advanced")]
        fav += [
            ("c", "claude-sonnet-5-5"),
            ("codex", "gpt-6.1-sol"),
            ("c", "claude-haiku-5-5"),
        ]
        return model_card(choices, fav, self._picker_current(scope), page, notes)

    async def _picker_display(self, m, scope, binding, page, *, existing=None):
        card = await self._picker_view(scope, binding, page)
        if existing:
            try:
                await self.gateway.patch_card(m.app_role, existing, card)
            except Exception:
                pass  # expired/non-editable card: one idempotent replacement
            else:
                self.db.model_card_page(existing, m, page)
                return
        remote = await self.gateway.send_card(
            m.app_role,
            m.chat_id,
            card,
            idempotency_key=f"model-menu:{m.message_id}",
            reply_to_message_id=existing or m.message_id,
        )
        if remote:
            self.db.remember_model_card(remote, m, scope, page)

    async def _picker_pick(self, m, scope, binding, group, model, requested=None):
        key = f"runtime:model_picker_receipts:{m.message_id}"
        previous = self.db.get_setting(key, "")
        if previous:
            return previous
        if not ID.fullmatch(model) or (group != "codex" and group not in self.groups):
            raise ValueError("无法识别该模型或组别；未更改设置。")
        group_name = group_style(group)[0]
        notes = []
        if group in self.groups:
            if not self.claude.available():
                raise ValueError("此机未配置 Claude 客户端或令牌；未切换。")
            levels = self.groups[group]
            candidates = [k for k, p in levels.items() if p.model == model]
            directory, note = (
                await self._picker_source("c") if group == "c" else ([], "")
            )
            if (group == "c" and not note and model not in directory) or (
                not candidates and (group != "c" or model not in directory)
            ):
                raise ValueError("当前组目录没有该型号；未切换。")
            current = (
                self._group_level(scope) if self._group(scope) == group else "default"
            )
            level = (
                (current if current in candidates else candidates[0])
                if candidates
                else current
            )
            if requested is not None and requested not in (
                "default",
                levels[level].effort if candidates else "",
            ):
                raise ValueError(
                    "这个 Claude 型号不支持通过此菜单指定该强度；未修改设置。"
                )
            values = {
                "model_group": group,
                "group_level": level,
                "group_model": "" if candidates else model,
            }
        else:
            models, notice = await self._picker_source("codex")
            entry = next(
                (row for row in models if model in (row.get("model"), row.get("id"))),
                None,
            )
            if not entry:
                raise ValueError(
                    (notice or f"当前 Codex 目录没有 {model}。") + " 未切换。"
                )
            model = entry.get("model") or entry["id"]
            supported = {
                v["reasoningEffort"]
                for v in entry.get("supportedReasoningEfforts") or []
                if v.get("reasoningEffort")
            }
            if (
                requested is not None
                and requested != "default"
                and requested not in supported
            ):
                raise ValueError("该模型不支持指定的推理强度；未修改设置。")
            rt = self._runtime_settings(scope)
            fast = rt.service_tier
            if fast and not any(
                t.get("id") == fast for t in entry.get("serviceTiers") or []
            ):
                fast = None
                notes.append("该型号不支持原速度，使用普通速度。")
            effort = (
                rt.effort
                if requested is None
                else None
                if requested == "default"
                else requested
            )
            if requested is None and effort and effort not in supported:
                effort = None
                notes.append("该型号不支持原推理强度，使用模型默认值。")
            presets = load_profiles(self.db)
            match = next((k for k, p in presets.items() if p.model == model), None)
            before = profile_for_scope(self.db, scope, binding.title if binding else "")
            logical = match or (
                before[0]
                if before
                else self.db.get_setting(f"runtime:{scope}:picker_level", "default")
            )
            preset = (
                match
                if match
                and requested is None
                and not fast
                and effort == presets[match].effort
                else ""
            )
            if (
                binding
                and "random" in binding.title.casefold()
                and "advanced" in presets
            ):
                advanced = presets["advanced"]
                if model != advanced.model or requested not in (None, advanced.effort):
                    raise ValueError(
                        "Random 的 Codex 档固定高级模型和 Fast；仍可在 /model 切换到其他组。"
                    )
                if advanced.effort not in supported or not any(
                    t.get("id") == "priority" for t in entry.get("serviceTiers") or []
                ):
                    raise ValueError("当前目录不再支持 Random 的高级档设置；未切换。")
                logical, preset, effort, fast = (
                    "advanced",
                    "advanced",
                    advanced.effort,
                    "priority",
                )
            values = {
                "model_group": "codex",
                "model": model,
                "model_profile": preset,
                "picker_level": logical,
                "effort": effort or "__model_default__",
                "service_tier": fast or "__off__",
            }
        reply = (
            f"已选择 {group_name} · {label(model)}。本对话下一项任务使用；正在执行的任务不变。"
            + "".join(notes)
        )
        written = self.db.set_runtime_configs(
            [
                *((scope, k, v) for k, v in values.items()),
                ("model_picker_receipts", m.message_id, reply),
            ],
            message_id=m.message_id,
            once_key=key,
        )
        if not written:
            return self.db.get_setting(key, "") or reply
        if group == "codex":
            try:
                await self._apply_runtime_if_idle(binding, scope)
            except Exception:
                pass  # durable settings still apply on the next turn
        return reply

    async def _bridge_status(self, scope, binding, m):
        group = self._group(scope)
        active = self._active_for_chat(
            m.app_role, m.chat_id, thread_id=binding.thread_id if binding else None
        )
        queue = self._thread_queues.get(scope) if binding else self._admin_queue
        run = "当前空闲"
        blocked = self.db.get_setting(f"blocked_thread:{scope}", "") if binding else ""
        if blocked:
            run = "安全锁定，等待核对"
        elif active:
            original = self.db.turn_job_message_id(active.turn_id)
            ctx = (
                self.db.group_question(original, m.chat_id, m.app_role)
                if original
                else None
            )
            engine = "Claude" if active.turn_id.startswith("cc:") else "Codex"
            run = f"正在执行：{engine}" + (
                f" · {label(ctx['model'])}" if ctx and ctx.get("model") else ""
            )
            run += "；模型选择用于下一项任务"
        extra = f"\n\n{run}；等待队列 {queue.qsize() if queue else 0} 条。"
        if not binding and m.text.strip() == "状态":
            extra += f"\n桥服务运行中；收件箱：{self.db.inbox_counts()}；发件箱：{self.db.outbox_counts()}；长连接：{self.gateway.receiver_status()}；本月桥内记录的飞书 API：{sum(self.db.api_usage().values())} 次。"
        if group == "codex":
            return (await self._format_runtime_status(scope, binding)) + extra
        preset = self._claude_profile(scope)
        rt = self._runtime_settings(scope)
        # Same effective permission decision as Claude execution; a collaborator
        # may not inherit the paired owner's unrestricted profile.
        full = (
            rt.sandbox == "danger-full-access"
            and rt.approval_policy == "never"
            and m.sender_open_id == self._owner(m.app_role)
        )
        perm = (
            "Full Access (YOLO)"
            if full
            else "工作区"
            if rt.sandbox == "workspace-write"
            else "只读"
        )
        cwd = binding.cwd if binding else str(self.config.admin_scratch_dir)
        return (
            f"{'当前对话' if binding else '私聊临时任务'}状态：\n"
            f"• 主组：{group_style(group)[0]}\n• 模型：{label(preset.model)}（{preset.model}）\n"
            f"• 推理：{preset.effort or '客户端默认（飞书桥未指定）'}\n• Fast：不适用（Claude 渠道）\n• 权限：{perm}\n"
            + (f"• 工作目录：{cwd}\n" if self.config.show_workspace_path else "")
            + "• 额度：使用 Claude 渠道，不消耗 Codex 额度。"
            + extra
        )

    async def _try_bridge_menu(self, item):
        m = item.message
        words = m.text.strip().split()
        if not words:
            return False
        cmd = words[0].casefold()
        status = (
            cmd in {"/status", "status", "!status", "状态", "!配置", "!设置"}
            and len(words) == 1
        )
        internal = cmd == "/bridge-model-menu"
        if not (status or internal or cmd in {"/model", "!模型"}):
            return False
        binding = self.db.get_binding_by_chat(m.chat_id)
        scope = binding.thread_id if binding else "admin"
        existing = None
        page = 0
        reply = None
        if (
            not internal
            and not binding
            and m.chat_type != "p2p"
            and m.app_role != "admin"
        ):
            if m.message_type != "card_action":
                return False
            # Old rev1 model cards did not carry a chat type; only a previously
            # verified owner DM may use that compatibility path.
            if m.chat_id != self.db.get_setting(
                f"owner_chat_id:{m.app_role}", ""
            ) or m.sender_open_id != self._owner(m.app_role):
                self.db.complete_incoming(m.message_id)
                return True
        try:
            if internal:
                # Never use a scope from card JSON. Bind to the originally sent
                # Feishu message, chat and app; forwarded/cross-DM buttons fail.
                ctx = (
                    self.db.read_model_card(m.parent_id, m)
                    if m.message_type == "card_action" and m.parent_id
                    else None
                )
                if (
                    not ctx
                    or ctx[0] != scope
                    or (not binding and m.sender_open_id != self._owner(m.app_role))
                ):
                    raise ValueError(
                        "此卡片与当前对话不匹配或已过期；请在这里发送 /model。"
                    )
                existing = m.parent_id
                page = ctx[1]
                if len(words) == 3 and words[1] == "page" and re_page(words[2]):
                    await self._picker_display(
                        m, scope, binding, int(words[2]), existing=existing
                    )
                elif (
                    len(words) == 4 and words[1] == "pick" and GROUP.fullmatch(words[2])
                ):
                    reply = await self._picker_pick(
                        m, scope, binding, words[2], words[3]
                    )
                    await self._picker_display(
                        m, scope, binding, page, existing=existing
                    )
                else:
                    raise ValueError("无效卡片操作；未修改设置。")
            elif status:
                reply = await self._bridge_status(scope, binding, m)
            elif len(words) == 1 or (
                len(words) == 2 and words[1].casefold() in {"more", "更多"}
            ):
                await self._picker_display(
                    m, scope, binding, 0 if len(words) == 1 else 1
                )
            elif len(words) in (2, 3):
                token = words[1]
                group, sep, model = token.partition(":")
                if not sep or group.casefold() not in {"codex", *self.groups}:
                    model = token
                    found = [
                        g
                        for g, tiers in self.groups.items()
                        if any(p.model == model for p in tiers.values())
                    ]
                    group = (
                        found[0]
                        if len(found) == 1
                        else "c"
                        if model.startswith("claude-")
                        else "codex"
                    )
                reply = await self._picker_pick(
                    m,
                    scope,
                    binding,
                    group.casefold(),
                    model,
                    words[2].casefold() if len(words) == 3 else None,
                )
            else:
                raise ValueError(
                    "发送 /model 打开选择器，或 /model 组名:模型ID 直接选择。"
                )
        except ValueError as exc:
            reply = str(exc)
        if reply:
            await self.gateway.send_text(
                m.app_role,
                m.chat_id,
                reply,
                idempotency_key=f"bridge-menu-result:{m.message_id}",
                reply_to_message_id=existing or m.message_id,
            )
        self.db.complete_incoming(m.message_id)
        return True


def re_page(value):
    return (
        value.isascii() and value.isdigit() and len(value) <= 5 and int(value) <= 10000
    )
