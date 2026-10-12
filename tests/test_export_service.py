"""交付 Excel 的形状。

**只有客户表头那一张表** —— 2026-10-12 用户要求「有达人信息就可以了，不要元信息的那一个」。
原来第二张「元信息」表放的是采集时间/筛选条件/来源接口/检索统计，已删。
这条要钉住：它很容易被"顺手加回来"（尤其有人觉得多给点信息没坏处）。
"""

from __future__ import annotations

from openpyxl import load_workbook

from backend.providers.pugongying.fields import COLUMNS
from backend.services.export_service import export_excel

ROW = {
    "量级": "头部KOL(10w-20w)", "序号": 1, "昵称": "某达人", "粉丝量": 123456,
    "主页链接": "https://example.invalid/u/1", "IP": "浙江", "类型": "母婴",
    "曝光中位数": 10000, "互动中位数": 200, "图文": 3000, "视频": "未有此报价",
}


def test_workbook_has_exactly_one_sheet(tmp_path):
    path = export_excel([ROW], tmp_path / "提报.xlsx")
    wb = load_workbook(path)

    assert wb.sheetnames == ["达人提报"], f"交付物只该有一张表，实际：{wb.sheetnames}"


def test_header_is_the_customer_header_and_rows_follow(tmp_path):
    path = export_excel([ROW, ROW], tmp_path / "提报.xlsx")
    ws = load_workbook(path)["达人提报"]

    header = [ws.cell(row=1, column=i).value for i in range(1, len(COLUMNS) + 1)]
    assert header == COLUMNS, "表头必须与客户提报表头逐列一致"
    assert ws.max_row == 3, "两行数据 + 一行表头"
    assert ws.cell(row=2, column=3).value == "某达人"
    # 绝不能多出第 12 列（曾经用"加一列标记"的思路，被 requirement 挡回去了）
    assert ws.cell(row=1, column=len(COLUMNS) + 1).value is None


def test_separator_line_between_tiers(tmp_path):
    """量级换档处划一条下框线（用户 2026-10-12 要求）。

    行按量级降序排，所以「量级这一列变了」就是换档 —— 线划在**上一组的末行**。
    """
    rows = [
        {"量级": "超头部KOL(40w-50w)", "昵称": "a"},
        {"量级": "超头部KOL(40w-50w)", "昵称": "b"},
        {"量级": "头部KOL(30w-40w)", "昵称": "c"},
        {"量级": "头部KOL(30w-40w)", "昵称": "d"},
        {"量级": "腰部KOL(10w-20w)", "昵称": "e"},
    ]
    ws = load_workbook(export_excel(rows, tmp_path / "提报.xlsx"))["达人提报"]

    def bottom(row: int) -> str | None:
        return ws.cell(row=row, column=1).border.bottom.style

    # 数据在 2..6 行；换档发生在 index 1→2 与 3→4，即第 3、5 行
    assert bottom(3) is not None, "第一档的最后一行该有线"
    assert bottom(5) is not None, "第二档的最后一行该有线"
    # 组内不划、最后一档（表尾）也不用划
    assert bottom(2) is None
    assert bottom(4) is None
    assert bottom(6) is None, "最后一行是表的结尾，不需要再划一条"

    # 线要横跨整行，不能只划在「量级」那一列上
    for column in range(1, len(COLUMNS) + 1):
        assert ws.cell(row=3, column=column).border.bottom.style is not None


def test_single_tier_has_no_separator(tmp_path):
    rows = [{"量级": "头部KOL(30w-40w)", "昵称": "a"},
            {"量级": "头部KOL(30w-40w)", "昵称": "b"}]
    ws = load_workbook(export_excel(rows, tmp_path / "提报.xlsx"))["达人提报"]

    assert ws.cell(row=2, column=1).border.bottom.style is None
    assert ws.cell(row=3, column=1).border.bottom.style is None


def test_no_metadata_leaks_into_the_sheet(tmp_path):
    """筛选条件、来源接口这些**不能出现在表里**。"""
    path = export_excel([ROW], tmp_path / "提报.xlsx")
    ws = load_workbook(path)["达人提报"]

    text = " ".join(str(ws.cell(row=r, column=c).value or "")
                    for r in range(1, ws.max_row + 1)
                    for c in range(1, len(COLUMNS) + 1))
    for forbidden in ("元信息", "来源接口", "检索统计", "采集时间", "字段字典"):
        assert forbidden not in text, f"交付表里混进了元信息：{forbidden}"
