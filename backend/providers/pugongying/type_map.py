"""同事给的「类型」词 → 平台可筛实体 的映射表。

**这是数据不是逻辑**（与 `fields.py` 同一个口径）：加一行、改一行都只改这个文件，
不动 `selection_service` 的代码。

---

## 一、为什么是手写的表，而不是"短文本概念映射 / 实体链接"

思路本身是对的（用户 2026-10-13 口述）：**词要沿着可解释的路径落到一个目标上，
没有合理路径就拒绝映射**，而不是拿字符串硬凑 —— 那正是「抽象 → 亲子」这类
无逻辑结果的来源。现在的实现里那条错路叫 `overlap`（≥2 个连续汉字就算像），
**已按本条删除**。

但**相似度/向量/分类器这条路在这里解决不了问题**，因为我们的目标不是
"我们自己定义的主类"，而是**平台真正能拿来筛的东西**。平台侧只有三种
（全部实测，见 `findings.md` F-34 / F-35 / F-38）：

| 通道 | 实测结论 |
|---|---|
| 行业树 | 只有**一级/二级能传参**；三级 179 个标签**没有参数位**（F-38） |
| `featureTags` | 达人自带的字符串数组，实测只有 **46 种**（F-34） |
| 笔记关键词 | 搜索框那个 free text，走同一个接口多一个 `keyword` 字段（F-35） |

**判定一个映射对不对，靠的是"平台把这个词放在哪"，而不是"两个词长得像不像"。**
实测的反例（F-38，这一条请务必读完再改这个文件）：

| 同事的词 | 平台里字面对得上的 | 平台的真实位置 | 语义 |
|---|---|---|---|
| 亲子达人 | `亲子家庭` | **汽车 / 用车场景 / 亲子家庭** | 开车带娃出行，**不是**亲子达人 |
| 体验 | `贵妇体验` | **母婴 / 母婴纸品 / 贵妇体验** | 纸品消费场景，**不是**"体验类内容" |

这两条的**字符串相似度都是高分，语义都是错的** —— 任何"算相似度 / 向量召回"
的方案都会把这两个错映射挑出来，而且它**无论如何算不出"亲子家庭挂在汽车下面"**：
这个事实只能靠人去看平台树。所以：

> **每一行都必须带 `evidence`。没有实测证据的行不许写进来。**

顺带一条：这次没匹配上反而是运气好 —— 真按「亲子达人 → 亲子家庭」筛，
交出去的就是"开车带娃的人"。

## 二、三种决定（`decision`）

| decision | 含义 | 落到的检索行为 |
|---|---|---|
| `map` | 有可解释的路径，平台**有**对应实体 | 用平台的行业参数筛（目标按 label 在**活的行业树**里解析成 ID） |
| `refuse` | 拒绝映射：字面对得上但语义错（`semantic_mismatch`），或平台里压根没有（`absent`） | 当**笔记关键词**搜，并把原因记账 |
| `pending` | 路径合理，但对应的平台通道**尚未接通/未实测** | 暂时当关键词，明细里写明"待接通" |

`refuse` **不等于把词丢掉**：丢掉的后果是"用户输入的字没起作用且毫无反馈"
（界面上本来就看不见这层判断）。当关键词搜至少还有作用，而且原因进了
`stats["classified_terms"]` 与运行日志，事后查得出来。

`map` 行的目标是 **label 不是 ID**：ID 会变，label 才是人看得懂、也改得动的东西；
解析不到（平台改版 / 快照过期）时**降级为 refuse** 并记原因，绝不硬塞一个旧 ID。
"""

from __future__ import annotations

# 关系类型 → 分数。**分数只用于展示与排序，不参与"猜"** ——
# 猜（模糊匹配）这条路已按上面的理由删掉了。
RELATION_SCORES: dict[str, float] = {
    "exact": 1.0,      # 与平台里的正式名字字面完全一致
    "alias": 0.95,     # 同义 / 别名
    "hypernym": 0.9,   # 下级概念上卷到平台的上位类目
    "related": 0.4,    # 强相关（**单靠它不足以映射**，只作为辅助信号）
    "none": 0.0,       # 无路径
}

