"""Provider-neutral, mobile-first Feishu model picker. Labels are not settings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", re.ASCII)
GROUP = re.compile(r"[a-z][a-z0-9]{0,9}", re.ASCII)
HOT = 5
PAGE = 8


@dataclass(frozen=True)
class Choice:
    group: str
    model: str
    title: str
    tier: str = ""  # a configured Claude profile, not a model-name suffix

    @property
    def key(self):
        return self.group, self.model


def label(model: str, fallback: str = "") -> str:
    match = re.fullmatch(r"gpt-([0-9.]+)-([a-z]+)", model)
    if match:
        return f"GPT-{match[1]} {match[2].capitalize()}"
    match = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-(\d{8}))?", model)
    if match:
        return (
            f"{match[1].capitalize()} {match[2]}"
            + (f".{match[3]}" if match[3] else "")
            + (f" ({match[4]})" if match[4] else "")
        )
    return fallback or model


def group_style(group):
    if group == "codex":
        return "Codex", "blue", "🟦"
    if group == "c":
        return "C 组 · Claude", "purple", "🟪"
    palette = [("green", "🟩"), ("orange", "🟧"), ("red", "🟥"), ("yellow", "🟨")]
    color, dot = palette[hashlib.sha256(group.encode()).digest()[0] % len(palette)]
    return group.upper() + " 组", color, dot


def ordered(choices, favorites, current):
    by_key = {c.key: c for c in choices}
    head = []
    for key in [*favorites[:4], current, *favorites[4:], *by_key]:
        if key in by_key and key not in head:
            head.append(key)
        if len(head) == HOT:
            break
    return [by_key[k] for k in head], [v for k, v in by_key.items() if k not in head]


def button(title, *, model=None, group=None, page=None, selected=False):
    value = {"kind": "bridge_model"}
    if page is not None:
        value.update(action="page", page=page)
    else:
        value.update(action="pick", group=group, model=model)
    item = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": title[:80]},
        "value": value,
    }
    if selected:
        item["type"] = "primary"
    return item


def model_card(choices, favorites, current, page=0, notes=()):
    common, rest = ordered(choices, favorites, current)
    pages = (len(rest) + PAGE - 1) // PAGE
    page = max(0, min(page, pages))
    group, model = current
    name, color, dot = group_style(group)
    # Use a color and a text label, not color alone. Two buttons per row also
    # keep a legible layout on mobile. Markdown never receives catalog prose.
    parts = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"当前：{dot} {name}  /  {label(model) if model else '默认'}",
            },
        }
    ]

    def sections(items):
        for group in dict.fromkeys(x.group for x in items):
            name, col, marker = group_style(group)
            parts.append(
                {
                    "tag": "markdown",
                    "content": f"<font color='{col}'>{marker}  {name}</font>",
                }
            )
            buttons = [
                button(x.title, model=x.model, group=x.group, selected=x.key == current)
                for x in items
                if x.group == group
            ]
            parts.extend(
                {"tag": "action", "actions": buttons[n : n + 2]}
                for n in range(0, len(buttons), 2)
            )

    sections(common)
    if page:
        parts.append({"tag": "hr"})
        parts.append({"tag": "markdown", "content": f"**更多模型**　{page} / {pages}"})
        sections(rest[(page - 1) * PAGE : page * PAGE])
    if rest:
        nav = []
        if not page:
            nav.append(button(f"查看更多（{len(rest)}）", page=1))
        else:
            if page > 1:
                nav.append(button("上一页", page=page - 1))
            if page < pages:
                nav.append(button("下一页", page=page + 1))
            nav.append(button("收起", page=0))
        parts.append({"tag": "action", "actions": nav})
    if notes:
        parts.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "　".join(notes)}],
            }
        )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": "选择模型"},
        },
        "elements": parts,
    }
