"""把蒲公英原始达人 JSON 映射成客户交付表的行。

职责单一：**只做映射，不碰网络、不碰浏览器**。这样它可以：
- 离线对历史快照重跑（平台改版时不用重采）；
- 被单测直接喂样例 JSON 覆盖。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import pricing
from .fields import COLUMNS, FIELD_MAP, HOMEPAGE_TEMPLATE, in_range, tier_index, tier_of

# 非交付列的内部标记键。`COLUMNS` 之外的键**不会**写进 Excel，
# 只用来在渲染时决定"给哪个单元格加底色"、以及排序。
FLAGS_KEY = "__flags__"
OVER_BUDGET_KEY = "over_budget"   # 值形如 ["图文"] / ["视频"]
#: 哪一形态**根本没有报价**（值形如 `["图文"]`）。交付表那格是**空的**，
#: 但**要加底色**（用户 2026-10-13：「改为 None」+「并且也底色标注」）——
#: 空的单元格不加标记，会被当成"漏填"而不是"平台没有这个报价"。
NO_QUOTE_KEY = "no_quote"
TIER_INDEX_KEY = "tier_index"     # 档位在报价表里的行序（0 在最上）
RANK_GROUP_KEY = "rank_group"     # 0=图文视频都符合；1=有一个不符（超预算或未有报价）
IMAGE_SORT_KEY = "image_sort"     # 图文报价排序用（未有报价 = 极大）

# 平台没有某个报价时，交付表那格写**英文 `None`**（用户 2026-10-13 明确要求，原话：
# 「缺报价的地方写英文 None」），并且**和超预算的格子一样加底色**（同一句要求的
# 「和超预算的一样」）。
#
# ⚠️ **这是用户点名要的写法，不是漏改** —— 2026-10-12 那版写的是中文文案
# 「未有此报价」，2026-10-13 先被要求改掉、又明确要的就是 `None` 这个英文词。
# 看着像调试残留是对的，但**别顺手"修"成空白或中文**：交付物长什么样是业务定的。
NO_PRICE_TEXT = "None"

_PURE_DIGITS = re.compile(r"^\d+$")


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    return int(float(text)) if text.replace(".", "", 1).isdigit() else None


def _to_price(value: Any) -> int | None:
    """报价 → 整数元。平台给的是 float（如 15000.0）。"""
    number = _to_int(value)
    if number is None:
        return None
    return number if number > 0 else None


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _homepage(value: Any) -> str | None:
    user_id = _to_str(value)
    if not user_id:
        return None
    return HOMEPAGE_TEMPLATE.format(user_id=user_id)


def _content_tag(value: Any, extra: Any) -> str | None:
    """内容类目：`contentTags` 是 [{taxonomy1Tag, taxonomy2Tags}]，取一级类目去重。

    取不到就回落到 `tradeType`（如「日用百货 美妆护肤」）。两者都没有则留空——
    **不编造**。
    """
    tags: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                tag = _to_str(item.get("taxonomy1Tag"))
                if tag and tag not in tags:
                    tags.append(tag)
    if tags:
        return "、".join(tags)
    return _to_str(extra)


def _apply(transform: str, values: list[Any]) -> Any:
    primary = values[0] if values else None
    if transform == "int":
        for candidate in values:
            number = _to_int(candidate)
            # ⚠️ 判 None，不要判 truthy：**0 是合法值**（如「互动中位数 = 0」＝没有互动），
            # 写成 `if number:` 会把 0 当成缺失丢掉，交付表上变成空白（踩过一次）。
            if number is not None:
                return number
        return None
    if transform == "price":
        return _to_price(primary)
    if transform == "homepage":
        return _homepage(primary)
    if transform == "content_tag":
        return _content_tag(primary, values[1] if len(values) > 1 else None)
    return _to_str(primary)


def map_blogger(item: dict) -> dict:
    """单个达人原始 JSON → 一行交付数据（键为 `COLUMNS` 里的列名）。"""
    row: dict[str, Any] = {}
    for column, spec in FIELD_MAP.items():
        values = [item.get(f) for f in spec["fields"]]
        row[column] = _apply(spec["transform"], values)

    # 派生与自有列
    fans = row.get("粉丝量")
    row["量级"] = tier_of(fans)   # 如 `头部KOL(10w-20w)`；粉丝数见「粉丝量」列，不重复
    row["序号"] = None          # 渲染时按行序填

    # 报价：先取出数值供排序用，再把「没有的」写成 `None` 那个词。
    #
    # ⚠️ 顺序要紧：**标记与排序都用数值**（`image_price` / `video_price`），
    # 写进交付表的那个字符串是最后一步的事 —— 一旦先替换成 "None"，
    # 预算判定与排序会拿一个字符串当价格用。
    #
    # ⚠️ 也别把"没有报价"当成"没数据就别进名单"：进不进名单由 `build_gate()` 判
    # （那边"没有报价"要与超预算一样往后排，见 RANK_GROUP_KEY）。两件事。
    image_price, video_price = row.get("图文"), row.get("视频")
    over = pricing.over_kinds(fans, image_price, video_price)
    has_both = image_price is not None and video_price is not None

    # 按 COLUMNS 顺序整理（保证列序稳定）
    ordered = {column: row.get(column) for column in COLUMNS}
    ordered["图文"] = image_price if image_price is not None else NO_PRICE_TEXT
    ordered["视频"] = video_price if video_price is not None else NO_PRICE_TEXT

    # 内部标记：排序键 + 超额/缺报价标记（都不进交付表）
    ordered[FLAGS_KEY] = {
        TIER_INDEX_KEY: tier_index(fans),
        # 0 = 图文视频**都有报价且都在预算内**（最优先）；1 = 有一个不符，往后排。
        # 「不符」既包括超预算，也包括**未有报价**（用户：未有和超报价一样往后排）。
        RANK_GROUP_KEY: 0 if (has_both and not over) else 1,
        IMAGE_SORT_KEY: image_price if image_price is not None else float("inf"),
        OVER_BUDGET_KEY: over,
        # 缺报价的形态：交付表里是空格，靠**底色**告诉人"这不是漏填，是平台没这个价"。
        NO_QUOTE_KEY: [name for name, price in (("图文", image_price), ("视频", video_price))
                       if price is None],
    }
    return ordered


def sort_rows(rows: list[dict]) -> list[dict]:
    """按用户的规则排序并重排序号：

        1. **量级**：按报价表自上而下（超头部40w-50w → … → 尾部1w-5w），量级大的在前
        2. **同量级内**：图文视频都符合的在前；有一个不符（超预算 / 未有报价）的在后
        3. **图文报价**低的在前
        4. **粉丝数**高的在前

    排序键放在行的内部标记里（`__flags__`），**不改交付表的列**。
    """
    def key(row: dict):
        flags = row.get(FLAGS_KEY) or {}
        return (
            flags.get(TIER_INDEX_KEY, 999),
            flags.get(RANK_GROUP_KEY, 1),
            flags.get(IMAGE_SORT_KEY, float("inf")),
            -(row.get("粉丝量") or 0),
        )

    rows.sort(key=key)
    for index, row in enumerate(rows, start=1):
        row["序号"] = index
    return rows


def map_page(items: list[dict], *, start_index: int = 1) -> list[dict]:
    """整页映射，并补上序号。"""
    rows = []
    for offset, item in enumerate(items):
        row = map_blogger(item)
        row["序号"] = start_index + offset
        rows.append(row)
    return rows


def build_gate() -> tuple[Callable[[dict], bool], dict]:
    """构造**统一的「达人有资格进名单吗」判定**。返回 `(keep, stats)`。

    这是把关规则的**唯一实现**。为什么要收拢到一处：规则曾经写在两个地方
    （检索链路 / 样例生成脚本），结果后者漏了预算那道，交付表里混进了
    「图文视频都超预算」的达人 —— 用户一眼就看出来了。**一份规则两处实现，
    必然会分叉。**

    `keep` 作用在**原始达人字典**上（不是映射后的行），因为翻页时要边取边判，
    剔掉的人不计入 `want`，否则只能交出一份短名单。

    **两道关**：
    1. **粉丝档位**：1w ≤ fans < 50w（之外没有对应报价行）；
    2. **预算**：按对应档位算上限，图文/视频**任一在预算内即通过**，两个都超才剔。

    ⚠️ **原来还有第三道「曝光/互动中位数不能为 0／缺失」，2026-10-13 按用户要求删掉了。**
    同事转达的口径是「曝光中位数缺失不影响我们的筛选」，用户拍板：
    **两个中位数都不再参与把关**。实测代价要记住 —— 上一轮因这道关剔掉的 97 条，
    样例**全是「曝光=0 互动=0」**，也就是完全没有任何合作数据的达人；
    现在他们**会进名单**，交付表里那两格就是 `0` 或空白。
    中位数仍然**采集、进表**，只是不再决定进不进名单（它也从来不参与排序）。

    `stats` 里累计剔除原因与样例，供调用方**如实报数**，而不是悄悄丢掉。
    """
    stats: dict = {"out_of_range": 0, "over_budget": 0, "samples": []}

    def keep(item: dict) -> bool:
        fans = item.get("fansNum")
        if not in_range(fans):
            stats["out_of_range"] += 1
            return False

        passed, reason = pricing.within_budget(
            fans, item.get("picturePrice"), item.get("videoPrice"))
        if not passed:
            stats["over_budget"] += 1
            if len(stats["samples"]) < 5:
                stats["samples"].append(f"{item.get('name')}({fans}): {reason}")
            return False
        return True

    return keep, stats


def filter_in_range(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """按粉丝档位过滤**已映射的行**，并重排序号。

    ⚠️ **本函数不含预算把关**，只有粉丝档位。要完整把关请用 `build_gate()`
    —— 那才是唯一实现。留着它是因为「只看粉丝档位」在某些排查场景下有用。
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    for row in rows:
        (kept if in_range(row.get("粉丝量")) else dropped).append(row)
    for index, row in enumerate(kept, start=1):
        row["序号"] = index
    return kept, dropped
