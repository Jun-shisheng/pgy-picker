"""「勾选哪些量级、各取几条」与「地域筛选」。

2026-11-19 用户把每档条数从选号时的单一数字改成**按量级分别设置 + 勾选**
（不勾的档不检索），并把平台「匹配度 · 博主信息」里的**地域**接进筛选。

地域参数的形状是实测出来的（见 findings 2026-11-19）：
- `location` 收**中文名称列表**：`["安徽"]`、`["合肥"]`；
- 字符串/对象会被平台报 10090102「参数格式校验失败」；
- `["安徽", "合肥"]` 是 **OR**，省名会把整个省带回来 ⇒ 要精确到市就只发市名。
"""

from __future__ import annotations

import pytest

from backend.providers.pugongying import pricing
from backend.providers.pugongying.fields import tier_labels
from backend.providers.pugongying.mapper import build_gate, region_matches
from backend.providers.pugongying.search import SearchParams
from backend.services import selection_service

HEAD = "头部KOL(20w-30w)"
TAIL = "尾部KOL(1w-5w)"


@pytest.fixture(autouse=True)
def _no_pricing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算关掉，本文件只测档位与地域两道。"""
    monkeypatch.setattr(pricing, "load", lambda force=False: None)


# ── 目标表 ──────────────────────────────────────────────────────────────────

def test_default_targets_keep_the_old_six_tier_behaviour():
    targets = selection_service.resolve_tier_targets(20, None)
    assert targets == {label: 20 for label in tier_labels()}


def test_only_checked_tiers_survive_resolution():
    targets = selection_service.resolve_tier_targets(
        20, {HEAD: 5, TAIL: 3})
    assert targets == {HEAD: 5, TAIL: 3}


def test_unknown_tier_is_rejected_not_silently_dropped():
    with pytest.raises(ValueError, match="未知量级"):
        selection_service.resolve_tier_targets(20, {"不存在的档": 5})


def test_no_tier_at_all_is_rejected():
    with pytest.raises(ValueError, match="至少要勾选"):
        selection_service.resolve_tier_targets(20, {})
    with pytest.raises(ValueError, match="1–500"):
        selection_service.resolve_tier_targets(20, {HEAD: 0})


# ── 检索循环只看勾选的档 ────────────────────────────────────────────────────

def test_search_queries_only_checked_tiers_with_their_own_counts(monkeypatch):
    asked: list[tuple[str, int]] = []

    def fake_search_all(page, params, **kwargs):
        tier = kwargs["tier_keys"][0]
        asked.append((tier, kwargs["per_tier"]))
        kwargs["counts"][tier] = kwargs["per_tier"]
        return [], {"pages": 1, "total": 0, "per_tier_counts": {},
                    "next_page": 2, "reached_end": False}

    monkeypatch.setattr(selection_service, "search_all", fake_search_all)
    monkeypatch.setattr(selection_service.pgy_session, "run_with_page",
                        lambda fn, *args, **kwargs: fn(None))

    targets = {HEAD: 5, TAIL: 3}
    _, stats = selection_service.search(SearchParams(), per_tier=20,
                                        tier_targets=targets, keywords=["a"])

    assert asked == [(HEAD, 5), (TAIL, 3)], "没勾的档一个请求都不该发"
    assert stats["per_tier_counts"] == {HEAD: 5, TAIL: 3}
    assert stats["per_tier_targets"] == targets
    assert stats["per_tier_short"] == {}, "两档都凑满了"
    assert stats["reached_end"] is False, "凑满只是我们停手，不是平台到底"


# ── 地域 ────────────────────────────────────────────────────────────────────

def test_area_enumeration_uses_type_2_so_districts_are_included():
    """达人地域必须调 `get_areas?type=2`；无参版本只到「市」，区县会整层丢失。"""
    assert "type=2" in selection_service._AREA_PATH

    class FakePage:
        def evaluate(self, _script, path):
            assert path == selection_service._AREA_PATH
            return {"data": {"list": [{
                "code": "中国", "name": "中国", "level": 1, "children": [{
                    "code": "安徽", "name": "安徽", "level": 0, "children": [{
                        "code": "合肥", "name": "合肥", "level": 0, "children": [
                            {"code": "蜀山区", "name": "蜀山区", "level": 0, "children": None},
                        ],
                    }],
                }],
            }]}}

    tree = selection_service._areas_inner(FakePage())
    assert tree[0]["children"][0]["children"][0]["children"][0]["label"] == "蜀山区"


def test_area_cache_discards_the_old_two_level_snapshot(isolated_storage):
    """v1 缓存只到市，读到必须丢掉重取；v2 才带区县。"""
    from backend.services import area_cache

    area_cache.CACHE_FILE.write_text(
        '{"version": 1, "areas": [{"label": "中国", "children": []}]}', encoding="utf-8")
    assert area_cache.load() is None

    area_cache.save([{"label": "中国", "children": [{"label": "安徽", "children": []}]}])
    assert area_cache.load()[0]["label"] == "中国"


def test_location_body_uses_a_name_list():
    assert "location" in SearchParams().to_body()
    assert SearchParams().to_body()["location"] is None
    assert SearchParams(location=["安徽"]).to_body()["location"] == ["安徽"]


def test_checked_region_paths_send_only_the_deepest_name():
    """界面存的是路径；请求体只发每条的**最后一级名称**（发路径会变宽）。"""
    assert selection_service.location_for([
        "中国 安徽", "中国 安徽 合肥", "中国 安徽 合肥 蜀山区",
    ]) == ["安徽", "合肥", "蜀山区"]
    assert selection_service.location_for([
        "中国 安徽 合肥", "中国 安徽 合肥", "  ",
    ]) == ["合肥"]
    assert selection_service.location_for([]) is None
    assert selection_service.location_for(None) is None


def test_region_matching_uses_whole_tokens():
    assert region_matches("安徽 合肥 蜀山区", "安徽")
    assert region_matches("安徽 合肥 蜀山区", "合肥")
    assert region_matches("安徽 合肥 蜀山区", "蜀山区")
    assert region_matches("北京", "北京")
    assert not region_matches("浙江 杭州", "安徽")
    assert not region_matches("安徽 合肥", "合")       # 半个词不算
    assert not region_matches(None, "安徽")
    assert region_matches(None, "")


def test_region_matching_uses_the_full_path_when_present():
    """完整路径按层级匹配：`中国 河北` 只收河北，不能因为平台把路径当 OR 混进北京。"""
    assert region_matches("河北 石家庄 新华区", "中国 河北")
    assert region_matches("安徽 合肥 蜀山区", "中国 安徽 合肥 蜀山区")
    assert not region_matches("北京", "中国 河北")
    assert not region_matches("安徽 合肥 瑶海区", "中国 安徽 合肥 蜀山区")
    assert region_matches("安徽 合肥 蜀山区", "中国")   # 中国 = 不限


def _blogger(**overrides) -> dict:
    item = {"fansNum": 150_000, "clickMidNum": 5000, "interMidNum": 500,
            "picturePrice": 3000, "videoPrice": 4500,
            "name": "某达人", "location": "安徽 合肥 蜀山区"}
    item.update(overrides)
    return item


def test_gate_drops_people_outside_the_selected_region():
    keep, stats = build_gate("合肥")
    assert keep(_blogger()) is True
    assert keep(_blogger(location="浙江 杭州")) is False
    assert stats["out_of_region"] == 1
    assert stats["region_samples"] == ["某达人(浙江 杭州)"]


def test_gate_without_region_does_not_care_about_location():
    keep, stats = build_gate()
    assert keep(_blogger(location="浙江 杭州")) is True
    assert stats["out_of_region"] == 0


def test_gate_accepts_any_checked_region():
    """多选地域是「或」：命中勾选的任意一个就保留（与平台一致）。"""
    keep, stats = build_gate(["中国 安徽", "中国 江苏"])
    assert keep(_blogger(location="安徽 合肥 蜀山区")) is True
    assert keep(_blogger(location="江苏 南京 鼓楼区")) is True
    assert keep(_blogger(location="浙江 杭州")) is False
    assert stats["out_of_region"] == 1


def test_gate_treats_a_province_path_as_strict():
    """`中国 河北` 是严格河北；平台路径会带回北京/天津，本地必须挡掉。"""
    keep, stats = build_gate(["中国 河北"])
    assert keep(_blogger(location="河北 石家庄 新华区")) is True
    assert keep(_blogger(location="北京")) is False
    assert keep(_blogger(location="天津")) is False
    assert stats["out_of_region"] == 2


def test_cursor_signature_includes_the_region(monkeypatch):
    """换地域就是另一个结果集，游标不能挪用。"""
    seen: list[str] = []

    from backend.services import selection_cursor as cursor_mod

    def fake_load(_first, _second, signature):
        seen.append(signature)
        return {}

    def fake_save(_first, _second, _pages, signature):
        seen.append(signature)

    monkeypatch.setattr(cursor_mod, "load", fake_load)
    monkeypatch.setattr(cursor_mod, "save", fake_save)
    monkeypatch.setattr(selection_service, "search",
                        lambda params, **kwargs: ([], {"next_pages": {}}))

    selection_service.search_batch(
        SearchParams(location=["合肥"], region_paths=["中国 安徽 合肥"]),
        keywords=["b", "a"], mode="next")

    assert "地域:中国 安徽 合肥" in seen[0]
    assert seen[0].endswith("a，b"), "关键词仍按排序后算，换序不会丢游标"


# ── HTTP 层 ─────────────────────────────────────────────────────────────────

def test_api_forwards_region_and_tier_targets(client, monkeypatch):
    from backend.services import selection_service as svc

    captured: dict = {}

    def fake_run(params, *, per_tier, mode, terms=None, tier_targets=None):
        captured.update(location=params.location, region_paths=params.region_paths,
                        targets=tier_targets, per_tier=per_tier, mode=mode)
        return {"rows": [], "stats": {}, "export": {}}

    monkeypatch.setattr(svc, "ensure_ready", lambda: None)
    monkeypatch.setattr(svc, "run_selection", fake_run)

    response = client.post("/api/selection/run", json={
        "regions": ["中国 安徽 合肥 蜀山区"],
        "tier_counts": {HEAD: 7, TAIL: 4},
        "enabled_tiers": [HEAD, TAIL],
    })
    assert response.status_code == 200, response.text
    assert captured["location"] == ["蜀山区"], "请求体只发最深一级名称"
    assert captured["region_paths"] == ["中国 安徽 合肥 蜀山区"], "完整路径留给本地核对"
    assert captured["targets"] == {HEAD: 7, TAIL: 4}


def test_api_rejects_an_unknown_tier(client):
    response = client.post("/api/selection/run", json={
        "enabled_tiers": ["不存在"], "tier_counts": {"不存在": 3},
    })
    assert response.status_code == 400
    assert "未知量级" in response.json()["detail"]


def test_api_rejects_unchecking_every_tier(client):
    response = client.post("/api/selection/run", json={"enabled_tiers": []})
    assert response.status_code == 400
    assert "至少要勾选" in response.json()["detail"]


def test_status_lists_the_fixed_six_tiers(client):
    data = client.get("/api/selection/status").json()
    keys = [item["key"] for item in data["tiers"]]
    assert keys == tier_labels()
