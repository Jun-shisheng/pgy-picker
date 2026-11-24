"""交付 Excel 的形状 —— 以客户最新模板为准（2026-11-10）。

**只有客户表头那一张表**（2026-10-12 用户要求「有达人信息就可以了，不要元信息的那一个」）。
2026-11-10 模板删掉量级/序号/曝光/互动中位数，新增赞藏（w）与
主页近10篇点赞中位数；2026-11-16 在昵称与粉丝量之间加「小红书号」。
表头字符串必须逐字照抄模板。
"""

from __future__ import annotations

from openpyxl import load_workbook

from backend.providers.pugongying.fields import COLUMNS
from backend.services.export_service import export_excel

ROW = {
    "昵称": "某达人",
    "小红书号": "49321476371",
    "粉丝量（w）": 12.3,
    "赞藏（w）": 456.7,
    "类型": "母婴 vlog",
    "ip": "浙江 杭州",
    "图文报价": 3000,
    "视频报价": "None",
    "主页链接（非蒲公英链接）": "https://www.xiaohongshu.com/user/profile/u1",
    "主页近10篇内容的点赞中位数": 88,
}


def test_header_is_exactly_the_customer_template():
    """10 列、逐字一致，且不再出现旧列。"""
    expected = [
        "昵称", "小红书号", "粉丝量（w）", "赞藏（w）", "类型", "ip",
        "图文报价", "视频报价", "主页链接（非蒲公英链接）",
        "主页近10篇内容的点赞中位数",
    ]
    assert COLUMNS == expected
    assert "量级" not in COLUMNS and "序号" not in COLUMNS
    assert "曝光中位数" not in COLUMNS and "互动中位数" not in COLUMNS


def test_a_missing_price_cell_is_marked_like_an_over_budget_one(tmp_path):
    """缺报价那格：写 `None` 那个词 + **和超预算同一种标注**（用户 2026-10-13）。

    用户原话：「缺报价的地方写英文 None」「和超预算的一样不是白底变个颜色」。
    所以这里连底色带字体一起比 —— 两处**必须完全一致**，只差格子里写什么。
    """
    normal = {**ROW, "视频报价": 4000, "__flags__": {"over_budget": [], "no_quote": []}}
    missing = {**ROW, "__flags__": {"over_budget": [], "no_quote": ["视频报价"]}}
    over = {**ROW, "图文报价": 20000,
            "__flags__": {"over_budget": ["图文报价"], "no_quote": []}}
    ws = load_workbook(export_excel([normal, missing, over], tmp_path / "提报.xlsx"))["达人提报"]

    video = COLUMNS.index("视频报价") + 1
    image = COLUMNS.index("图文报价") + 1

    missing_cell = ws.cell(row=3, column=video)
    over_cell = ws.cell(row=4, column=image)

    assert missing_cell.value == "None", "缺报价写英文 None"
    assert missing_cell.fill.fgColor.rgb == over_cell.fill.fgColor.rgb, "底色要和超预算一致"
    assert missing_cell.font.color.rgb == over_cell.font.color.rgb
    assert missing_cell.font.bold == over_cell.font.bold
    # 没被标记的格子保持白底
    assert ws.cell(row=2, column=video).fill.fgColor.rgb != over_cell.fill.fgColor.rgb


def test_the_none_text_is_right_aligned_like_the_numbers(tmp_path):
    """缺报价那格写的 `None`，要和同列的报价数字一样**右对齐**（用户 2026-10-13）。

    用户原话：「None 靠右边和数字一样，不要靠左边」。
    ⚠️ 判据是**列**不是值的类型 —— `None` 是字符串，按 `isinstance(value, (int, float))`
    判会把它留成左对齐，跟同列的数字对不齐（这就是当时那个 bug）。
    """
    ws = load_workbook(export_excel([ROW], tmp_path / "提报.xlsx"))["达人提报"]

    video = COLUMNS.index("视频报价") + 1
    image = COLUMNS.index("图文报价") + 1
    name = COLUMNS.index("昵称") + 1

    assert ws.cell(row=2, column=video).value == "None"
    assert ws.cell(row=2, column=video).alignment.horizontal == "right", \
        "缺报价的 None 必须和同列数字一样右对齐"
    assert ws.cell(row=2, column=image).alignment.horizontal == "right", "数字列右对齐"
    # 反向钉住：不是"整张表都右对齐" —— 文本列保持默认（左）。
    assert ws.cell(row=2, column=name).alignment.horizontal != "right"


def test_a_real_number_is_still_a_number_next_to_the_none_text(tmp_path):
    """同一行里：有报价的形态仍是数字，没报价的才是 `None` 那个词。"""
    ws = load_workbook(export_excel([ROW], tmp_path / "提报.xlsx"))["达人提报"]

    assert ws.cell(row=2, column=COLUMNS.index("图文报价") + 1).value == 3000
    assert ws.cell(row=2, column=COLUMNS.index("视频报价") + 1).value == "None"


def test_workbook_has_exactly_one_sheet(tmp_path):
    path = export_excel([ROW], tmp_path / "提报.xlsx")
    wb = load_workbook(path)

    assert wb.sheetnames == ["达人提报"], f"交付物只该有一张表，实际：{wb.sheetnames}"


def test_header_and_rows_follow_the_template(tmp_path):
    path = export_excel([ROW, ROW], tmp_path / "提报.xlsx")
    ws = load_workbook(path)["达人提报"]

    header = [ws.cell(row=1, column=i).value for i in range(1, len(COLUMNS) + 1)]
    assert header == COLUMNS, "表头必须与客户模板逐列一致"
    assert ws.max_row == 3, "两行数据 + 一行表头"
    assert ws.max_column == len(COLUMNS), "不能多出模板之外的列"
    assert ws.cell(row=2, column=1).value == "某达人"
    assert ws.cell(row=2, column=COLUMNS.index("赞藏（w）") + 1).value == 456.7
    assert ws.cell(
        row=2, column=COLUMNS.index("主页近10篇内容的点赞中位数") + 1).value == 88


def test_no_metadata_leaks_into_the_sheet(tmp_path):
    """筛选条件、来源接口这些**不能出现在表里**。"""
    path = export_excel([ROW], tmp_path / "提报.xlsx")
    ws = load_workbook(path)["达人提报"]

    text = " ".join(str(ws.cell(row=r, column=c).value or "")
                    for r in range(1, ws.max_row + 1)
                    for c in range(1, len(COLUMNS) + 1))
    for forbidden in ("元信息", "来源接口", "检索统计", "采集时间", "字段字典"):
        assert forbidden not in text, f"交付表里混进了元信息：{forbidden}"
