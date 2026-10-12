"""关键词检索（findings F-35）。

链路的由来：平台检索页那个搜索框（placeholder「按笔记关键词找博主」）打的
**就是我们一直在用的同一个接口**，只是多带一个 `keyword` 字段。
一个请求只能带一个关键词 ⇒ 多关键词必须逐个搜、再合并去重。

**用户的两条硬约束**（2026-10-12）：
1. 命中**任意一个**即算（不是必须全命中）；
2. 命中**多个不会提高优先级** —— 先后仍然只看粉丝量和报价。
   第 2 条在实现上落成：同一个人被多个关键词命中时**只收一次、不重复占名额**。
"""

from __future__ import annotations

from backend.providers.pugongying import search as search_mod
from backend.providers.pugongying.search import SearchParams
from backend.services import selection_service


# ── 请求体 ──────────────────────────────────────────────────────────────────

def test_body_carries_the_keyword_only_when_present():
    """不带关键词的请求体必须与实测样本**逐字节一致** —— 不能凭空多一个空串。

    平台自己的普通检索也不发 `keyword`；多发一个空串未必等价，没实测过就不冒险。
    """
    assert "keyword" not in SearchParams().to_body()
    assert SearchParams(keyword="戏精").to_body()["keyword"] == "戏精"


# ── 跨关键词去重 ────────────────────────────────────────────────────────────

def _fake_pages(monkeypatch, pages: dict[int, list[str]]):
    """按 pageNum 返回给定 userId 列表的假 search_page。"""
    def fake(page, params, *, page_num, page_size):
        users = pages.get(page_num, [])
        return {"data": {"kols": [{"userId": u, "name": u, "fansNum": 150_000,
                                   "accumCoopImpMedinNum30d": 1000, "interMidCoop30": 10}
                                  for u in users],
                         "total": 5000}}
    monkeypatch.setattr(search_mod, "search_page", fake)


def test_same_person_is_not_collected_twice_across_keywords(monkeypatch):
    """一个人被两个关键词都命中时，只收一次、也只占一个名额。"""
    _fake_pages(monkeypatch, {1: ["u1", "u2"], 2: ["u3", "u4"]})
    counts: dict = {}
    seen: set = set()
    common = dict(per_tier=10, key_of=lambda item: "X", tier_keys=["X"],
                  page_size=2, max_pages=2, throttle=None)

    first, _ = search_mod.search_all(None, SearchParams(keyword="戏精"),
                                     counts=counts, seen=seen, **common)
    assert [i["userId"] for i in first] == ["u1", "u2", "u3", "u4"]
    assert counts["X"] == 4

    # 第二个关键词返回的是同一批人 —— 一个都不该再收
    second, _ = search_mod.search_all(None, SearchParams(keyword="活人感"),
                                      counts=counts, seen=seen, **common)
    assert second == [], "重复的人不该被第二个关键词再收一遍（那等于变相提高优先级）"
    assert counts["X"] == 4, "名额也不能重复计数"


def test_without_a_shared_seen_set_the_same_person_would_repeat(monkeypatch):
    """反证：不传共享集合时确实会重复 —— 说明那两个参数是**必须**的，不是装饰。"""
    _fake_pages(monkeypatch, {1: ["u1", "u2"], 2: ["u3", "u4"]})
    common = dict(per_tier=10, key_of=lambda item: "X", tier_keys=["X"],
                  page_size=2, max_pages=2, throttle=None)

    first, _ = search_mod.search_all(None, SearchParams(keyword="A"), **common)
    second, _ = search_mod.search_all(None, SearchParams(keyword="B"), **common)
    assert len(first) == len(second) == 4


# ── 页数预算 ────────────────────────────────────────────────────────────────

def test_page_budget_is_split_evenly_between_keywords(monkeypatch):
    """预算按关键词均分，否则第一个关键词把预算吃光、后面的轮不到。"""
    calls: list[tuple[str, int]] = []

    def fake_search_all(page, params, **kwargs):
        calls.append((params.keyword, kwargs["max_pages"]))
        return [], {"pages": kwargs["max_pages"], "total": 0, "per_tier_counts": {},
                    "reached_end": False}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=5, keywords=["a", "b", "c", "d"])

    assert [kw for kw, _ in calls] == ["a", "b", "c", "d"]
    assert {n for _, n in calls} == {selection_service.PAGES_PER_RUN // 4}
    assert sum(n for _, n in calls) <= selection_service.PAGES_PER_RUN


def test_no_keywords_means_one_pass_without_the_field(monkeypatch):
    calls: list[str] = []

    def fake_search_all(page, params, **kwargs):
        calls.append(params.keyword)
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": False}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=5, keywords=[])

    assert calls == [""], "没有关键词就是一次普通检索，不带 keyword"
