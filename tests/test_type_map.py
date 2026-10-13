"""检索词 → 平台实体 的映射：**有路径就落上去，没路径就拒绝**，不许硬凑。

用户 2026-10-13 的口径：不要把它当字符串匹配，要当"短文本概念映射"——
沿着讲得通的路径落到一个目标上；没有合理路径就拒绝映射，
而不是像原来那样"有 2 个连续汉字相同就算像"（那条已删，见 `type_map.py` 顶部）。

这里钉住的是**几条实测事实**（F-34 / F-38），它们是这个文件存在的全部理由：
- 「亲子达人」在平台里字面对得上的只有「亲子家庭」，而它挂在**汽车**下；
- 「体验」在平台里只有「贵妇体验」，挂在**母婴纸品**下；
- 抽象 / 戏精 / cos / 活人感：平台里**压根没有**。
"""

from __future__ import annotations

import pytest

from backend.providers.pugongying import type_map
from backend.services import selection_service

_LIVE = [
    {"value": "V_MM", "label": "母婴",
     "children": [{"value": "V_MM_ZP", "label": "母婴纸品"}]},
    {"value": "V_QC", "label": "汽车",
     "children": [{"value": "V_QC_YC", "label": "用车场景"}]},
]


def _one(term: str) -> dict:
    """拿一个词走完整的分类路径（行业清单用上面那份固定的）。"""
    items, _source = selection_service.classify_terms([term], industries=_LIVE)
    assert len(items) == 1
    return items[0]


def test_a_platform_label_maps_on_the_spot():
    """「母婴」就是平台自己的名字 —— 最强的一条路径，不需要任何外部知识。"""
    item = _one("母婴")
    assert item["kind"] == "industry"
    assert item["match"] == "exact"
    assert item["matched"]["value"] == "V_MM"


def test_the_word_the_colleague_gave_is_refused_not_forced_onto_a_lookalike():
    """「亲子达人」字面上对得上「亲子家庭」，但那玩意挂在**汽车**下面。

    这是整个文件存在的理由：**字面相似度是高分，语义是错的。**
    真按它筛，交出去的是"开车带娃出行的人" —— 所以宁可拒绝映射。
    """
    item = _one("亲子达人")

    assert item["kind"] == "keyword", "拒绝映射 ⇒ 降级为关键词，而不是当成行业"
    assert item["match"] == "refused"
    assert item["matched"] is None
    assert "汽车" in item["reason"]
    assert "F-38" in item["evidence"]


def test_experience_is_refused_too():
    item = _one("体验")
    assert item["match"] == "refused"
    assert "母婴纸品" in item["reason"]


@pytest.mark.parametrize("term", ["抽象", "戏精", "cos", "活人感"])
def test_words_the_platform_does_not_have_are_refused(term):
    item = _one(term)
    assert item["kind"] == "keyword"
    assert item["match"] == "refused"
    assert item["score"] == 0.0


def test_vlog_has_a_real_platform_channel_but_it_is_not_wired_yet():
    """平台的 featureTags 里**确实有** `vlog` —— 但那条通道还没接进检索参数，
    所以这里是 `pending`（暂按关键词），而不是假装已经能用。"""
    item = _one("生活vlog")
    assert item["match"] == "pending"
    assert item["platform_label"] == "vlog"
    assert "featureTags" in item["platform_path"]


def test_a_word_nobody_has_looked_at_is_a_plain_keyword():
    item = _one("摸鱼神器")
    assert item["match"] == "no_path"
    assert item["score"] == 0.0


def test_a_map_row_resolves_through_the_live_tree(monkeypatch):
    """`map` 行只写 **label**，ID 到活的行业树里现解析 —— 表里不存会过期的 ID。"""
    monkeypatch.setitem(type_map.TYPE_MAP, "母婴用品", {
        "decision": "map", "relation": "hypernym", "platform_label": "母婴",
        "reason": "测试用", "evidence": "测试用",
    })

    item = _one("母婴用品")
    assert item["kind"] == "industry"
    assert item["match"] == "map"
    assert item["matched"]["value"] == "V_MM"
    assert item["score"] == type_map.relation_score("hypernym")


def test_a_map_row_whose_target_is_gone_degrades_instead_of_using_a_stale_id(monkeypatch):
    """平台改版 / 快照过期 ⇒ 目标解析不出来，**降级为关键词**，绝不硬塞旧 ID。"""
    monkeypatch.setitem(type_map.TYPE_MAP, "母婴用品", {
        "decision": "map", "relation": "hypernym", "platform_label": "已经不存在的类目",
        "reason": "测试用", "evidence": "测试用",
    })

    item = _one("母婴用品")
    assert item["kind"] == "keyword"
    assert item["match"] == "refused"
    assert "已经不存在的类目" in item["reason"]


def test_no_industry_list_still_keeps_the_table_verdicts(monkeypatch):
    """行业清单取不到（没登录 / 没快照）不该让整轮检索垮掉，也不该丢掉映射表的判定。

    映射表是静态数据，它的结论与"能不能取到行业树"无关 —— "取不到就一律当关键词"
    那种兜底会把「亲子达人已被拒绝映射」这条实测结论一起扔掉。
    """
    def boom():
        raise selection_service.NotReady("没有可用的登录态")
    monkeypatch.setattr(selection_service, "industries_cached", boom)

    items, source = selection_service.classify_terms(["亲子达人", "母婴"])

    assert source == "unavailable"
    assert items[0]["match"] == "refused" and "汽车" in items[0]["reason"]
    # 取不到行业树 ⇒ 判断不了「母婴」是不是平台正式名，只能退化成关键词
    assert items[1]["kind"] == "keyword"


def test_the_old_fuzzy_matcher_is_gone():
    """`overlap`（≥2 个连续汉字）**必须**已经删掉 —— 它就是无逻辑映射的来源。

    钉死它：只要还有人把"最长公共子串"捡回来，这个用例就会红。
    """
    assert not hasattr(selection_service, "_common_chars")
    assert all(row["decision"] in {"map", "refuse", "pending"}
               for row in type_map.TYPE_MAP.values())
    assert type_map.relation_score("none") == 0.0
