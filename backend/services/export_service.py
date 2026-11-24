"""把达人行渲染成交付给客户的 Excel。

**只有一张表，严格对齐客户最新模板**
（`资料和说明/最新模板/ai需求.xlsx`，2026-11-10 生效）：
10 列、一人一行、不额外加列、**不带任何元信息**。

「元信息」（采集时间 / 筛选条件 / 来源接口 / 检索统计）曾经放在第二张表里，
**2026-10-12 按用户要求去掉了**：交付物就是达人名单，别的东西混进去只会让人追问
「这行是什么」——它们属于运行日志，去「设置 → 运行日志」看。
2026-11-10 的模板变更：删掉「量级 / 序号 / 曝光中位数 / 互动中位数」，
新增「赞藏（w）」与「主页近10篇内容的点赞中位数」；「粉丝量」改为以 w 为单位。
2026-11-16 客户要求：昵称与粉丝量之间新增「小红书号」列（详情接口 `redId`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from backend.providers.pugongying.fields import COLUMNS
from backend.providers.pugongying.mapper import FLAGS_KEY, NO_QUOTE_KEY, OVER_BUDGET_KEY

# 列宽（按模板实际观感给，够看即可）。表头里有很长的中文列名，留足宽度。
_COLUMN_WIDTH = {
    "昵称": 22,
    "小红书号": 16,
    "粉丝量（w）": 13,
    "赞藏（w）": 13,
    # 现在会输出全部 contentTags + featureTags，可能 4–7 个，留宽一点。
    "类型": 34,
    "ip": 20,
    "图文报价": 12,
    "视频报价": 12,
    "主页链接（非蒲公英链接）": 52,
    "主页近10篇内容的点赞中位数": 30,
}
# 右对齐的列。⚠️ **判据必须是「列」而不是「值的类型」** —— 缺报价那格写的是
# 字符串 `None`（`mapper.NO_PRICE_TEXT`），按 `isinstance(value, (int, float))`
# 判会把它留成左对齐，跟同列的报价数字对不齐（用户 2026-10-13 反馈：
# 「None 靠右边和数字一样，不要靠左边」）。
_RIGHT_ALIGNED = {
    "粉丝量（w）", "赞藏（w）", "图文报价", "视频报价",
    "主页近10篇内容的点赞中位数",
}
_HEADER_FILL = PatternFill("solid", fgColor="F0F0F0")
# 报价超出该档位上限的单元格底色（浅琥珀）。**只标单元格，不加列、不改结构** ——
# 客户表头就是这些列，不能因为要标记就多一列出来。
_OVER_BUDGET_FILL = PatternFill("solid", fgColor="FFE9A8")
_OVER_BUDGET_FONT = Font(bold=True, color="9C5700")


def _write_header(ws) -> None:
    for index, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=index, value=name)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(index)].width = _COLUMN_WIDTH.get(name, 14)


def export_excel(rows: Iterable[dict], path: str | Path) -> Path:
    """写出 Excel：**只有客户模板那一张表**。

    Args:
        rows: `mapper.map_page()` 的产物（键为 `COLUMNS` 里的列名）。
        path: 输出路径（.xlsx）。
    """
    rows = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "达人提报"
    _write_header(ws)

    for row_index, row in enumerate(rows, start=2):
        flags = row.get(FLAGS_KEY) or {}
        over = set(flags.get(OVER_BUDGET_KEY) or ())      # 如 {"图文报价"}
        no_quote = set(flags.get(NO_QUOTE_KEY) or ())     # 如 {"视频报价"}
        for column_index, name in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row_index, column=column_index, value=row.get(name))
            # 数字列一律右对齐 —— 连缺报价写的那个 `None` 也一起，它得跟同列的
            # 报价数字对齐（见 `_RIGHT_ALIGNED` 的说明）。
            if name in _RIGHT_ALIGNED:
                cell.alignment = Alignment(horizontal="right")
            # 这一项超了预算上限 → 给这个单元格上色（"一个超一个没超"要能一眼看出来）
            if name in over:
                cell.fill = _OVER_BUDGET_FILL
                cell.font = _OVER_BUDGET_FONT
            elif name in no_quote:
                # 平台没有这个报价 —— **与超预算同一种标注**（底色 + 字体都一致）。
                cell.fill = _OVER_BUDGET_FILL
                cell.font = _OVER_BUDGET_FONT

    ws.freeze_panes = "A2"

    wb.save(path)
    return path