# ── 映射表 ────────────────────────────────────────────────────────────────
#
# key 用**用户会输入的原词**（去空格、小写后比对；见 `lookup()`）。
# 每一行的 evidence 都必须指向 findings 里的一条实测；`checked_at` 是核对日期。
#
# 现状：这一批（同事 2026-10-12 给的 7 个词）**没有一个能用行业参数筛** ——
# 两个字面对得上但语义错，四个平台里压根没有，一个（生活vlog）通道还没接通。
# 这不是"表没写完"，是实测出来的结论（F-38）：同事那批词更像内容风格，
# 不是平台的场景类目。**将来同事给别的词，就往这里加行。**

TYPE_MAP: dict[str, dict] = {
    "体验": {
        "decision": "refuse",
        "refuse_kind": "semantic_mismatch",
        "relation": "exact",
        "platform_label": "贵妇体验",
        "platform_path": "母婴 / 母婴纸品 / 贵妇体验",
        "reason": "平台里只有「贵妇体验」，挂在母婴→母婴纸品下，说的是纸品消费场景，"
                  "不是「体验类内容」；而且它是三级标签，请求参数没有位置传它。",
        "evidence": "findings F-38（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
    "亲子达人": {
        "decision": "refuse",
        "refuse_kind": "semantic_mismatch",
        "relation": "exact",
        "platform_label": "亲子家庭",
        "platform_path": "汽车 / 用车场景 / 亲子家庭",
        "reason": "平台里只有「亲子家庭」，挂在汽车→用车场景下，意思是「开车带娃出行」，"
                  "不是「亲子达人」。字面对得上、语义不相干 —— 真按它筛会交出"
                  "「开车带娃的人」。三级标签同样没有参数位。",
        "evidence": "findings F-38（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
    "抽象": {
        "decision": "refuse",
        "refuse_kind": "absent",
        "relation": "none",
        "platform_label": "",
        "platform_path": "",
        "reason": "平台三级行业树与 46 个 featureTags 里都不存在。",
        "evidence": "findings F-34 / F-38（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
    "戏精": {
        "decision": "refuse",
        "refuse_kind": "absent",
        "relation": "none",
        "platform_label": "",
        "platform_path": "",
        "reason": "平台里只出现在导航菜单文案里，不是行业也不是 featureTags 标签"
                  "（实测 featureTags=[\"戏精\"] 的 total 是 0）。",
        "evidence": "findings F-34（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
    "cos": {
        "decision": "refuse",
        "refuse_kind": "absent",
        "relation": "none",
        "platform_label": "",
        "platform_path": "",
        "reason": "平台三级行业树与 46 个 featureTags 里都不存在。",
        "evidence": "findings F-34 / F-38（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
    "活人感": {
        "decision": "refuse",
        "refuse_kind": "absent",
        "relation": "none",
        "platform_label": "",
        "platform_path": "",
        "reason": "平台三级行业树与 46 个 featureTags 里都不存在。",
        "evidence": "findings F-34 / F-38（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
    "生活vlog": {
        "decision": "pending",
        "relation": "alias",
        "platform_label": "vlog",
        "platform_channel": "feature_tag",
        "platform_path": "featureTags（不是行业树）",
        "reason": "平台**确实有** `vlog` 这个标签（实测出现 290 次），但我们的请求体"
                  "一直在传空的 featureTags —— 这条通道**还没接通**，"
                  "而且多标签是否真的 OR 未实测。接通前不敢让它改变检索结果。",
        "evidence": "findings F-34（2026-10-12 实测：传 [\"vlog\"] 服务端确实筛）",
        "checked_at": "2026-10-12",
    },
    # 平台标签写的是 `vlog`（没有「生活」二字），另一种常见写法一并记上。
    "vlog": {
        "decision": "pending",
        "relation": "exact",
        "platform_label": "vlog",
        "platform_channel": "feature_tag",
        "platform_path": "featureTags（不是行业树）",
        "reason": "同「生活vlog」：平台有这个标签，但 featureTags 通道尚未接入检索参数。",
        "evidence": "findings F-34（2026-10-12 实测）",
        "checked_at": "2026-10-12",
    },
}


def normalize(term: str) -> str:
    """比对用的归一化：去空格、小写。繁简与全半角**暂不处理**（没实测需求）。"""
    return str(term or "").replace(" ", "").lower()


def lookup(term: str) -> dict | None:
    """查这张表；查不到返回 None（= 没有合理路径，由调用方拒绝映射）。"""
    key = normalize(term)
    for raw, row in TYPE_MAP.items():
        if normalize(raw) == key:
            return {"term": raw, **row}
    return None


def relation_score(relation: str) -> float:
    return RELATION_SCORES.get(relation, 0.0)
