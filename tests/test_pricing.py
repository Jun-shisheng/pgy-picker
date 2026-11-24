"""报价档位：首次用开发默认初始化、设置页保存、区间不可改。"""

from __future__ import annotations

import json

import pytest

from backend.providers.pugongying import pricing
from backend.providers.pugongying.fields import TIERS


def _tiers(prices: int = 1) -> list[dict]:
    return [
        {"label": label, "bracket": bracket, "min": low, "max": high,
         "image": prices, "video": prices}
        for low, high, label, bracket in TIERS
    ]


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把 pricing.json / 默认来源都换到临时目录，绝不动仓库根的真实价格。"""
    config = tmp_path / "pricing.json"
    default = tmp_path / "pricing.default.json"
    default.write_text(json.dumps({"multiplier": 1.3, "tiers": _tiers(1000)}),
                       encoding="utf-8")
    monkeypatch.setattr(pricing, "CONFIG_PATH", config)
    monkeypatch.setattr(pricing, "CONFIG_CANDIDATES", (config, default))
    monkeypatch.setattr(pricing, "_cache", None)
    monkeypatch.setattr(pricing, "_loaded_from", None)
    return {"config": config, "default": default}


def test_first_load_seeds_config_from_default(isolated):
    config = pricing.load()
    assert config is not None
    assert config.multiplier == 1.3
    assert len(config.tiers) == len(TIERS)
    assert isolated["config"].is_file(), "第一次加载应把默认值写成 pricing.json"
    saved = json.loads(isolated["config"].read_text(encoding="utf-8"))
    assert saved["tiers"][0]["image"] == 1000


def test_save_persists_and_load_reads_it(isolated):
    tiers = _tiers(1000)
    tiers[0]["image"] = 12345
    tiers[0]["video"] = 23456
    pricing.save({"multiplier": 1.5, "tiers": tiers})

    pricing._cache = None
    pricing._loaded_from = None
    loaded = pricing.load(force=True)
    assert loaded is not None
    assert loaded.multiplier == 1.5
    assert loaded.tiers[0].image == 12345
    assert loaded.tiers[0].video == 23456


def test_tier_ranges_cannot_be_changed(isolated):
    tiers = _tiers(1000)
    tiers[0]["min"] = 1
    with pytest.raises(ValueError, match="只能改"):
        pricing.save({"multiplier": 1.3, "tiers": tiers})


def test_bad_multiplier_is_rejected(isolated):
    with pytest.raises(ValueError, match="倍数"):
        pricing.save({"multiplier": 0, "tiers": _tiers(1000)})


def test_snapshot_falls_back_to_blank_six_tiers(isolated):
    isolated["default"].unlink()
    pricing._cache = None
    pricing._loaded_from = None
    snap = pricing.snapshot()
    assert len(snap["tiers"]) == len(TIERS)
    assert all(t["image"] == 0 and t["video"] == 0 for t in snap["tiers"])
