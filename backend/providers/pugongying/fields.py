"""蒲公英响应字段 → 客户交付表列 的映射配置。

**这是数据不是逻辑**：平台改版时改这里，不改代码。

依据：客户最新模板 `资料和说明/最新模板/ai需求.xlsx`（2026-11-10 生效）；
原始字段清单：`docs/evidence/bloggers-*/field-inventory.md`。

⚠️ 口径说明（2026-11-10 实测）：
- `类型` = 蒲公英详情页昵称下方那两个标签：`contentTags[0].taxonomy1Tag`
  + `featureTags[0]`（如「母婴 vlog」「生活记录 职场生活」）。
- `赞藏` = 详情接口的 `likeCollectCountInfo`（单位是「个」，输出时换算成 w）。
- `主页近10篇内容的点赞中位数` = **蒲公英详情页**「日常笔记 / 笔记案例」里
  按发布时间最新的 10 篇点赞数中位数（`notes_rate` + `notes_detail` 合并）；
  **2026-11-23 起不再访问小红书主页**（见 `enrich.py` 顶部与 `findings.md` F-60）。
"""

from __future__ import annotations

# ---- 量级档位（粉丝数 → 量级 + 区间）。依据 docs/05 第二节与公司报价表。----
#
# ⚠️ **区间必须一起输出**：同一个「头部KOL」横跨两个价格档（20w-30w 与 10w-20w，
# 报价差一倍以上）。只写「头部KOL」的话，看表的人不知道该套哪一行价。
TIERS: list[tuple[int, int, str, str]] = [
    (400_000, 500_000, "超头部KOL", "40w-50w"),
    (300_000, 400_000, "超头部KOL", "30w-40w"),
    (200_000, 300_000, "头部KOL", "20w-30w"),
    (100_000, 200_000, "头部KOL", "10w-20w"),
    (50_000, 100_000, "腰部KOL", "5w-10w"),
    (10_000, 50_000, "尾部KOL", "1w-5w"),
    # KOC(1k–1w) 已排除；>50w 客户报价表也没有对应行 —— 两者都**不进名单**
]

# 生效粉丝区间（含下界、不含上界）。在此之外的达人本地剔除。
IN_RANGE_LOW = 10_000
IN_RANGE_HIGH = 500_000


def tier_of(fans: int | None) -> str:
    """按粉丝数落档位，返回如 `头部KOL(10w-20w)`；不在任何档位则返回空串。"""
    if fans is None:
        return ""
    for low, high, label, bracket in TIERS:
        if low <= fans < high:
            return f"{label}({bracket})"
    return ""


# 注：「量级」列**只写档位 + 区间**（`头部KOL(10w-20w)`），**不再重复实际粉丝数**
# —— 后面的「粉丝量」列本来就是具体数字，同一个数写两处是冗余（用户 2026-10-12 确认）。
# 但**区间必须留**：同一个「头部KOL」横跨 20w-30w 与 10w-20w 两个价格档，
# 套价时要靠它区分。


def tier_labels() -> list[str]:
    """全部量级名（按报价表顺序），如 `["超头部KOL(40w-50w)", ...]`。

    按量级分配名额时用它做键，报数时人能直接看懂，不用去查档位表。
    """
    return [f"{label}({bracket})" for _low, _high, label, bracket in TIERS]


def tier_index(fans: int | None) -> int:
    """档位在报价表里的**行序**（0 在最上面）。不在任何档位返回一个大数。

    排序用：用户要求「量级的排序依次是从上到下按照表格的」。
    """
    if fans is None:
        return 999
    for index, (low, high, _label, _bracket) in enumerate(TIERS):
        if low <= fans < high:
            return index
    return 999


def in_range(fans: int | None) -> bool:
    """粉丝数是否落在生效档位内（1w ≤ fans < 50w）。"""
    return isinstance(fans, int) and IN_RANGE_LOW <= fans < IN_RANGE_HIGH


# ---- 交付表列定义（顺序即 Excel 列顺序）----
#
# ⚠️ **2026-11-10 起以客户最新模板为准**（`资料和说明/最新模板/ai需求.xlsx`）：
# 一人一行。旧的「量级 / 序号 / 曝光中位数 / 互动中位数」四列已删除
# （用户原话：「曝光中位数那些就去掉，变成一个近10篇点赞的中位数」）。
# **2026-11-16 用户要求**：昵称与粉丝量之间加一列「小红书号」
# （客户截图 `资料和说明/最新模板/屏幕截图 2026-11-10 090959.png` 上标出来的
# 「小红书号：49321476371」，即详情接口的 `redId`，**不是** `userId` 那串 24 位内部 id）。
#
# 表头字符串**逐字照抄模板**（含全角括号与那个小写的 `ip`）——
# 客户拿到表后要按表头对列，差一个字都算错。
COLUMNS: list[str] = [
    "昵称",
    "小红书号",
    "粉丝量（w）",
    "赞藏（w）",
    "类型",
    "ip",
    "图文报价",
    "视频报价",
    "主页链接（非蒲公英链接）",
    "主页近10篇内容的点赞中位数",
]

# ---- 平台字段映射：客户列 → (平台字段列表, 变换) ----
# 变换是给 mapper 看的标记，不是可调用对象（保持本文件可被安全地当作数据读取）。
#
# 数据来源（2026-11-10 实测）：
#   - 除最后一列外均来自**蒲公英达人详情接口** `GET /api/solar/cooperator/user/blogger/{userId}`
#     （`name` / `redId` / `fansNum` / `likeCollectCountInfo` / `contentTags` + `featureTags` /
#     `location` / `picturePrice` / `videoPrice`）；
#   - 最后一列来自**蒲公英详情页**（`pgy.xiaohongshu.com/solar/pre-trade/blogger-detail/{userId}`），
#     由 `enrich.py` 让页面自己发 `notes_rate` / `notes_detail`，合并后算「最新 10 篇点赞中位数」。
#     ⚠️ **2026-11-23 起不再访问 `www.xiaohongshu.com`**（用户拍板，见 `findings.md` F-60）。
#
# 赞藏**不是**搜索接口的字段（搜索返回的 `likeCollectCountInfo` 恒为 null），
# 必须逐个走详情接口；曝光/互动中位数已按用户要求移出交付表。
FIELD_MAP: dict[str, dict] = {
    "昵称":                    {"fields": ["name"], "transform": "str"},
    # 小红书号 = 详情页昵称下方那串数字（客户截图里的「小红书号：49321476371」），
    # 接口字段是 `redId`。⚠️ **不是 `userId`**（那是 24 位内部 id，用于拼主页链接）。
    "小红书号":                {"fields": ["redId"], "transform": "str"},
    "粉丝量（w）":              {"fields": ["fansNum"], "transform": "wan"},
    "赞藏（w）":                {"fields": ["likeCollectCountInfo"], "transform": "wan"},
    "类型":                    {"fields": ["contentTags", "featureTags"], "transform": "content_type"},
    "ip":                      {"fields": ["location"], "transform": "str"},
    "图文报价":                 {"fields": ["picturePrice"], "transform": "price"},
    "视频报价":                 {"fields": ["videoPrice"], "transform": "price"},
    "主页链接（非蒲公英链接）":   {"fields": ["userId"], "transform": "homepage"},
    "主页近10篇内容的点赞中位数": {"fields": ["homepageLikeMedian"], "transform": "int"},
}

# 自有列（渲染时生成）
DERIVED_COLUMNS: dict[str, str] = {}

HOMEPAGE_TEMPLATE = "https://www.xiaohongshu.com/user/profile/{user_id}"
