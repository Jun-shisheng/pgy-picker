"""把关规则（`build_gate`）—— 「谁能进名单」。

**现在是两道关**：粉丝档位 + 预算。

⚠️ **2026-10-13 起「中位数不能为 0／缺失」那道关已删**（同事转达「曝光中位数缺失
不影响我们的筛选」，用户拍板两个中位数都不再参与把关）。所以：

- 中位数**照样采集、照样进表**（`_apply` 那条「判 None 不判 truthy」的注释仍然有效：
  表格里 0 要显示成 0，不能变成空白）；
- 但它**不再决定进不进名单**，也从来不参与排序。

删这道关的实际代价：上一轮因它剔掉的 97 条样例**全是「曝光=0 互动=0」**
（完全没有任何合作数据的达人），现在这些人**会进名单**。
"""

from __future__ import annotations

import pytest

from backend.providers.pugongying import pricing
from backend.providers.pugongying.mapper import (
    FLAGS_KEY,
    IMAGE_SORT_KEY,
    NO_QUOTE_KEY,
    OVER_BUDGET_KEY,
    build_gate,
    map_blogger,
    sort_rows,
)


@pytest.fixture(autouse=True)
def _no_pricing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """把报价档位置成「未配置」（预算把关关闭）。

    不这样的话，本文件的结果会**取决于这台机器上有没有 `报价档位.json`**
    —— 开发机上它存在（已 gitignore 但文件在），CI 或别人机器上不存在，
    同一份测试会有两种结果。这里只测中位数与粉丝档位两道关，预算那道
    由 `test_pricing*` 单独覆盖。
    """
    monkeypatch.setattr(pricing, "load", lambda force=False: None)


def test_a_missing_price_shows_the_word_none_and_is_flagged():
    """平台没有某个报价时，交付表那格写**英文 `None`**，并**标记出来加底色**。

    用户 2026-10-13 的原话：「缺报价的地方写英文 None」+「和超预算的一样不是白底变个颜色」。
    ⚠️ **那个 `None` 是用户点名要的写法，不是调试残留** —— 别顺手改成空白或中文。
    """
    row = map_blogger(_blogger(videoPrice=None))

    assert row["视频"] == "None", "缺报价写英文 None（用户明确要求）"
    assert row["图文"] == 3000, "有报价的那一形态照旧是数字"
    assert row[FLAGS_KEY][NO_QUOTE_KEY] == ["视频"]
    assert row[FLAGS_KEY][OVER_BUDGET_KEY] == [], "「没有报价」不是「超预算」，两者分开标"


def test_the_none_text_never_leaks_into_the_sorting_keys():
    """写了 `None` 那个词之后，**排序键仍然是数值**。

    顺序错了就会拿字符串当价格去比大小（缺报价本该按"极大"排在后面）。
    """
    row = map_blogger(_blogger(videoPrice=None))

    assert row[FLAGS_KEY][IMAGE_SORT_KEY] == 3000
    assert isinstance(row["图文"], int)


def test_both_prices_present_means_nothing_to_flag():
    row = map_blogger(_blogger())
    assert row[FLAGS_KEY][NO_QUOTE_KEY] == []
    assert row[FLAGS_KEY][OVER_BUDGET_KEY] == []


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
def test_zero_or_missing_medians_no_longer_keep_anyone_out(field, bad):
    """中位数为 0／缺失**不再剔人**（用户 2026-10-13 拍板把这道关删掉）。

    实测代价就在这里面：上一轮被这道关剔掉的 97 条，样例全是「曝光=0 互动=0」——
    现在他们照进名单，交付表那两格显示 `0` 或空白。别"顺手"把这道关加回来。
    """
    keep, stats = build_gate()
    assert keep(_blogger(**{field: bad})) is True, "中位数不再是进名单的条件"
    assert "no_median" not in stats, "这道关的计数器也该一并消失"


def test_medians_never_participate_in_sorting():
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
    keep(_blogger(fansNum=5_000))             # 不到 1w → out_of_range
    keep(_blogger())                          # 通过（1w–50w 内、价位内）

    assert stats["out_of_range"] == 1
    assert stats["over_budget"] == 0
    assert stats["samples"] == []
