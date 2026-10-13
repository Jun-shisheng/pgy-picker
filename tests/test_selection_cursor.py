"""「换一批」：接着上次翻到的位置往下取。

**这个功能的由来**：平台侧排序是写死的（`comprehensiverank` 降序），而检索总是
**从第 1 页开始、每个量级凑够就停** —— 于是同一组条件每次都给同一批人，
`total=5000` 的池子里永远只看得到最靠前的那几十个。

这里钉三件事：游标按筛选条件分开记、`start_page` 真的生效、到头了要如实说。
全部不碰浏览器（`search_page` 被替换掉）。
"""

from __future__ import annotations

from pathlib import Path

from backend.providers.pugongying import search as search_mod
from backend.providers.pugongying.search import SearchParams
from backend.services import selection_cursor, selection_service


# ── 游标本身 ────────────────────────────────────────────────────────────────

def test_cursor_defaults_to_first_page():
    assert selection_cursor.load("", "") == {}
    assert selection_cursor.load("某行业", "某子类") == {}


def test_cursor_round_trips():
    selection_cursor.save("母婴", "婴童洗护", {"超头部KOL(40w-50w)": 7, "尾部KOL(1w-5w)": 2})
    assert selection_cursor.load("母婴", "婴童洗护") == {"超头部KOL(40w-50w)": 7,
                                                        "尾部KOL(1w-5w)": 2}


def test_cursor_is_kept_per_tier_and_keyword():
    """**每一格（档位 × 关键词）各记各的页码**（2026-10-13 起）。

    档位是一个独立的粉丝区间查询；同一档里不同关键词又是不同的结果集；
    兜底那趟（不带检索词）更是另一个池子 —— 三者的页码**都不能互相挪用**。
    键由 `selection_service.combo_key()` 生成（游标文件本身不关心键长什么样）。
    """
    combo_a = selection_service.combo_key("超头部KOL(40w-50w)", "体验")
    combo_b = selection_service.combo_key("超头部KOL(40w-50w)", "抽象")
    combo_fallback = selection_service.combo_key("超头部KOL(40w-50w)", "")

    assert len({combo_a, combo_b, combo_fallback}) == 3, \
        "同一档里：关键词之间、关键词与兜底之间，键必须互不相同"
    assert combo_fallback.endswith("（不限）")

    selection_cursor.save("母婴", "", {combo_a: 12, combo_b: 3})
    loaded = selection_cursor.load("母婴", "")
    assert loaded[combo_a] == 12
    assert loaded[combo_b] == 3
    assert combo_fallback not in loaded, "没跑过的格子不该凭空冒出一个页码"


def test_cursor_is_kept_per_filter():
    """换了行业就是另一个结果集，页码对不上 —— 必须各记各的。"""
    selection_cursor.save("母婴", "", {"超头部KOL(40w-50w)": 5})
    assert selection_cursor.load("美妆", "") == {}, "别的行业不该继承这个游标"
    assert selection_cursor.load("母婴", "") == {"超头部KOL(40w-50w)": 5}


def test_a_legacy_cursor_is_still_readable_even_though_it_no_longer_matches(isolated_storage):
    """旧版游标（一个整数 / 一个档位一个页码）读得出来，只是**键对不上了**。

    新键是「档位 × 关键词」组合（`combo_key()`），旧键（`"*"` 或档位名）自然不匹配
    ⇒ 那几格从第 1 页开始。这样**不会出错**（不过是少续了一次），
    而且现在 `next` 是默认模式、续到空还会自动回落第 1 页，所以升级不会让人卡住。
    """
    selection_cursor.CURSOR_FILE.write_text(
        '{"version": 1, "pages": {"*|*|*": 31}}', encoding="utf-8")

    loaded = selection_cursor.load("", "")
    assert loaded == {"*": 31}, "文件仍然读得出来（不因为格式旧就报错）"
    assert selection_service.combo_key("超头部KOL(40w-50w)", "体验") not in loaded, \
        "但组合键匹配不上 ⇒ 调用方会从第 1 页开始"


def test_cursor_survives_a_corrupt_file(isolated_storage):
    """游标文件坏了只是"从头来"，不该让检索失败。"""
    selection_cursor.CURSOR_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
    assert selection_cursor.load("", "") == {}


# ── search_all 的 start_page ────────────────────────────────────────────────

def _fake_search_page(pages_sizes: dict[int, int], seen: list[int]):
    """返回一个假的 search_page：按 page_num 决定这一页给多少条。"""
    def fake(page, params, *, page_num, page_size):
        seen.append(page_num)
        count = pages_sizes.get(page_num, page_size)
        return {"data": {"kols": [{"id": page_num * 1000 + i} for i in range(count)],
                         "total": 5000}}
    return fake


