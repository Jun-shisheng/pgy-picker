"""检索词分派：把随手输入的一串词拆成「行业」与「笔记关键词」。

**两条来自用户的要求**（2026-10-12）：
1. 「可以是这样但这些逻辑都是隐性的不要暴露出来我们自己程序有判断」——
   界面不弹确认、不摆分类结果；
2. 「命中多个不提高优先级，优先级还是粉丝数和报价」—— 去重与排序都不受它影响。

判断明细仍然要返回并记账：猜错了得能事后查出来（项目设计约束 第九条）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.providers.pugongying.search import SearchParams
from backend.services import selection_service


def _stub_classify(monkeypatch, items: list[dict]):
    monkeypatch.setattr(selection_service, "classify_terms", lambda terms: (items, "cache"))


def _industry(term: str, label: str, first: str, second: str = "") -> dict:
    return {"term": term, "kind": "industry",
            "match": "overlap",
            "matched": {"label": label, "value": second or first,
                        "first": first, "second": second},
            "candidates": []}


def _keyword(term: str) -> dict:
    return {"term": term, "kind": "keyword", "match": "none",
            "matched": None, "candidates": []}


def test_industry_term_becomes_the_industry_filter(monkeypatch):
    _stub_classify(monkeypatch, [_industry("亲子达人", "亲子家庭", "F1", "S1"),
                                 _keyword("戏精")])

    params, keywords, classified = selection_service.apply_terms(SearchParams(), ["亲子达人", "戏精"])

    assert (params.first_industry, params.second_industry) == ("F1", "S1")
    assert keywords == ["戏精"], "被识别成行业的词不该再当关键词搜一遍"
    assert len(classified) == 2


def test_explicit_dropdown_wins_over_the_guess(monkeypatch):
    """下拉里人选过行业就听人的 —— 程序猜的不该覆盖人明确选的。"""
    _stub_classify(monkeypatch, [_industry("亲子达人", "亲子家庭", "F1", "S1")])

    given = SearchParams(first_industry="人选的一级")
    params, keywords, _ = selection_service.apply_terms(given, ["亲子达人"])

    assert params.first_industry == "人选的一级"
    assert keywords == ["亲子达人"], "人已经选过行业了，这个词就只当关键词"


def test_only_the_first_industry_is_used_and_the_rest_degrade_to_keywords(monkeypatch):
    """一次只能按一个行业筛；多出来的**降级成关键词**，而不是丢掉。

    为什么降级而不是丢：丢掉等于**用户输入的字没起作用**，而且没有任何反馈
    （判断是隐性的，界面上看不到）。降级至少让这个词还起作用 ——
    "在笔记里提到母婴"也是个合理理解。这条会记进日志（用户要的「开发检查」）。
    """
    _stub_classify(monkeypatch, [_industry("亲子达人", "亲子家庭", "F1"),
                                 _industry("母婴", "母婴", "F2"),
                                 _keyword("戏精")])

    params, keywords, _ = selection_service.apply_terms(SearchParams(), ["a", "b", "c"])

    assert params.first_industry == "F1"
    assert keywords == ["戏精", "母婴"]


def test_failed_classification_falls_back_to_keywords(monkeypatch):
    """行业清单拿不到（没登录/没缓存）不该让整轮检索失败。"""
    def boom(_terms):
        raise selection_service.NotReady("没有可用的登录态")
    monkeypatch.setattr(selection_service, "classify_terms", boom)

    params, keywords, classified = selection_service.apply_terms(SearchParams(), ["戏精", "抽象"])

    assert keywords == ["戏精", "抽象"]
    assert all(item["match"] == "failed" for item in classified)
    assert params.first_industry == ""


def test_no_terms_is_a_no_op():
    params = SearchParams(first_industry="X")
    out_params, keywords, classified = selection_service.apply_terms(params, [])

    assert out_params is params and keywords == [] and classified == []


def test_apply_terms_never_leaves_a_keyword_on_the_params():
    """`params.keyword` 由关键词循环单独设置，分派时必须是空的 ——
    否则"第一个关键词"会被搜两遍。"""
    params, _, _ = selection_service.apply_terms(SearchParams(), [])
    assert params.keyword == ""


@pytest.mark.parametrize("term", ["戏精", "抽象", "活人感", "cos"])
def test_a_word_matching_no_industry_is_a_keyword(monkeypatch, term):
    _stub_classify(monkeypatch, [_keyword(term)])
    _, keywords, _ = selection_service.apply_terms(SearchParams(), [term])
    assert keywords == [term]
