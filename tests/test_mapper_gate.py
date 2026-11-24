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

    assert row["视频报价"] == "None", "缺报价写英文 None（用户明确要求）"
    assert row["图文报价"] == 3000, "有报价的那一形态照旧是数字"
    assert row[FLAGS_KEY][NO_QUOTE_KEY] == ["视频报价"]
    assert row[FLAGS_KEY][OVER_BUDGET_KEY] == [], "「没有报价」不是「超预算」，两者分开标"


def test_the_none_text_never_leaks_into_the_sorting_keys():
    """写了 `None` 那个词之后，**排序键仍然是数值**。

    顺序错了就会拿字符串当价格去比大小（缺报价本该按"极大"排在后面）。
    """
    row = map_blogger(_blogger(videoPrice=None))

    assert row[FLAGS_KEY][IMAGE_SORT_KEY] == 3000
    assert isinstance(row["图文报价"], int)


def test_both_prices_present_means_nothing_to_flag():
    row = map_blogger(_blogger())
    assert row[FLAGS_KEY][NO_QUOTE_KEY] == []
    assert row[FLAGS_KEY][OVER_BUDGET_KEY] == []


def _blogger(**overrides) -> dict:
    """一份最小可用的原始达人字典（字段名与 FIELD_MAP 对齐）。"""
    item = {
        "fansNum": 150_000,
        "clickMidNum": 5000,
        "interMidNum": 500,
        "likeCollectCountInfo": 12_345_678,
        "homepageLikeMedian": 88,
        "picturePrice": 3000,
        "videoPrice": 4500,
        "name": "某达人",
        "userId": "u1",
        "redId": "49321476371",
        "location": "浙江 杭州",
        "contentTags": [{"taxonomy1Tag": "母婴"}],
        "featureTags": ["vlog"],
    }
    item.update(overrides)
    return item


def test_keeps_a_blogger_with_data():
    keep, _ = build_gate()
    assert keep(_blogger()) is True


@pytest.mark.parametrize("bad", [0, None, "", "0"])
def test_empty_or_zero_daily_read_median_is_dropped(bad):
    """阅读中位数（日常，`clickMidNum`）为空或 0 的不要；正数都收。

    用户 2026-11-19：「为空，0的不要，其他都符合」。
    该字段**不输出到交付表**，只做本地把关。
    """
    keep, stats = build_gate()
    assert keep(_blogger(clickMidNum=bad)) is False
    assert stats["no_read_median"] == 1
    assert stats["read_median_samples"] == [f"某达人(clickMidNum={bad!r})"]


def test_positive_daily_read_median_passes():
    keep, stats = build_gate()
    assert keep(_blogger(clickMidNum=1)) is True
    assert stats["no_read_median"] == 0


@pytest.mark.parametrize("bad", [0, None, "", "0"])
def test_empty_or_zero_daily_interaction_median_is_dropped(bad):
    """互动中位数（日常，`interMidNum`）同样为空/0 不要；正数都收。"""
    keep, stats = build_gate()
    assert keep(_blogger(interMidNum=bad)) is False
    assert stats["no_inter_median"] == 1
    assert stats["inter_median_samples"] == [f"某达人(interMidNum={bad!r})"]


def test_positive_daily_interaction_median_passes():
    keep, stats = build_gate()
    assert keep(_blogger(interMidNum=1)) is True
    assert stats["no_inter_median"] == 0


def test_xiaohongshu_id_comes_from_red_id_not_user_id():
    """「小红书号」= 详情页昵称下方那串数字（`redId`），**不是** 24 位内部 `userId`。

    用户 2026-11-16 要求把它加在昵称与粉丝量之间；客户截图上的
    「小红书号：49321476371」就是 `redId`。
    """
    row = map_blogger(_blogger())

    assert row["小红书号"] == "49321476371"
    assert row["小红书号"] != "u1", "不能拿 userId 顶替"


@pytest.mark.parametrize("field", ["likeCollectCountInfo", "homepageLikeMedian"])
@pytest.mark.parametrize("bad", [0, None, ""])
def test_missing_totals_or_homepage_median_do_not_keep_anyone_out(field, bad):
    """赞藏 / 主页近10篇中位数为 0 或缺失**不剔人**。

    这两列是客户新模板新增的「展示型」字段；平台没给就留空，不能因此把
    一个粉丝/报价都合适的达人丢掉。进名单只看粉丝档位与预算两道关。
    """
    keep, stats = build_gate()
    assert keep(_blogger(**{field: bad})) is True, "展示字段不是进名单的条件"
    assert "no_median" not in stats, "这道关的计数器不该存在"


def test_other_columns_never_participate_in_sorting():
    """排序只看档位/符合度/图文报价/精确粉丝数，与赞藏、主页中位数无关。"""
    base = _blogger(name="a")
    changed = _blogger(name="b", likeCollectCountInfo=99_999_999, homepageLikeMedian=99999)
    a, b = map_blogger(base), map_blogger(changed)
    assert a[FLAGS_KEY] == b[FLAGS_KEY], "赞藏/主页中位数不该改变排序键"


def test_drop_counters_are_reported_separately():
    """剔除原因要**分类如实报数**，不能只给一个总数。"""
    keep, stats = build_gate()
    keep(_blogger(fansNum=5_000))             # 不到 1w → out_of_range
    keep(_blogger())                          # 通过（1w–50w 内、价位内）

    assert stats["out_of_range"] == 1
    assert stats["over_budget"] == 0
    assert stats["samples"] == []


def test_type_includes_all_content_and_feature_tags():
    """类型不能只取第一个 —— 用户实测 AvaFoo 有 4 个标签，我们只回了 2 个。

    contentTags = [影视综资讯, 时尚]，featureTags = [ootd, 测评]
    → 交付表「类型」必须是这 4 个，按原顺序、空格分隔。
    """
    row = map_blogger(_blogger(
        contentTags=[{"taxonomy1Tag": "影视综资讯"}, {"taxonomy1Tag": "时尚"}],
        featureTags=["ootd", "测评"],
    ))
    assert row["类型"] == "影视综资讯 时尚 ootd 测评"


def test_type_deduplicates_repeated_tags():
    row = map_blogger(_blogger(
        contentTags=[{"taxonomy1Tag": "母婴"}],
        featureTags=["vlog", "母婴", "vlog"],
    ))
    assert row["类型"] == "母婴 vlog"


def test_type_excludes_personal_tags():
    """人设标签（3-6岁 / 妈妈 / 整理师 / 创业者）**不进类型**。

    用户 2026-11-10 校准：类型只要内容类目 + 特征标签。
    """
    row = map_blogger(_blogger(
        contentTags=[{"taxonomy1Tag": "影视综资讯"}, {"taxonomy1Tag": "时尚"}],
        featureTags=["ootd", "测评"],
        personalTags=["创业者", "3-6岁", "妈妈"],
    ))
    assert row["类型"] == "影视综资讯 时尚 ootd 测评"
