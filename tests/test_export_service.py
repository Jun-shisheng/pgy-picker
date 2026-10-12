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


def test_no_metadata_leaks_into_the_sheet(tmp_path):
    """筛选条件、来源接口这些**不能出现在表里**。"""
    path = export_excel([ROW], tmp_path / "提报.xlsx")
    ws = load_workbook(path)["达人提报"]

    text = " ".join(str(ws.cell(row=r, column=c).value or "")
                    for r in range(1, ws.max_row + 1)
                    for c in range(1, len(COLUMNS) + 1))
    for forbidden in ("元信息", "来源接口", "检索统计", "采集时间", "字段字典"):
        assert forbidden not in text, f"交付表里混进了元信息：{forbidden}"
