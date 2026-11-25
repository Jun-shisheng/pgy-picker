"""把蒲公英/小红书原始 JSON 映射成交付表的行。

职责单一：**只做映射，不碰网络、不碰浏览器**。这样它可以：
- 离线对历史快照重跑（平台改版时不用重采）；
- 被单测直接喂样例 JSON 覆盖。

交付表固定 10 列（2026-11-10 定稿，2026-11-16 加「小红书号」）。
「量级 / 序号 / 曝光中位数 / 互动中位数」已移出交付表；
「赞藏」与「主页近10篇点赞中位数」由采集层补进原始字典后再映射。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import pricing
from .fields import COLUMNS, FIELD_MAP, HOMEPAGE_TEMPLATE, in_range, tier_index

# 非交付列的内部标记键。`COLUMNS` 之外的键**不会**写进 Excel，
# 只用来在渲染时决定"给哪个单元格加底色"、以及排序。
FLAGS_KEY = "__flags__"
OVER_BUDGET_KEY = "over_budget"   # 值形如 ["图文报价"] / ["视频报价"]
#: 哪一形态**根本没有报价**（值形如 `["图文报价"]`）。交付表那格写 `None`，
#: 但**要加底色**（用户 2026-10-13：「改为 None」+「并且也底色标注」）——
#: 空的单元格不加标记，会被当成"漏填"而不是"平台没有这个报价"。
NO_QUOTE_KEY = "no_quote"
TIER_INDEX_KEY = "tier_index"     # 档位在报价表里的行序（0 在最上）
RANK_GROUP_KEY = "rank_group"     # 0=图文视频都符合；1=有一个不符（超预算或未有报价）
IMAGE_SORT_KEY = "image_sort"     # 图文报价排序用（未有报价 = 极大）
#: 精确粉丝数（个）。交付表里只有四舍五入到 w 的「粉丝量（w）」，
#: 但排序必须用精确值，否则 466299 和 466300 会变成同一个数。
FANS_KEY = "fans"

# 平台没有某个报价时，交付表那格写**英文 `None`**（用户 2026-10-13 明确要求，原话：
# 「缺报价的地方写英文 None」），并且**和超预算的格子一样加底色**（同一句要求的
# 「和超预算的一样」）。
#
# ⚠️ **这是用户点名要的写法，不是漏改** —— 2026-10-12 那版写的是中文文案
# 「未有此报价」，2026-10-13 先被要求改掉、又明确要的就是 `None` 这个英文词。
# 看着像调试残留是对的，但**别顺手"修"成空白或中文**：交付物长什么样是业务定的。
NO_PRICE_TEXT = "None"

IMAGE_COLUMN = "图文报价"
VIDEO_COLUMN = "视频报价"

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


def _to_wan(value: Any) -> float | None:
    """个数 → 万（保留 1 位小数）。如 466326 → 46.6，61542120 → 6154.2。

    ⚠️ **0 是合法值**（没有赞藏），必须返回 0.0 而不是 None。
    """
    number = _to_int(value)
    if number is None:
        return None
    return round(number / 10_000, 1)


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


def _content_type(content_tags: Any, feature_tags: Any, trade_type: Any) -> str | None:
    """「类型」= **所有内容类目 + 所有特征标签**（不含人设标签）。

    2026-11-10 用户两次校准后的口径：
    - ❌ **不要** `personalTags`（如 3-6岁 / 妈妈 / 整理师 / 创业者）——
      它们是"人设/家庭阶段"，不是内容类型；
    - ✅ **要** 所有 `contentTags[].taxonomy1Tag`（如 家居家装、时尚、影视综资讯）
      和所有 `featureTags`（如 vlog、极简主义、职场生活、隔离防晒）；
    - 去重、保持原顺序、空格分隔；
    - 取不到任何标签时，才回落到 `tradeType` 第一个词。

    示例一：`影视综资讯 时尚 ootd 测评`（4 个）；
    示例二：`家居家装 时尚 极简主义 职场生活 隔离防晒 vlog 教程`。
    """
    names: list[str] = []
    if isinstance(content_tags, list):
        for item in content_tags:
            if isinstance(item, dict):
                tag = _to_str(item.get("taxonomy1Tag"))
                if tag and tag not in names:
                    names.append(tag)
    if isinstance(feature_tags, list):
        for item in feature_tags:
            tag = _to_str(item)
            if tag and tag not in names:
                names.append(tag)
    if not names:
        fallback = _to_str(trade_type)
        if fallback:
            names.append(fallback.split()[0])
    return " ".join(names) or None


def _apply(transform: str, values: list[Any]) -> Any:
    primary = values[0] if values else None
    if transform == "int":
        for candidate in values:
            number = _to_int(candidate)
            # ⚠️ 判 None，不要判 truthy：**0 是合法值**（如「赞藏 = 0」＝没有互动），
            # 写成 `if number:` 会把 0 当成缺失丢掉，交付表上变成空白（踩过一次）。
            if number is not None:
                return number
        return None
    if transform == "wan":
        return _to_wan(primary)
    if transform == "price":
        return _to_price(primary)
    if transform == "homepage":
        return _homepage(primary)
    if transform == "content_type":
        padded = values + [None, None, None]
        return _content_type(padded[0], padded[1], padded[2])
    return _to_str(primary)


def map_blogger(item: dict) -> dict:
    """单个达人原始 JSON → 一行交付数据（键为 `COLUMNS` 里的列名）。"""
    row: dict[str, Any] = {}
    for column, spec in FIELD_MAP.items():
        values = [item.get(f) for f in spec["fields"]]
        row[column] = _apply(spec["transform"], values)

    # 报价：先取出数值供排序用，再把「没有的」写成 `None` 那个词。
    #
    # ⚠️ 顺序要紧：**标记与排序都用数值**（`image_price` / `video_price`），
    # 写进交付表的那个字符串是最后一步的事 —— 一旦先替换成 "None"，
    # 预算判定与排序会拿一个字符串当价格用。
    #
    # ⚠️ 也别把"没有报价"当成"没数据就别进名单"：进不进名单由 `build_gate()` 判
    # （那边"没有报价"要与超预算一样往后排，见 RANK_GROUP_KEY）。两件事。
    fans = _to_int(item.get("fansNum"))
    image_price, video_price = row.get(IMAGE_COLUMN), row.get(VIDEO_COLUMN)
    over = pricing.over_kinds(fans, image_price, video_price)
    has_both = image_price is not None and video_price is not None

    ordered = {column: row.get(column) for column in COLUMNS}
    ordered[IMAGE_COLUMN] = image_price if image_price is not None else NO_PRICE_TEXT
    ordered[VIDEO_COLUMN] = video_price if video_price is not None else NO_PRICE_TEXT

    # 内部标记：排序键 + 超额/缺报价标记（都不进交付表）
    ordered[FLAGS_KEY] = {
        TIER_INDEX_KEY: tier_index(fans),
        # 0 = 图文视频**都有报价且都在预算内**（最优先）；1 = 有一个不符，往后排。
        # 「不符」既包括超预算，也包括**未有报价**（用户：未有和超报价一样往后排）。
        RANK_GROUP_KEY: 0 if (has_both and not over) else 1,
        IMAGE_SORT_KEY: image_price if image_price is not None else float("inf"),
        OVER_BUDGET_KEY: [f"{name}报价" for name in over],
        NO_QUOTE_KEY: [name for name, price in ((IMAGE_COLUMN, image_price),
                                                (VIDEO_COLUMN, video_price))
                       if price is None],
        FANS_KEY: fans,
    }
    return ordered


def sort_rows(rows: list[dict]) -> list[dict]:
    """按用户的规则排序（**不改交付表的列**）：

        1. **量级**：按报价表自上而下（超头部40w-50w → … → 尾部1w-5w），量级大的在前
        2. **同量级内**：图文视频都符合的在前；有一个不符（超预算 / 未有报价）的在后
        3. **图文报价**低的在前
        4. **粉丝数**高的在前

    排序键放在行的内部标记里（`__flags__`）。交付表里已经没有「量级 / 序号」列，
    但顺序仍按档位走 —— 看名单时还是按报价档位从高到低。
    """
    def key(row: dict):
        flags = row.get(FLAGS_KEY) or {}
        return (
            flags.get(TIER_INDEX_KEY, 999),
            flags.get(RANK_GROUP_KEY, 1),
            flags.get(IMAGE_SORT_KEY, float("inf")),
            -(flags.get(FANS_KEY) or 0),
        )

    rows.sort(key=key)
    return rows


def map_page(items: list[dict]) -> list[dict]:
    """整页映射。"""
    return [map_blogger(item) for item in items]


def region_matches(value: Any, regions: str | list[str] | tuple[str, ...] | None) -> bool:
    """达人地域是否命中勾选的地域（**多选是「或」**，与平台一致）。

    界面存的是网页版同款的**路径**（如 `"中国 安徽 合肥 蜀山区"`）：

    - 完整路径（以「中国」开头）：去掉「中国」后，要求达人地域**按顺序以这些级开头**；
      这样 `"中国 河北"` 只收河北，不会因为平台把路径当 OR 而混进北京/天津；
    - 只有一级名称（测试/旧调用）：在该达人地域的各级里**出现过**即算；
    - `"中国"`：等价于不限（中国范围内全收）。

    这里再本地核对一次：万一平台哪天把该参数静默忽略（粉丝区间就出过这种事，
    findings），交出去的名单也不会混进外地人。
    """
    if isinstance(regions, (list, tuple, set)):
        wanted_paths = [str(item).strip() for item in regions if str(item).strip()]
    else:
        text = str(regions or "").strip()
        wanted_paths = [text] if text else []
    if not wanted_paths:
        return True

    tokens = str(value or "").split()
    if not tokens:
        return False

    for path in wanted_paths:
        parts = path.split()
        if not parts:
            continue
        if parts[0] == "中国":
            parts = parts[1:]
            if not parts:
                return True          # 中国 = 不限
            if tokens[:len(parts)] == parts:
                return True
        else:
            # 只有名称（没有国家前缀）：在达人地域的各级里出现过即算。
            if any(part in tokens for part in parts):
                return True
    return False


def build_gate(region: str | list[str] | tuple[str, ...] | None = None
               ) -> tuple[Callable[[dict], bool], dict]:
    """构造**统一的「达人有资格进名单吗」判定**。返回 `(keep, stats)`。

    这是把关规则的**唯一实现**。为什么要收拢到一处：规则曾经写在两个地方
    （检索链路 / 样例生成脚本），结果后者漏了预算那道，交付表里混进了
    「图文视频都超预算」的达人 —— 用户一眼就看出来了。**一份规则两处实现，
    必然会分叉。**

    `keep` 作用在**原始达人字典**上（不是映射后的行），因为翻页时要边取边判，
    剔掉的人不计入 `want`，否则只能交出一份短名单。

    **四道关**（`region` 为空时第二道不生效）：
    1. **粉丝档位**：1w ≤ fans < 50w（之外没有对应报价行）；
    2. **地域**：用户选了省/市时，达人的 `location` 必须命中该地域；
    3. **阅读中位数（日常）/ 互动中位数（日常）**：`clickMidNum` / `interMidNum`
       必须 > 0；为空或 0 的剔除（用户 2026-11-19：「为空，0的不要，其他都符合」
       「这两个有空的就不要」）。这两个字段**不输出到 Excel**；
    4. **预算**：按对应档位算上限，图文/视频**任一在预算内即通过**，两个都超才剔。

    ⚠️ **原来还有一道「曝光/互动中位数不能为 0／缺失」，2026-10-13 按用户要求删掉了。**
    2026-11-10 两个中位数更是**移出了交付表**。2026-11-19 只对**阅读中位数（日常）**
    恢复了一条更窄的过滤：为空/0 剔除，正数都收。

    `stats` 里累计剔除原因与样例，供调用方**如实报数**，而不是悄悄丢掉。
    """
    stats: dict = {"out_of_range": 0, "out_of_region": 0,
                   "no_read_median": 0, "no_inter_median": 0,
                   "over_budget": 0, "samples": [], "region_samples": [],
                   "read_median_samples": [], "inter_median_samples": []}

    def keep(item: dict) -> bool:
        fans = item.get("fansNum")
        if not in_range(fans):
            stats["out_of_range"] += 1
            return False

        if region and not region_matches(item.get("location"), region):
            stats["out_of_region"] += 1
            if len(stats["region_samples"]) < 5:
                stats["region_samples"].append(
                    f"{item.get('name')}({item.get('location') or '地域未知'})")
            return False

        # 阅读中位数（日常）= 平台 `clickMidNum`（详情页「日常笔记」实测一致）。
        # 用户口径：为空 / 0 的不要，正数都收。它**不进 Excel**，只做本地把关。
        read_mid = _to_int(item.get("clickMidNum"))
        if read_mid is None or read_mid <= 0:
            stats["no_read_median"] += 1
            if len(stats["read_median_samples"]) < 5:
                stats["read_median_samples"].append(
                    f"{item.get('name')}(clickMidNum={item.get('clickMidNum')!r})")
            return False

        # 互动中位数（日常）= 平台 `interMidNum`（合作口径是 `interMidCoop30`，别混）。
        inter_mid = _to_int(item.get("interMidNum"))
        if inter_mid is None or inter_mid <= 0:
            stats["no_inter_median"] += 1
            if len(stats["inter_median_samples"]) < 5:
                stats["inter_median_samples"].append(
                    f"{item.get('name')}(interMidNum={item.get('interMidNum')!r})")
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
    """按粉丝档位过滤**已映射的行**。

    ⚠️ **本函数不含预算把关**，只有粉丝档位。要完整把关请用 `build_gate()`
    —— 那才是唯一实现。留着它是因为「只看粉丝档位」在某些排查场景下有用。
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    for row in rows:
        flags = row.get(FLAGS_KEY) or {}
        (kept if in_range(flags.get(FANS_KEY)) else dropped).append(row)
    return kept, dropped
