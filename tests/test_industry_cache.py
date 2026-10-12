"""行业枚举的三种状态：活取 / 回退快照 / 无缓存。

用户看到的那个「空下拉」就是第三种状态的旧表现。这里把三种都钉住，
免得改着改着又把「有快照」和「没快照」混成同一个 409。
"""

from __future__ import annotations

from backend.services import industry_cache, selection_service

SAMPLE = [{"value": "698c373db570b54469f1b874", "label": "母婴",
           "children": [{"value": "698c373db570b54469f1b875", "label": "婴童洗护"}]}]


def _no_session(*_args, **_kwargs):
    raise selection_service.NotReady("没有可用的登录态")


def test_live_fetch_is_marked_live_and_written_to_cache(client, monkeypatch):
    monkeypatch.setattr(selection_service, "industries", lambda: SAMPLE)

    response = client.get("/api/selection/industries")
    assert response.status_code == 200
    assert response.json() == {"industries": SAMPLE, "source": "live"}
    # 活取的成果要顺手存下来 —— 否则"下次没登录也能看"就无从谈起
    assert industry_cache.load() == SAMPLE


def test_falls_back_to_cache_when_not_logged_in(client, monkeypatch):
    # 用真实写入路径造一份快照（缓存文件已被夹具隔离到临时目录）
    industry_cache.save(SAMPLE)
    monkeypatch.setattr(selection_service, "industries", _no_session)

    response = client.get("/api/selection/industries")
    assert response.status_code == 200, "有快照时不该再报 409 —— 那正是用户看到的空下拉"
    assert response.json() == {"industries": SAMPLE, "source": "cache"}


def test_reports_not_cached_when_nothing_available(client, monkeypatch):
    monkeypatch.setattr(selection_service, "industries", _no_session)

    response = client.get("/api/selection/industries")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_CACHED"
    assert "账户" in body["detail"]


def test_garbled_cache_is_treated_as_missing(tmp_path):
    """缓存读坏了应当当作"没有缓存"，而不是让选号页崩掉。"""
    bad = tmp_path / "industries-cache.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    import pytest

    from backend.services import industry_cache as cache

    original = cache.CACHE_FILE
    cache.CACHE_FILE = bad
    try:
        assert cache.load() is None
    finally:
        cache.CACHE_FILE = original
