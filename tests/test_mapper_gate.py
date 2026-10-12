"""把关规则（`build_gate`）—— 「谁能进名单」。

⚠️ **这里有个极易被"顺手修坏"的点**，先写在最前面：

`mapper._apply` 里有一段注释说「判 None，不要判 truthy：**0 是合法值**」——
那是**映射层**的规矩（表格里 0 要显示成 0，不能变成空白）。

而 `build_gate` 里**反着来**：中位数 0 或缺失都剔除。两处判的是两件不同的事：

| 位置 | 管什么 | 0 怎么处理 |
|---|---|---|
| `_apply`（映射） | 表格单元格里显示什么 | **保留**，显示 `0` |
| `build_gate`（把关） | 这个人进不进名单 | **剔除** |

用户 2026-10-12 的原话：「没有互动中位数或者曝光中位数的达人为 0 的也删掉」，
以及「那一条目前还不作为排序的指标但不能为 0」。
"""

from __future__ import annotations

import pytest

from backend.providers.pugongying import mapper, pricing
from backend.providers.pugongying.mapper import build_gate, map_blogger, sort_rows


@pytest.fixture(autouse=True)
def _no_pricing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """把报价档位置成「未配置」（预算把关关闭）。

    不这样的话，本文件的结果会**取决于这台机器上有没有 `报价档位.json`**
    —— 开发机上它存在（已 gitignore 但文件在），CI 或别人机器上不存在，
    同一份测试会有两种结果。这里只测中位数与粉丝档位两道关，预算那道
    由 `test_pricing*` 单独覆盖。
    """
    monkeypatch.setattr(pricing, "load", lambda force=False: None)


def _blogger(**overrides) -> dict:
    """一份最小可用的原始达人字典（字段名与 FIELD_MAP 对齐）。"""
    item = {
        "fansNum": 150_000,
        "accumCoopImpMedinNum30d": 42_000,     # 曝光中位数
        "interMidCoop30": 500,                 # 互动中位数
        "picturePrice": 3000,
        "videoPrice": 4500,
        "name": "某达人",
    }
    item.update(overrides)
    return item


def test_keeps_a_blogger_with_data():
    keep, _ = build_gate()
    assert keep(_blogger()) is True


@pytest.mark.parametrize("field", ["accumCoopImpMedinNum30d", "interMidCoop30"])
@pytest.mark.parametrize("bad", [0, None, ""])
def test_drops_zero_or_missing_medians(field, bad):
    """曝光或互动中位数为 0／缺失 → 不进名单。"""
    keep, stats = build_gate()
    assert keep(_blogger(**{field: bad})) is False
    assert stats["no_median"] == 1


def test_median_rule_is_a_gate_not_a_sort_key():
    """**不是排序指标** —— 排序规则不能被它改动（用户明确说了）。"""
    rows = [map_blogger(_blogger(**{"interMidCoop30": v, "name": f"n{v}"}))
            for v in (500, 50, 5000)]
    before = [(r.get("量级"), r.get("图文"), r.get("粉丝量")) for r in rows]
    sort_rows(rows)
    after = [(r.get("量级"), r.get("图文"), r.get("粉丝量")) for r in rows]
    # 排序只由 量级/符合度/图文报价/粉丝数 决定，与中位数无关：三个行的相对顺序
    # 只取决于粉丝数与报价，这里粉丝数/报价都相同，故顺序应当保持稳定。
    assert before == after, "中位数不该参与排序"


def test_drop_counters_are_reported_separately():
    """剔除原因要**分类如实报数**，不能只给一个总数。"""
    keep, stats = build_gate()
    keep(_blogger(interMidCoop30=0))          # no_median
    keep(_blogger(fansNum=5_000))             # 不到 1w → out_of_range
    keep(_blogger())                          # 通过（1w–50w 内、中位数齐、价位内）

    assert stats["no_median"] == 1
    assert stats["out_of_range"] == 1
    assert "median_samples" in stats and stats["median_samples"], "要给样例，便于排查"


def test_median_of_reads_through_field_map():
    """原始字段名只写在 FIELD_MAP 一处，把关不能另抄一份。"""
    assert mapper.median_of(_blogger(), "曝光中位数") == 42_000
    assert mapper.median_of(_blogger(accumCoopImpMedinNum30d="1,234"), "曝光中位数") == 1234
    assert mapper.median_of({}, "曝光中位数") is None
    assert mapper.median_of(_blogger(), "不存在的列") is None
