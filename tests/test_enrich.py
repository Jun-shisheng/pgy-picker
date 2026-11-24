"""近 10 篇点赞中位数的纯函数与页面旁听逻辑（不碰真实浏览器）。

2026-11-23 起中位数改从**蒲公英详情页**取（`notes_rate` + `notes_detail`），
不再访问小红书。这里钉住四件最容易算错的事：
1. 「最近 10 篇」按发布时间倒序，不是接口返回顺序；
2. 不足 10 篇就按实际篇数算，不伪造；
3. 两个接口的笔记要按 `noteId` 合并去重；
4. 蒲公英风控/掉登录必须**停下**，不能当成「这个人没笔记」继续刷。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.providers.pugongying import enrich
from backend.providers.pugongying.enrich import (
    DetailUnauthorized,
    NotesBlocked,
    cache_get,
    cache_put,
    load_cache,
    median_of,
    recent_note_like_median,
    save_cache,
)


def test_median_of_odd_and_even():
    assert median_of([1, 3, 2]) == 2
    assert median_of([1, 2, 3, 4]) == 3      # (2+3)/2 = 2.5 → 四舍五入 3
    assert median_of([]) is None


def test_latest_ten_are_used_by_publish_time_not_api_order():
    notes = [
        {"noteId": "old", "publishTime": "2026-01-01", "likeNum": 999999},
        *[{"noteId": f"n{i}", "publishTime": f"2026-11-{i:02d}", "likeNum": i}
          for i in range(1, 11)],
    ]
    # 最新 10 篇是 11-01..11-10，点赞 1..10，中位数 (5+6)/2 = 5.5 → 6
    assert recent_note_like_median(notes) == 6


def test_fewer_than_ten_uses_all_available():
    notes = [
        {"noteId": "a", "publishTime": "2026-11-01", "likeNum": 100},
        {"noteId": "b", "publishTime": "2026-11-02", "likeNum": 200},
    ]
    assert recent_note_like_median(notes) == 150


def test_date_field_is_accepted_for_notes_detail():
    notes = [
        {"noteId": "a", "date": "2026-11-01", "likeNum": 100},
        {"noteId": "b", "date": "2026-11-02", "likeNum": 200},
    ]
    assert recent_note_like_median(notes) == 150


def test_missing_or_bad_like_num_is_ignored():
    notes = [
        {"noteId": "a", "publishTime": "2026-11-01", "likeNum": 100},
        {"noteId": "b", "publishTime": "2026-11-02", "likeNum": None},
        {"noteId": "c", "publishTime": "2026-11-03", "likeNum": "--"},
    ]
    assert recent_note_like_median(notes) == 100


def test_no_usable_notes_returns_none():
    assert recent_note_like_median([]) is None
    assert recent_note_like_median([{"likeNum": None}]) is None


def test_notes_cache_round_trip(tmp_path):
    path = tmp_path / "notes-cache.json"
    data = load_cache(path)
    cache_put(data, "u1", 123)
    save_cache(data, path)

    loaded = load_cache(path)
    hit, value = cache_get(loaded, "u1")
    assert hit is True and value == 123


def test_old_homepage_cache_version_is_ignored(tmp_path):
    """旧 `homepage-cache.json` 是小红书主页口径，绝不能拿来冒充新口径。"""
    path = tmp_path / "notes-cache.json"
    path.write_text(
        '{"version": 1, "items": {"u1": {"median": 999, "at": "2026-11-01T00:00:00"}}}',
        encoding="utf-8")
    assert load_cache(path)["items"] == {}


def test_expired_cache_is_a_miss(tmp_path):
    path = tmp_path / "notes-cache.json"
    data = {"version": 2, "items": {
        "u1": {"median": 123, "at": (datetime.now() - timedelta(days=30)).isoformat()}
    }}
    save_cache(data, path)

    hit, _ = cache_get(load_cache(path), "u1")
    assert hit is False, "过期条目不能再用，必须重新打开详情页"


def test_corrupt_cache_is_treated_as_empty(tmp_path):
    path = tmp_path / "notes-cache.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_cache(path)["items"] == {}


def test_cache_can_be_disabled_via_zero_days(monkeypatch):
    """`PGY_NOTES_CACHE_DAYS=0` 时缓存不生效（每次都真去详情页）。"""
    monkeypatch.setattr(enrich, "CACHE_DAYS", 0.0)
    data = {"version": 2, "items": {
        "u1": {"median": 123, "at": datetime.now().isoformat(timespec="seconds")}
    }}
    hit, _ = cache_get(data, "u1")
    assert hit is False


class _FakeResponse:
    def __init__(self, url: str, payload):
        self.url = url
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakePage:
    """够 `read_recent_notes` 用的最小页面替身。"""

    def __init__(self, responses, body: str = "", final_url: str | None = None):
        self._responses = responses
        self._handlers: list[tuple[str, object]] = []
        self.body = body
        self._final_url = final_url
        self.url = final_url or "https://pgy.xiaohongshu.com/solar/pre-trade/blogger-detail/u1"
        self.closed = False

    def on(self, event, handler):
        self._handlers.append((event, handler))

    def remove_listener(self, event, handler):
        self._handlers = [(e, h) for e, h in self._handlers
                          if not (e == event and h == handler)]

    def goto(self, url, **_kwargs):
        self.url = self._final_url or url
        for event, handler in list(self._handlers):
            if event == "response":
                for response in self._responses:
                    handler(response)

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, _js):
        return self.body

    def set_default_timeout(self, *_a, **_k):
        pass

    def set_default_navigation_timeout(self, *_a, **_k):
        pass

    def close(self):
        self.closed = True


def _rate(*notes):
    return _FakeResponse(
        "https://pgy.xiaohongshu.com/api/solar/kol/data_v3/notes_rate?userId=u1",
        {"data": {"notes": list(notes)}})


def _detail(*notes):
    return _FakeResponse(
        "https://pgy.xiaohongshu.com/api/solar/kol/data_v2/notes_detail?userId=u1",
        {"data": {"list": list(notes)}})


def test_read_recent_notes_merges_both_endpoints_and_dedups():
    page = _FakePage([
        _rate({"noteId": "a", "publishTime": "2026-11-01", "likeNum": 10},
              {"noteId": "b", "publishTime": "2026-11-02", "likeNum": 20}),
        _detail({"noteId": "b", "date": "2026-11-02", "likeNum": 20},
                {"noteId": "c", "date": "2026-10-31", "likeNum": 30}),
    ])
    notes = enrich.read_recent_notes(page, "u1")
    assert {n["noteId"] for n in notes} == {"a", "b", "c"}
    assert recent_note_like_median(notes) == 20


def test_read_recent_notes_ignores_unreadable_responses():
    page = _FakePage([
        _rate({"noteId": "a", "publishTime": "2026-11-01", "likeNum": 10}),
        _FakeResponse("...notes_detail...", ValueError("bad body")),
    ])
    notes = enrich.read_recent_notes(page, "u1", timeout_s=0.05)
    assert [n["noteId"] for n in notes] == ["a"]


def test_read_recent_notes_stops_on_pgy_verification():
    page = _FakePage([_rate({"noteId": "a", "likeNum": 10})], body="请完成下列安全验证")
    with pytest.raises(NotesBlocked, match="安全验证"):
        enrich.read_recent_notes(page, "u1", timeout_s=0.05)


def test_read_recent_notes_stops_on_login_redirect():
    page = _FakePage([], final_url="https://pgy.xiaohongshu.com/login?redirect=x")
    with pytest.raises(DetailUnauthorized, match="重新登录"):
        enrich.read_recent_notes(page, "u1", timeout_s=0.05)


def test_enrich_items_updates_detail_and_notes(monkeypatch):
    monkeypatch.setattr(enrich, "load_cache", lambda *a, **k: {"version": 2, "items": {}})
    saved = []
    monkeypatch.setattr(enrich, "save_cache", lambda data, *a, **k: saved.append(dict(data)))
    monkeypatch.setattr(enrich, "fetch_detail",
                        lambda page, uid: {"name": "某达人", "redId": "123", "userId": uid})
    monkeypatch.setattr(enrich, "read_recent_notes",
                        lambda page, uid: [{"publishTime": "2026-11-01", "likeNum": 10},
                                           {"publishTime": "2026-11-02", "likeNum": 20}])
    monkeypatch.setattr(enrich, "open_detail_page", lambda context: object())
    monkeypatch.setattr(enrich, "close_detail_page", lambda page: None)

    class _Context:
        pass

    class _Page:
        context = _Context()

    items = [{"userId": "u1", "name": "旧名字"}]
    seen = []
    stats = enrich.enrich_items(
        _Page(), items, throttle=lambda: None,
        on_item=lambda index, total, item: seen.append((index, total, item["name"])))

    assert stats["detail_ok"] == 1 and stats["notes_ok"] == 1
    assert items[0]["redId"] == "123"
    assert items[0]["name"] == "某达人"
    assert items[0]["homepageLikeMedian"] == 15
    assert seen == [(1, 1, "某达人")]
    assert saved and saved[-1]["items"]["u1"]["median"] == 15


def test_enrich_items_uses_cache_without_opening_notes(monkeypatch):
    monkeypatch.setattr(enrich, "load_cache", lambda *a, **k: {
        "version": 2, "items": {"u1": {"median": 88,
                                       "at": datetime.now().isoformat(timespec="seconds")}}})
    monkeypatch.setattr(enrich, "save_cache", lambda *a, **k: None)
    monkeypatch.setattr(enrich, "fetch_detail", lambda page, uid: {"userId": uid})
    monkeypatch.setattr(enrich, "read_recent_notes",
                        lambda *a, **k: pytest.fail("命中缓存时不应打开详情页"))
    monkeypatch.setattr(enrich, "open_detail_page", lambda context: object())
    monkeypatch.setattr(enrich, "close_detail_page", lambda page: None)

    class _Page:
        context = object()

    items = [{"userId": "u1"}]
    stats = enrich.enrich_items(_Page(), items, throttle=lambda: None)
    assert stats["notes_cached"] == 1 and stats["notes_ok"] == 0
    assert items[0]["homepageLikeMedian"] == 88
