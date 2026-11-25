"""关键词检索（findings）。

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


def test_body_carries_the_fans_range_only_when_set():
    """粉丝区间同理：**不限时不带这两个键**（平台界面自己不设区间时也不发）。

    形状是驱动界面抓包得到的（findings）：`fansNumberLower` / `fansNumberUpper`
    两个**平铺**的整数键。别照界面保存的 `fansCount` 去构造 —— 那四种写法实测被忽略。
    """
    assert "fansNumberLower" not in SearchParams().to_body()
    assert "fansNumberUpper" not in SearchParams().to_body()

    body = SearchParams(fans_lower=400_000, fans_upper=500_000).to_body()
    assert body["fansNumberLower"] == 400_000
    assert body["fansNumberUpper"] == 500_000


def test_filling_the_quota_is_not_the_same_as_running_out(monkeypatch):
    """**凑满 20 ≠ 平台到底了** —— 这一条把「换一批」的置灰逻辑钉死。

    第一版把"凑满"也算成到头，于是界面会显示"平台这边已经翻到底了"并把
    「换一批」置灰 —— 而实际上 40w-50w 那一档有 2150 人，我们才翻了第 1 页。
    用户想再换一批人就没路了。
    """
    def fake_search_all(page, params, **kwargs):
        tier = kwargs["tier_keys"][0]
        kwargs["counts"][tier] = kwargs["per_tier"]     # 一页就凑满
        return [], {"pages": 1, "total": 2150, "per_tier_counts": {},
                    "next_page": 2,                     # 这一档下次从第 2 页接着翻
                    "reached_end": False}               # 平台没到底

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    _, stats = selection_service.search(SearchParams(), per_tier=20, keywords=["a"])

    assert stats["per_tier_counts"] == {k: 20 for k in stats["per_tier_counts"]}
    assert stats["reached_end"] is False, "只是我们停手了，不是平台没人了"
    assert all(page == 2 for page in stats["next_pages"].values()), \
        "每一档都该从下一页接着换一批"


def test_a_full_tier_stops_being_queried(monkeypatch):
    """某一档凑满 20 就**不再问它** —— 否则白花请求数（也白等节流）。"""
    asked: list[str] = []

    def fake_search_all(page, params, **kwargs):
        tier = kwargs["tier_keys"][0]
        asked.append(tier)
        # 假装这一档一次就给够了
        kwargs["counts"][tier] = kwargs["per_tier"]
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": True}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    _, stats = selection_service.search(SearchParams(), per_tier=20, keywords=["a", "b"])

    assert len(asked) == 6, "每档只问一次就满了"
    assert stats["per_tier_counts"] == {key: 20 for key in stats["per_tier_counts"]}
    assert stats["reached_end"] is True


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

def test_each_keyword_gets_its_own_page_allowance(monkeypatch):
    """**每个「档位 × 关键词」各有各的额度**，不是把总预算按个数切。

    曾经是"60 页总预算 ÷ 关键词个数"（7 个词 → 每个 8 页）—— 实测那 8 页把 6 个
    关键词全截断了，高量级因此凑不满，界面还错报成"平台到底了"。
    """
    calls: list[tuple[str, int]] = []

    def fake_search_all(page, params, **kwargs):
        calls.append((params.keyword, kwargs["max_pages"]))
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": False}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=5, keywords=["a", "b"])

    # 档位在外、关键词在内：没凑满就一直问到最后一个词；全跑完还不够就再兜底一趟
    # （兜底那趟不带 keyword，见 test_a_tier_that_the_keywords_cannot_fill_falls_back…）
    assert [kw for kw, _ in calls] == ["a", "b", ""] * 6
    assert {n for _, n in calls} == {selection_service.PAGES_PER_KEYWORD}
    # 总额度只是防失控的安全阀，不该把每个组合的额度压小
    assert selection_service.PAGES_PER_RUN >= selection_service.PAGES_PER_KEYWORD


def test_a_tier_that_the_keywords_cannot_fill_falls_back_without_the_keyword(monkeypatch):
    """检索词在某档里凑不满 ⇒ **不带检索词**再翻这一档（用户 2026-10-13 拍板）。

    实测背景：40w-50w 挂上检索词后整档只剩几十人（'体验' 15、'活人感' 0、
    '亲子达人  抽象' 34），怎么翻都到不了 30；而**不带检索词**时这一档有 2150 人。
    所以口径改成：**每个量级必凑满**优先于"只收命中检索词的人"。

    ⚠️ 代价是兜底收进来的人**不保证命中检索词** —— 所以必须留痕：
    哪几档补了、补了几条，进 `stats["keyword_fallback"]`。
    """
    calls: list[tuple[str, str]] = []

    def fake_search_all(page, params, **kwargs):
        tier = kwargs["tier_keys"][0]
        calls.append((tier, params.keyword))
        # 带检索词那趟一条不给；不带检索词那趟把这一档填满
        kwargs["counts"][tier] = kwargs["per_tier"] if params.keyword == "" else 0
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": True}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    _, stats = selection_service.search(SearchParams(), per_tier=20, keywords=["a"])

    head = "超头部KOL(40w-50w)"
    assert (head, "a") in calls, "先按检索词问"
    assert (head, "") in calls, "凑不满就必须再问一次，且**不带 keyword**"
    assert stats["per_tier_counts"] == {key: 20 for key in stats["per_tier_counts"]}, \
        "兜底之后每一档都该凑满"
    assert stats["keyword_fallback"] == {
        key: 20 for key in stats["per_tier_counts"]}, "补了几条必须记在账上"


def test_the_fallback_is_skipped_when_there_were_no_keywords_at_all(monkeypatch):
    """**没给检索词时不兜底** —— 那一趟本身就是"不带 keyword 的普通检索"。

    否则同一件事会对同一档问两遍（白花请求数、也白等节流）。
    """
    calls: list[str] = []

    def fake_search_all(page, params, **kwargs):
        calls.append(params.keyword)
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": True}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    _, stats = selection_service.search(SearchParams(), per_tier=5, keywords=[])

    assert calls == [""] * 6
    assert stats["keyword_fallback"] == {}


def test_each_tier_query_carries_its_own_fans_range(monkeypatch):
    """**每个档位的查询都带上它自己的粉丝区间**（findings）。

    这是"每档必 20"的全部依据：不带区间时平台按综合排序返回各种粉丝量的人，
    高量级在里面稀疏，本地再剔 ⇒ 超头部永远凑不满（实测只有 3 条）。
    """
    seen: list[tuple[str, int, int]] = []

    def fake_search_all(page, params, **kwargs):
        tier = kwargs["tier_keys"][0]
        seen.append((tier, params.fans_lower, params.fans_upper))
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": False}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=5, keywords=["a"])

    expected = [
        ("超头部KOL(40w-50w)", 400_000, 500_000),
        ("超头部KOL(30w-40w)", 300_000, 400_000),
        ("头部KOL(20w-30w)", 200_000, 300_000),
        ("头部KOL(10w-20w)", 100_000, 200_000),
        ("腰部KOL(5w-10w)", 50_000, 100_000),
        ("尾部KOL(1w-5w)", 10_000, 50_000),
    ]
    # 每档问两趟（检索词一趟 + 兜底一趟），**两趟都必须带对区间** ——
    # 兜底那趟尤其不能漏：它正是在"这一档"里翻，区间错了就是另一个结果集。
    assert seen[0::2] == expected, "档位与粉丝区间必须一一对上（对错了就是交出一份错名单）"
    assert seen[1::2] == expected, "兜底那一趟也得带自己的区间"


def test_reached_end_is_not_claimed_when_a_keyword_was_cut_by_the_limit(monkeypatch):
    """被页数上限截断 ≠ 平台到底了 —— 这个误报实测把界面骗过。"""
    def fake_search_all(page, params, **kwargs):
        # 每个组合都恰好翻满上限、且平台报的是"没到底"
        return [], {"pages": kwargs["max_pages"], "total": 0, "per_tier_counts": {},
                    "reached_end": False}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    _, stats = selection_service.search(SearchParams(), per_tier=5, keywords=["a", "b"])

    assert stats["reached_end"] is False, "被上限截断时不能报「平台到底了」"
    assert stats["stopped_by_page_limit"] is True
    assert stats["keywords_cut_by_limit"] >= 1
    # 真实开销也一并钉住：每个组合都翻满 30 页 ⇒ 整轮预算（300 页）在这里正好耗尽，
    # 后面的档位**不再开新的查询**（安全阀生效，不是无限翻）。
    assert stats["pages"] == selection_service.PAGES_PER_RUN


def test_reached_end_is_claimed_only_when_every_keyword_exhausted(monkeypatch):
    def fake_search_all(page, params, **kwargs):
        return [], {"pages": 2, "total": 0, "per_tier_counts": {}, "reached_end": True}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    _, stats = selection_service.search(SearchParams(), per_tier=5, keywords=["a", "b"])

    assert stats["reached_end"] is True
    assert stats["stopped_by_page_limit"] is False


def test_no_keywords_means_one_pass_per_tier_without_the_field(monkeypatch):
    calls: list[str] = []

    def fake_search_all(page, params, **kwargs):
        calls.append(params.keyword)
        return [], {"pages": 1, "total": 0, "per_tier_counts": {}, "reached_end": True}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=5, keywords=[])

    assert calls == [""] * 6, "没有关键词就是每个档位各一次普通检索，都不带 keyword"
