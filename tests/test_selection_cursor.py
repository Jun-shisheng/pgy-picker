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
    assert selection_cursor.load("", "") == 1
    assert selection_cursor.load("某行业", "某子类") == 1


def test_cursor_round_trips():
    selection_cursor.save("母婴", "婴童洗护", 7)
    assert selection_cursor.load("母婴", "婴童洗护") == 7


def test_cursor_is_kept_per_filter():
    """换了行业就是另一个结果集，页码对不上 —— 必须各记各的。"""
    selection_cursor.save("母婴", "", 5)
    assert selection_cursor.load("美妆", "") == 1, "别的行业不该继承这个游标"
    assert selection_cursor.load("母婴", "") == 5


def test_cursor_survives_a_corrupt_file(isolated_storage):
    """游标文件坏了只是"从头来"，不该让检索失败。"""
    selection_cursor.CURSOR_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
    assert selection_cursor.load("", "") == 1


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
    selection_cursor.save("母婴", "", 9)
    seen: dict = {}

    def fake_search(params, *, per_tier, start_page):
        seen["start_page"] = start_page
        return [], {"next_page": start_page + 2, "per_tier_counts": {}}

    monkeypatch.setattr(selection_service, "search", fake_search)

    selection_service.search_batch(SearchParams(first_industry="母婴"), mode="next")

    assert seen["start_page"] == 9
    assert selection_cursor.load("母婴", "") == 11, "游标该被推进到下一页"


def test_fresh_mode_ignores_the_cursor(monkeypatch):
    selection_cursor.save("母婴", "", 9)
    seen: dict = {}

    def fake_search(params, *, per_tier, start_page):
        seen["start_page"] = start_page
        return [], {"next_page": start_page + 1, "per_tier_counts": {}}

    monkeypatch.setattr(selection_service, "search", fake_search)

    selection_service.search_batch(SearchParams(first_industry="母婴"), mode="fresh")

    assert seen["start_page"] == 1, "从头取就该从第 1 页开始"


# ── 单次翻页上限 ────────────────────────────────────────────────────────────

def test_one_run_carries_the_page_safety_cap(monkeypatch):
    """目标是「每档凑够 20」，`PAGES_PER_RUN` 只是**安全上限**。

    别把它调小：调小就等于"凑不完也不翻了"，直接违背用户要的「每个量级必定要有 20 条」。
    （历史上它一度是 15，那是"单次只等 50 秒"的方案 —— 与"必 20"冲突，已废止。）
    """
    captured: dict = {}

    def fake_search_all(page, params, **kwargs):
        captured.update(kwargs)
        return [], {"pages": 0, "page_limit": selection_service.PAGES_PER_RUN,
                    "start_page": 1, "next_page": 1,
                    "total": 0, "per_tier_counts": {}, "collected": 0}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    # 绕过浏览器工作线程，直接同步执行传进去的那个函数
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    selection_service.search(SearchParams(), per_tier=20)

    assert captured["max_pages"] == selection_service.PAGES_PER_RUN
    assert selection_service.PAGES_PER_RUN >= 60, "上限太小会让'必 20'变成空话"


# ── 接口层 ──────────────────────────────────────────────────────────────────

def test_api_accepts_next_mode(client, monkeypatch):
    """`mode` 要能一路透到 search_batch，不能被 422 拦掉。"""
    calls: list[str] = []

    def fake_ensure_ready():
        return None

    def fake_search_batch(params, *, per_tier, mode):
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


def test_api_falls_back_to_fresh_for_unknown_mode(client, monkeypatch):
    """拼错个词不该 422 —— 按保守语义当"从头取"。"""
    calls: list[str] = []
    monkeypatch.setattr(selection_service, "ensure_ready", lambda: None)
    monkeypatch.setattr(selection_service, "search_batch",
                        lambda params, *, per_tier, mode: (calls.append(mode) or [],
                                                           {"next_page": 2, "pages": 1}))
    monkeypatch.setattr(selection_service, "export",
                        lambda rows, params, *, stats: Path("x.xlsx"))

    assert client.post("/api/selection/run", json={"mode": "无聊"}).status_code == 200
    assert calls == ["fresh"]
