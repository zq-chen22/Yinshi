from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .db import BridgeDB


PROFILE_SCOPE = "model_profiles"
PROFILE_LABELS = {"default": "默认", "advanced": "高级"}


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model: str
    effort: str

    def encode(self) -> str:
        return json.dumps({"model": self.model, "effort": self.effort}, sort_keys=True)


def load_profiles(database: BridgeDB) -> dict[str, ModelProfile]:
    profiles = {}
    for name in PROFILE_LABELS:
        raw = database.get_setting(f"runtime:{PROFILE_SCOPE}:{name}", "")
        if not raw:
            continue
        try:
            value = json.loads(raw)
            if all(
                isinstance(value.get(key), str) and value[key]
                for key in ["model", "effort"]
            ):
                profiles[name] = ModelProfile(value["model"], value["effort"])
        except (TypeError, ValueError, AttributeError):
            continue
    return profiles


def validate_profile(
    argument: str, models: list[dict[str, Any]], *, advanced: bool
) -> ModelProfile:
    parts = argument.strip().split()
    if len(parts) < 2:
        raise ValueError("请同时指定模型与思考强度，例如：默认模型 gpt-6.2-sol xhigh")
    requested_model = "-".join(parts[:-1]).casefold()
    effort = parts[-1].casefold()
    selected = next(
        (
            model
            for model in models
            if requested_model
            in {
                str(model.get("model") or "").casefold(),
                str(model.get("id") or "").casefold(),
            }
        ),
        None,
    )
    if selected is None:
        raise ValueError(
            f"当前CLI目录没有模型 {requested_model}；未修改任何档位或对话。"
        )
    efforts = {
        entry.get("reasoningEffort")
        for entry in selected.get("supportedReasoningEfforts") or []
    }
    if effort not in efforts:
        raise ValueError(f"该模型不支持 {effort} 思考强度；未修改任何设置。")
    if advanced and not any(
        tier.get("id") == "priority" for tier in selected.get("serviceTiers") or []
    ):
        raise ValueError("高级模型必须支持Fast，才能满足Random固定高级+Fast。")
    return ModelProfile(str(selected.get("model") or selected["id"]), effort)


def profile_changes(
    scope: str, title: str, level: str, profiles: dict[str, ModelProfile]
) -> list[tuple[str, str, str]]:
    if set(profiles) != set(PROFILE_LABELS):
        raise ValueError("请先设置默认模型和高级模型两个档位。")
    random = "random" in title.casefold()
    level = "advanced" if random else level
    preset = profiles[level]
    return [
        (scope, "model", preset.model),
        (scope, "effort", preset.effort),
        (scope, "service_tier", "priority" if random else "__off__"),
        (scope, "model_profile", level),
    ]


def profile_for_scope(
    database: BridgeDB, scope: str, title: str
) -> tuple[str, ModelProfile] | None:
    profiles = load_profiles(database)
    if set(profiles) != set(PROFILE_LABELS):
        return None
    level = (
        "advanced"
        if "random" in title.casefold()
        else database.get_setting(f"runtime:{scope}:model_profile", "default")
    )
    return (level, profiles[level]) if level in profiles else None


def profile_command(text: str) -> tuple[str, str] | None:
    if text in {"默认", "高级", "全部默认", "全部高级", "模型档位"}:
        return text, ""
    match = re.fullmatch(r"(默认模型|高级模型)\s+(.+)", text, re.DOTALL)
    return (match.group(1), match.group(2).strip()) if match else None


def initialize_profiles(
    database: BridgeDB, definitions: dict, *, level: str, revision: str
) -> dict:
    if (
        database.get_setting(f"runtime:{PROFILE_SCOPE}:migration_revision", "")
        == revision
    ):
        return {"unchanged": True}
    profiles = {name: ModelProfile(**value) for name, value in definitions.items()}
    if (
        set(profiles) != set(PROFILE_LABELS)
        or level not in PROFILE_LABELS
        or not revision
    ):
        raise ValueError("Invalid model-profile migration")
    changes = [
        (PROFILE_SCOPE, name, preset.encode()) for name, preset in profiles.items()
    ]
    bindings = [
        binding
        for binding in database.list_bindings()
        if binding.active and binding.chat_id
    ]
    scopes = [(binding.thread_id, binding.title) for binding in bindings] + [
        ("admin", "")
    ]
    for scope, title in scopes:
        changes.extend(profile_changes(scope, title, level, profiles))
    changes.append((PROFILE_SCOPE, "migration_revision", revision))
    database.set_runtime_configs(
        changes, message_id=f"model-profile-migration:{revision}"
    )
    return {
        "scopes": len(scopes),
        "random_scopes": sum("random" in title.casefold() for _, title in scopes),
        "initial_level": level,
    }