def _run_all(monkeypatch, *, start_page, max_pages, per_tier, sizes=None, seen=None):
    monkeypatch.setattr(search_mod, "search_page",
                        _fake_search_page(sizes or {}, seen if seen is not None else []))
    return search_mod.search_all(
        None, SearchParams(), per_tier=per_tier, key_of=lambda item: "X",
        tier_keys=["X"], keep=None, page_size=20, max_pages=max_pages,
        start_page=start_page, throttle=None)


def test_starts_from_the_requested_page(monkeypatch):
    seen: list[int] = []
    _, stats = _run_all(monkeypatch, start_page=7, max_pages=3, per_tier=5, seen=seen)

    assert seen == [7], "只该取这一页：per_tier=5 而一页有 20 条，凑够就停"
    assert stats["start_page"] == 7
    assert stats["pages"] == 1
    assert stats["next_page"] == 8, "下次该从第 8 页接着翻"


def test_max_pages_counts_from_start_page(monkeypatch):
    """上限是"本次翻几页"，不是绝对页码 —— 否则接着翻时一页都取不到。"""
    seen: list[int] = []
    _, stats = _run_all(monkeypatch, start_page=7, max_pages=3, per_tier=10_000, seen=seen)

    assert seen == [7, 8, 9]
    assert stats["next_page"] == 10


def test_reports_when_the_platform_runs_out(monkeypatch):
    """平台给的不足一页 ＝ 到头了，界面据此把「换一批」置灰。"""
    _, stats = _run_all(monkeypatch, start_page=3, max_pages=10, per_tier=10_000,
                        sizes={3: 4})

    assert stats["reached_end"] is True


def test_not_reached_end_when_a_full_page_came_back(monkeypatch):
    _, stats = _run_all(monkeypatch, start_page=1, max_pages=2, per_tier=10_000)

    assert stats["reached_end"] is False
    assert stats["next_page"] == 3


# ── search_batch：把游标接上 ────────────────────────────────────────────────

def test_next_mode_uses_the_saved_cursor(monkeypatch):
    selection_cursor.save("母婴", "", {"超头部KOL(40w-50w)": 9})
    seen: dict = {}

    def fake_search(params, *, per_tier, start_pages, keywords=None):
        seen["start_pages"] = start_pages
        return [], {"next_pages": {"超头部KOL(40w-50w)": 11}, "per_tier_counts": {}}

    monkeypatch.setattr(selection_service, "search", fake_search)

    selection_service.search_batch(SearchParams(first_industry="母婴"), mode="next")

    assert seen["start_pages"] == {"超头部KOL(40w-50w)": 9}
    assert selection_cursor.load("母婴", "") == {"超头部KOL(40w-50w)": 11}, \
        "游标该被推进到下一页"


def test_fresh_mode_ignores_the_cursor(monkeypatch):
    selection_cursor.save("母婴", "", {"超头部KOL(40w-50w)": 9})
    seen: dict = {}

    def fake_search(params, *, per_tier, start_pages, keywords=None):
        seen["start_pages"] = start_pages
        return [], {"next_pages": {"超头部KOL(40w-50w)": 2}, "per_tier_counts": {}}

    monkeypatch.setattr(selection_service, "search", fake_search)

    selection_service.search_batch(SearchParams(first_industry="母婴"), mode="fresh")

    assert seen["start_pages"] == {}, "从头取就不该带上任何游标"


def test_resuming_past_the_end_falls_back_to_page_one(monkeypatch):
    """接着上次的位置往下问、平台一个人都不给 ⇒ **自动回到第 1 页**。

    不这么做的话：用户点一次「开始检索」会拿到一份**空名单**，而他并不知道那是
    "续到底"造成的 —— 平台排序写死，回头要靠的就是第 1 页那批人。
    """
    from backend.providers.pugongying import pricing

    monkeypatch.setattr(pricing, "load", lambda force=False: None)   # 不依赖本机报价配置
    asked: list[int] = []

    def fake_search_page(page, params, *, page_num, page_size):
        asked.append(page_num)
        if page_num > 3:                     # 这个池子总共只有 3 页
            return {"data": {"kols": [], "total": 60}}
        prefix = f"{params.fans_lower}-{params.keyword}"
        return {"data": {"kols": [{"userId": f"{prefix}-{page_num}-{i}",
                                   "fansNum": 420_000} for i in range(page_size)],
                         "total": 60}}

    monkeypatch.setattr(search_mod, "search_page", fake_search_page)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    combo = selection_service.combo_key("超头部KOL(40w-50w)", "体验")
    _, stats = selection_service.search(SearchParams(), per_tier=10,
                                        keywords=["体验"], start_pages={combo: 7})

    assert asked[0] == 7, "先按游标的位置问"
    assert 1 in asked, "续到空之后必须回到第 1 页，而不是交一份空名单"
    assert stats["per_tier_counts"]["超头部KOL(40w-50w)"] == 10
    assert stats["start_pages"][combo] == 7, "报出去的仍是这一格本来的起点"
    assert stats["next_pages"][combo] == 2, "回落之后游标也跟着回到第 2 页"


