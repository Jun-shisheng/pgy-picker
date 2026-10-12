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
TIER_INDEX_KEY = "tier_index"     # 档位在报价表里的行序（0 在最上）
RANK_GROUP_KEY = "rank_group"     # 0=图文视频都符合；1=有一个不符（超预算或未有报价）
IMAGE_SORT_KEY = "image_sort"     # 图文报价排序用（未有报价 = 极大）

# 没有某个报价时在交付表里显示的文案（用户 2026-10-12：「显示未有此报价」）。
NO_PRICE_TEXT = "未有此报价"

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

    # 报价：先取出数值供排序用，再把「没有的」写成文案
    # （用户 2026-10-12：「没有某个报价的显示未有此报价」）。
    image_price, video_price = row.get("图文"), row.get("视频")
    over = pricing.over_kinds(fans, image_price, video_price)
    has_both = image_price is not None and video_price is not None

    # 按 COLUMNS 顺序整理（保证列序稳定）
    ordered = {column: row.get(column) for column in COLUMNS}
    ordered["图文"] = image_price if image_price is not None else NO_PRICE_TEXT
    ordered["视频"] = video_price if video_price is not None else NO_PRICE_TEXT

    # 内部标记：排序键 + 超额标记（都不进交付表）
    ordered[FLAGS_KEY] = {
        TIER_INDEX_KEY: tier_index(fans),
        # 0 = 图文视频**都有报价且都在预算内**（最优先）；1 = 有一个不符，往后排。
        # 「不符」既包括超预算，也包括**未有报价**（用户：未有和超报价一样往后排）。
        RANK_GROUP_KEY: 0 if (has_both and not over) else 1,
        IMAGE_SORT_KEY: image_price if image_price is not None else float("inf"),
        OVER_BUDGET_KEY: over,
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


def median_of(item: dict, column: str) -> int | None:
    """按 `FIELD_MAP` 取原始达人字典里某一列的数值（如「曝光中位数」）。

    **故意复用 `FIELD_MAP`**：原始字段名（`accumCoopImpMedinNum30d` 之类）
    只写在映射表那一处。把关逻辑里再抄一遍的话，平台改字段时必然漏改一处。
    """
    spec = FIELD_MAP.get(column)
    if spec is None:
        return None
    return _apply(spec["transform"], [item.get(f) for f in spec["fields"]])


def build_gate() -> tuple[Callable[[dict], bool], dict]:
    """构造**统一的「达人有资格进名单吗」判定**。返回 `(keep, stats)`。

    这是把关规则的**唯一实现**。为什么要收拢到一处：规则曾经写在两个地方
    （检索链路 / 样例生成脚本），结果后者漏了预算那道，交付表里混进了
    「图文视频都超预算」的达人 —— 用户一眼就看出来了。**一份规则两处实现，
    必然会分叉。**

    `keep` 作用在**原始达人字典**上（不是映射后的行），因为翻页时要边取边判，
    剔掉的人不计入 `want`，否则只能交出一份短名单。

    三道关：
    1. **粉丝档位**：1w ≤ fans < 50w（之外没有对应报价行）；
    2. **中位数不能为 0／缺失**：曝光中位数或互动中位数为 0 的达人没有可参考的
       数据表现，不进名单（用户 2026-10-12）。⚠️ 这是**把关，不是排序指标** ——
       排序规则不变（量级 → 符合度 → 图文报价 → 粉丝数），**不按中位数排**；
    3. **预算**：按对应档位算上限，图文/视频**任一在预算内即通过**，两个都超才剔。

    `stats` 里累计剔除原因与样例，供调用方**如实报数**，而不是悄悄丢掉。
    """
    stats: dict = {"out_of_range": 0, "no_median": 0, "over_budget": 0,
                   "samples": [], "median_samples": []}

    def keep(item: dict) -> bool:
        fans = item.get("fansNum")
        if not in_range(fans):
            stats["out_of_range"] += 1
            return False

        # ⚠️ 这里判 falsy 是**故意的**：0 与 None 都算"没有数据表现"。
        # 别照着 mapper._apply 那条注释（那里判 None 是因为"0 是合法值，不该显示成空白"）
        # 把这里改成只判 None —— 两处判的是**两件事**：那边管"表格里显示什么"，
        # 这边管"这个人进不进名单"。
        exposure = median_of(item, "曝光中位数")
        interaction = median_of(item, "互动中位数")
        if not exposure or not interaction:
            stats["no_median"] += 1
            if len(stats["median_samples"]) < 5:
                stats["median_samples"].append(
                    f"{item.get('name')}: 曝光={exposure} 互动={interaction}")
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
