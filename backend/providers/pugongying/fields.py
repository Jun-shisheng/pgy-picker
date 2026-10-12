"""蒲公英响应字段 → 客户交付表列 的映射配置。

**这是数据不是逻辑**：平台改版时改这里，不改代码。

依据：`docs/04-达人字段字典.md`（v0.2，2026-10-12 实测）；
原始字段清单：`docs/evidence/bloggers-*/field-inventory.md`。

⚠️ 口径待定项：
- `曝光中位数` 有多个候选（日常/合作、曝光/阅读），见 `docs/05` D4；
  默认取**合作**口径（客户是投放提报场景），可配置。
- `类型` 可能是内容类目、交易类型或特征标签，见 `docs/05` D2；默认取内容类目一级。
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

COLUMNS: list[str] = [
    "量级", "序号", "昵称", "粉丝量", "主页链接",
    "IP", "类型", "曝光中位数", "互动中位数", "图文", "视频",
]

# 「量级」列由粉丝量派生，不由 FIELD_MAP 采（见 mapper）。
DERIVED_FROM_FANS = "量级"

# ---- 平台字段映射：客户列 → (平台字段列表, 变换) ----
# 变换是给 mapper 看的标记，不是可调用对象（保持本文件可被安全地当作数据读取）。

FIELD_MAP: dict[str, dict] = {
    "昵称":       {"fields": ["name"], "transform": "str"},
    "粉丝量":     {"fields": ["fansNum"], "transform": "int"},
    "主页链接":   {"fields": ["userId"], "transform": "homepage"},
    "IP":         {"fields": ["location"], "transform": "str"},
    "类型":       {"fields": ["contentTags", "tradeType"], "transform": "content_tag"},
    # 曝光中位数 / 互动中位数 / 阅读中位数：**合作笔记口径（Coop）**。
    #
    # ⚠️⚠️ 这块翻过车，2026-10-12 反复了一次，**改动前务必读完**：
    #
    # 平台对同一指标给了**两套**字段，名字只差几个字母：
    #   `accumCoopImpMedinNum30d` / `readMidCoop30` / `interMidCoop30`  = **合作笔记**口径
    #   `accumCommonImpMedinNum30d` / `interMidNum`                    = **日常笔记**口径
    #
    # **UI 上「合作笔记」区块的三行，对应的是 Coop 那套**（用户 2026-10-12 用生耶女孩直接对照确认）：
    #   UI 曝光中位数 42,362(70.31%)  ← accumCoopImpMedinNum30d = 42270（差 0.2%）
    #   UI 阅读中位数  5,161(67.22%)  ← readMidCoop30           =  5154（差 0.1%）
    #   UI 互动中位数    585(78.37%)  ← interMidCoop30          =   559（差 4.6%）
    #
    # **我中间错改成 Common 的原因**：拿「有云详情页 notes_rate.impMedian=109704」去对
    # accumCommon=108873，看到接近就推翻了正确答案 —— 但**详情页那个 impMedian 根本不是
    # UI 合作笔记区显示的数**，参照物选错了。**教训：口径要以客户看到的 UI 原文对照为准，
    # 不要拿另一个页面的同名字段间接推断。**
    "曝光中位数": {"fields": ["accumCoopImpMedinNum30d"], "transform": "int"},
    "互动中位数": {"fields": ["interMidCoop30"], "transform": "int"},
    "图文":       {"fields": ["picturePrice"], "transform": "price"},
    "视频":       {"fields": ["videoPrice"], "transform": "price"},
}

# 自有列（渲染时生成）
DERIVED_COLUMNS: dict[str, str] = {
    "量级": "由「粉丝量」落 TIERS 档位算出",
    "序号": "渲染时按行序生成",
}

HOMEPAGE_TEMPLATE = "https://www.xiaohongshu.com/user/profile/{user_id}"
