"""把达人行渲染成交付给客户的 Excel。

**只有一张表，严格对齐客户提报表头**（`资料和说明/横店影视中秋国庆达人提报表头.xlsx`）：
11 列、一人一行、不额外加列、**不带任何元信息**。

「元信息」（采集时间 / 筛选条件 / 来源接口 / 检索统计）曾经放在第二张表里，
**2026-10-12 按用户要求去掉了**：交付物就是达人名单，别的东西混进去只会让人追问
「这行是什么」——它们属于运行日志，去「设置 → 运行日志」看。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from backend.providers.pugongying.fields import COLUMNS
from backend.providers.pugongying.mapper import FLAGS_KEY, NO_QUOTE_KEY, OVER_BUDGET_KEY

# 列宽（按客户表头实际观感给，够看即可）
#
# 「量级」要装下 `超头部KOL(40w-50w)`：中文按 2 个单位算，
# 3 个中文字(6) + `KOL`(3) + `(40w-50w)`(9) = 18，留点余量给 20。
# 窄了会被右边的「序号」挡住（用户 2026-10-12 反馈）。
_COLUMN_WIDTH = {
    "量级": 20, "序号": 7, "昵称": 22, "粉丝量": 12, "主页链接": 42,
    "IP": 18, "类型": 16, "曝光中位数": 14, "互动中位数": 14, "图文": 12, "视频": 12,
}
# 右对齐的列（与界面预览表 `SelectionPage` 的 `NUMERIC` 同一组）。
# ⚠️ **判据必须是「列」而不是「值的类型」** —— 缺报价那格写的是字符串 `None`
# （`mapper.NO_PRICE_TEXT`），按 `isinstance(value, (int, float))` 判会把它留成左对齐，
# 跟同列的报价数字对不齐（用户 2026-10-13 反馈：「None 靠右边和数字一样，不要靠左边」）。
_RIGHT_ALIGNED = {"序号", "粉丝量", "曝光中位数", "互动中位数", "图文", "视频"}
_HEADER_FILL = PatternFill("solid", fgColor="F0F0F0")
# 报价超出该档位上限的单元格底色（浅琥珀）。**只标单元格，不加列、不改结构** ——
# 客户表头是 11 列，不能因为要标记就多一列出来。
_OVER_BUDGET_FILL = PatternFill("solid", fgColor="FFE9A8")
_OVER_BUDGET_FONT = Font(bold=True, color="9C5700")
# 「平台没有这个报价」的格子**和超预算一样**：同样的底色、同样的字体
# （用户 2026-10-13：「和超预算的一样不是白底变个颜色」）。
# 两者对客户是同一类事 —— "这一格要人看一眼"，排序里也同等对待（都是 `RANK_GROUP_KEY=1`）。
# 区别只在格子里写什么：超预算是数字，缺报价是 `None` 那个词（见 mapper.NO_PRICE_TEXT）。

# 量级分组之间的分隔线（用户 2026-10-12 要求：40w-50w 过渡到 30w-40w 处"划一下"）。
# 表格本来就按量级降序排，所以"量级这一列的值变了"就是分组边界 ——
# 在**上一组的最后一行**划一条下框线，一档一档看的时候不会串行。
# 用 medium 而不是 thin：thin 打印/截图后基本看不见，起不到分隔作用。
_GROUP_SEPARATOR = Border(bottom=Side(style="medium", color="808080"))


def _write_header(ws) -> None:
    for index, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=index, value=name)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(index)].width = _COLUMN_WIDTH.get(name, 14)


def export_excel(rows: Iterable[dict], path: str | Path) -> Path:
    """写出 Excel：**只有客户表头那一张表**。

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
        over = set(flags.get(OVER_BUDGET_KEY) or ())      # 如 {"图文"}
        no_quote = set(flags.get(NO_QUOTE_KEY) or ())     # 如 {"视频"}
        for column_index, name in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row_index, column=column_index, value=row.get(name))
            # 数字列一律右对齐 —— 连缺报价写的那个 `None` 也一起，它得跟同列的报价数字
            # 对齐（见 `_RIGHT_ALIGNED` 的说明）。
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

    # 量级分组之间划线：行的顺序是「量级降序」，所以量级一变就是换档了，
    # 在**上一组的末行**整行划一条下框线（见 _GROUP_SEPARATOR 的说明）。
    for index in range(len(rows) - 1):
        if rows[index].get("量级") != rows[index + 1].get("量级"):
            last_row_of_group = index + 2          # +1 跳过表头，+1 落到"上一行"
            for column_index in range(1, len(COLUMNS) + 1):
                cell = ws.cell(row=last_row_of_group, column=column_index)
                cell.border = _GROUP_SEPARATOR

    ws.freeze_panes = "A2"

    wb.save(path)
    return path
