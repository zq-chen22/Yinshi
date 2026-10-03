from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from codex_feishu_bridge.config import (
    BridgeConfig,
    DailyStatsConfig,
    FeishuAppConfig,
    FeishuConfig,
)
from codex_feishu_bridge.daily_stats import DailyCount, sync_daily_stats
from codex_feishu_bridge.quota_stats import QuotaSample


def _column_index(name: str) -> int:
    value = 0
    for letter in name:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value - 1


class FakeSheet:
    def __init__(self, today: datetime) -> None:
        self.width = 12
        self.rows = [
            ["日期", "旧设备", None, "旧设备·额度", None, "全设备汇总", None],
            [None, "总任务", "长任务", "剩余周额度", "当日观测用量", "总任务", "长任务"],
            [today.date().isoformat(), 1, 0, "90%", "1%", None, None],
            [(today - timedelta(days=1)).date().isoformat(), 2, 1, "91%", "2%", None, None],
        ]
        self.merges = [
            {
                "start_row_index": 0,
                "end_row_index": 0,
                "start_column_index": index,
                "end_column_index": index + 1,
            }
            for index in (1, 3, 5)
        ]
        self.inserted: list[int] = []

    async def __aenter__(self) -> FakeSheet:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def bot_name(self) -> str:
        return "新设备"

    async def document_permission(self, *_: object) -> bool:
        return True

    async def sheet_info(self, *_: object) -> dict:
        return {
            "title": "每日总览",
            "grid_properties": {"column_count": self.width, "row_count": 10},
            "merges": self.merges,
        }

    def _range(self, name: str) -> tuple[int, int, int, int]:
        match = re.fullmatch(r"[^!]+!([A-Z]+)(\d+):([A-Z]+)(\d+)", name)
        assert match is not None
        left, top, right, bottom = match.groups()
        return _column_index(left), int(top) - 1, _column_index(right), int(bottom) - 1

    async def values(self, _: str, name: str) -> list[list]:
        left, top, right, bottom = self._range(name)
        result = []
        for row_number in range(top, bottom + 1):
            source = self.rows[row_number] if row_number < len(self.rows) else []
            row = []
            for column in range(left, right + 1):
                value = source[column] if column < len(source) else None
                if isinstance(value, dict) and value.get("type") == "formula":
                    value = value["text"].lstrip("=")
                row.append(value)
            result.append(row)
        return result

    async def write_values(self, _: str, name: str, values: list[list]) -> None:
        left, top, _, _ = self._range(name)
        for row_offset, items in enumerate(values):
            while len(self.rows) <= top + row_offset:
                self.rows.append([])
            row = self.rows[top + row_offset]
            for column_offset, value in enumerate(items):
                while len(row) <= left + column_offset:
                    row.append(None)
                row[left + column_offset] = value

    async def batch_write_values(self, token: str, ranges: list[dict]) -> None:
        for item in ranges:
            await self.write_values(token, item["range"], item["values"])

    async def merge_cells(self, _: str, name: str) -> None:
        left, _, _, _ = self._range(name)
        self.merges.append(
            {
                "start_row_index": 0,
                "end_row_index": 0,
                "start_column_index": left,
                "end_column_index": left + 1,
            }
        )

    async def insert_columns(self, _: str, __: str, start: int) -> None:
        self.inserted.append(start)
        self.width += 2
        for row in self.rows:
            row[start:start] = [None, None]
        for merge in self.merges:
            if merge["start_column_index"] >= start:
                merge["start_column_index"] += 2
                merge["end_column_index"] += 2


@pytest.mark.asyncio
async def test_new_device_inserts_both_groups_before_summary(monkeypatch, tmp_path: Path) -> None:
    import codex_feishu_bridge.daily_stats as stats

    today = datetime.now(ZoneInfo("Asia/Shanghai"))
    sheet = FakeSheet(today)
    config = BridgeConfig(
        config_path=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        feishu=FeishuConfig(conversation=FeishuAppConfig("app", "TEST_APP_SECRET")),
        daily_stats=DailyStatsConfig(True, "spreadsheet", "sheet", "Asia/Shanghai"),
    )
    sample = QuotaSample(int(today.timestamp()), 90, None, "account", "codex")

    async def read_quota(_: str) -> QuotaSample:
        return sample

    monkeypatch.setenv("TEST_APP_SECRET", "dummy")
    monkeypatch.setattr(stats, "FeishuSheetsClient", lambda *_args, **_kwargs: sheet)
    monkeypatch.setattr(stats, "detect_host_id", lambda: ("host-test", "host"))
    monkeypatch.setattr(
        stats,
        "calculate_daily_counts",
        lambda _config, targets: tuple(
            DailyCount(day, 3 if day == today.date() else 4, 1) for day in targets
        ),
    )
    monkeypatch.setattr(stats, "read_weekly_quota", read_quota)
    monkeypatch.setattr(stats, "save_sample", lambda *_args: [sample])

    result = await sync_daily_stats(config)

    assert sheet.inserted == [5, 7]
    assert (result.column_start_index, result.quota_column_start_index) == (5, 7)
    assert result.summary_column_start_index == 9
    assert result.summary_row_count == 2
    assert sheet.rows[0][1:11:2] == ["旧设备", "旧设备·额度", "新设备", "新设备·额度", "全设备汇总"]
    assert sheet.rows[2][1:5] == [1, 0, "90%", "1%"]
    assert sheet.rows[2][9]["text"].startswith('=SUMIF(B$2:I$2,"总任务"')
