from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from codex_feishu_bridge.daily_stats import (
    DailyStatsError,
    FeishuSheetsClient,
    choose_group_column,
    choose_quota_column,
    column_name,
    find_summary_column,
    is_long_task,
    strip_bridge_constraint,
    summary_formula,
    summary_value_ranges,
)


def test_strip_bridge_constraint_before_counting() -> None:
    text = "实际问题\n飞书桥交付约束：若本任务需要把新生成的文件回传给用户，请保存到专用目录。"
    assert strip_bridge_constraint(text) == "实际问题"


@pytest.mark.parametrize(
    ("text", "duration_ms", "expected"),
    [
        ("甲" * 101, 180_001, True),
        ("甲" * 100, 180_001, False),
        ("甲" * 101, 180_000, False),
        ("短任务", 600_001, True),
        ("短任务", 600_000, False),
        ("甲" * 101 + "飞书桥交付约束：" + "乙" * 300, 180_001, True),
        ("甲" * 100 + "飞书桥交付约束：" + "乙" * 300, 180_001, False),
    ],
)
def test_long_task_boundaries(text: str, duration_ms: int, expected: bool) -> None:
    assert is_long_task(text, duration_ms) is expected


def test_choose_group_preserves_existing_and_appends() -> None:
    rows = [
        ["日期", "Codex-其他主机", "", "", ""],
        ["", "总任务", "长任务", "", ""],
    ]
    assert choose_group_column(rows, "Codex-本机", column_count=5) == 3
    assert choose_group_column(rows, "Codex-其他主机", column_count=5) == 1


def test_duplicate_bot_group_is_rejected() -> None:
    rows = [
        ["日期", "Codex-本机", "", "Codex-本机", ""],
        ["", "总任务", "长任务", "总任务", "长任务"],
    ]
    with pytest.raises(DailyStatsError, match="duplicate"):
        choose_group_column(rows, "Codex-本机", column_count=5)


def test_quota_columns_append_without_moving_other_hosts() -> None:
    rows = [
        ["日期", "Codex-本机", None, "Codex-其他主机", None, "", "", ""],
        [None, "总任务", "长任务", "总任务", "长任务", "", "", ""],
    ]
    assert choose_quota_column(rows, "Codex-本机", column_count=8) == 5
    rows[0][5] = "Codex-本机·额度"
    rows[1][5:7] = ["剩余周额度", "当日观测用量"]
    assert choose_quota_column(rows, "Codex-本机", column_count=8) == 5
    assert choose_group_column(rows, "Codex-其他主机", column_count=8) == 3


def test_column_names() -> None:
    assert [column_name(index) for index in (0, 25, 26, 99)] == ["A", "Z", "AA", "CV"]


def test_summary_header_and_formulas_exclude_summary_and_quota_columns() -> None:
    headers = [
        ["日期", "Codex-本机", None, "Codex-另一台", None, "Codex-本机·额度", None, "全设备汇总"],
        [
            None,
            "总任务",
            "长任务",
            "总任务",
            "长任务",
            "剩余周额度",
            "当日观测用量",
            "总任务",
            "长任务",
        ],
    ]
    assert find_summary_column(headers, 100) == 7
    assert summary_formula(3, 7, 100, "总任务") == (
        '=SUMIF(B$2:G$2,"总任务",B3:G3)+SUMIF(J$2:CV$2,"总任务",J3:CV3)'
    )
    assert summary_formula(3, 7, 100, "长任务") == (
        '=SUMIF(B$2:G$2,"长任务",B3:G3)+SUMIF(J$2:CV$2,"长任务",J3:CV3)'
    )


def test_summary_rejects_duplicate_or_malformed_headers() -> None:
    with pytest.raises(DailyStatsError, match="duplicate summary"):
        find_summary_column(
            [
                ["日期", "全设备汇总", None, "全设备汇总"],
                [None, "总任务", "长任务", "总任务", "长任务"],
            ],
            5,
        )
    with pytest.raises(DailyStatsError, match="unexpected subcolumns"):
        find_summary_column([["日期", "全设备汇总"], [None, "长任务", "总任务"]], 3)


def test_summary_backfill_groups_only_contiguous_date_rows() -> None:
    rows = {date(2026, 10, 3): 3, date(2026, 10, 2): 4, date(2026, 9, 30): 6}
    ranges = summary_value_ranges("sheet", rows, 7, 100)
    assert [item["range"] for item in ranges] == ["sheet!H3:I4", "sheet!H6:I6"]
    assert ranges[0]["values"][0][0]["text"] == summary_formula(3, 7, 100, "总任务")
    assert ranges[0]["values"][1][1]["text"] == summary_formula(4, 7, 100, "长任务")


@pytest.mark.asyncio
async def test_insert_row_inherits_body_style_from_following_row() -> None:
    client = object.__new__(FeishuSheetsClient)
    client.request = AsyncMock(return_value={})

    await client.insert_row("spreadsheet", "sheet", 3)

    client.request.assert_awaited_once_with(
        "POST",
        "/sheets/v2/spreadsheets/spreadsheet/insert_dimension_range",
        operation="sheet.rows.insert",
        json_body={
            "dimension": {
                "sheetId": "sheet",
                "majorDimension": "ROWS",
                "startIndex": 2,
                "endIndex": 3,
            },
            "inheritStyle": "AFTER",
        },
    )


@pytest.mark.asyncio
async def test_insert_columns_keeps_summary_to_the_right() -> None:
    client = object.__new__(FeishuSheetsClient)
    client.request = AsyncMock(return_value={})

    await client.insert_columns("spreadsheet", "sheet", 7)

    client.request.assert_awaited_once_with(
        "POST",
        "/sheets/v2/spreadsheets/spreadsheet/insert_dimension_range",
        operation="sheet.columns.insert",
        json_body={
            "dimension": {
                "sheetId": "sheet",
                "majorDimension": "COLUMNS",
                "startIndex": 7,
                "endIndex": 9,
            },
            "inheritStyle": "BEFORE",
        },
    )