# ── 单次翻页上限 ────────────────────────────────────────────────────────────

def test_one_run_carries_the_page_safety_cap(monkeypatch):
    """目标是「每档凑够 20」，页数额度只是**安全上限**。

    别把它调小：调小就等于"凑不完也不翻了"，直接违背用户要的「每个量级必定要有 20 条」。
    （历史上它一度是 15，那是"单次只等 50 秒"的方案 —— 与"必 20"冲突，已废止；
    后来又变成"总预算 60 按关键词均分"，实测把 6 个关键词全截断了，同样废止。）
    """
    captured: dict = {}

    def fake_search_all(page, params, **kwargs):
        captured.update(kwargs)
        return [], {"pages": 0, "page_limit": kwargs["max_pages"],
                    "start_page": 1, "next_page": 1, "reached_end": True,
                    "total": 0, "per_tier_counts": {}, "collected": 0}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    # 绕过浏览器工作线程，直接同步执行传进去的那个函数
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=20)

    assert captured["max_pages"] == selection_service.PAGES_PER_KEYWORD
    assert selection_service.PAGES_PER_KEYWORD >= 30, "额度太小会让'必 20'变成空话"


# ── 接口层 ──────────────────────────────────────────────────────────────────

def test_api_accepts_next_mode(client, monkeypatch):
    """`mode` 要能一路透到 search_batch，不能被 422 拦掉。"""
    calls: list[str] = []

    def fake_ensure_ready():
        return None

    def fake_search_batch(params, *, per_tier, mode, keywords=None):
        calls.append(mode)
        return [], {"next_page": 2, "per_tier_counts": {}, "pages": 1}

    def fake_export(rows, params, *, stats):
        from pathlib import Path
        return Path("D:/达人提报/达人提报-20261012-153000.xlsx")

    monkeypatch.setattr(selection_service, "ensure_ready", fake_ensure_ready)
    monkeypatch.setattr(selection_service, "search_batch", fake_search_batch)
    monkeypatch.setattr(selection_service, "export", fake_export)

    response = client.post("/api/selection/run", json={"mode": "next"})
    assert response.status_code == 200
    assert calls == ["next"]
    # 产物路径必须是绝对路径（装机后导出目录不在 ROOT 底下，见 test_export_payload）
    assert response.json()["export"]["dir"] == "D:\\达人提报"


def test_api_falls_back_to_next_for_unknown_mode(client, monkeypatch):
    """拼错个词不该 422，也**不该默默退回"每次给同一批人"** ⇒ 落回默认的 `next`。"""
    calls: list[str] = []
    monkeypatch.setattr(selection_service, "ensure_ready", lambda: None)
    monkeypatch.setattr(selection_service, "search_batch",
                        lambda params, *, per_tier, mode, keywords=None: (calls.append(mode) or [],
                                                           {"next_page": 2, "pages": 1}))
    monkeypatch.setattr(selection_service, "export",
                        lambda rows, params, *, stats: Path("x.xlsx"))

    assert client.post("/api/selection/run", json={"mode": "无聊"}).status_code == 200
    assert calls == ["next"]

    # 不传 mode 也一样（默认就是接着上次的位置往下取）
    assert client.post("/api/selection/run", json={}).status_code == 200
    assert calls == ["next", "next"]


def test_api_honours_an_explicit_fresh(client, monkeypatch):
    """**显式**要 `fresh` 才从头取 —— 那是界面上「从头开始」那条路。"""
    calls: list[str] = []
    monkeypatch.setattr(selection_service, "ensure_ready", lambda: None)
    monkeypatch.setattr(selection_service, "search_batch",
                        lambda params, *, per_tier, mode, keywords=None: (calls.append(mode) or [],
                                                           {"next_page": 2, "pages": 1}))
    monkeypatch.setattr(selection_service, "export",
                        lambda rows, params, *, stats: Path("x.xlsx"))

    assert client.post("/api/selection/run", json={"mode": "fresh"}).status_code == 200
    assert calls == ["fresh"]
