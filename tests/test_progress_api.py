"""选号进度：界面画步骤条与「停在哪一步」全靠它。

没有这块状态，`/api/selection/run` 那几分钟里界面只有一个转圈的按钮 ——
用户分不清是在干活、卡死了、还是早就失败了。所以这几条要钉住。
"""

from __future__ import annotations

from backend.services import progress


def test_starts_idle(client):
    body = client.get("/api/selection/progress").json()
    assert body["stage"] == "idle"
    assert body["active"] is False
    # 字段名是前后端的契约，缺了界面就会画不出来
    for key in ("label", "detail", "page", "collected", "total", "per_tier_counts",
                "started_at", "updated_at", "active", "error",
                "failed_stage", "failed_label"):
        assert key in body, f"进度快照缺字段：{key}"


def test_reports_page_and_counts_while_searching(client):
    progress.begin(progress.LOGIN)
    progress.begin(progress.SEARCH, "正在翻页…")
    progress.update(page=3, collected=42, total=5000,
                    per_tier_counts={"头部(5w-10w)": 20, "腰部(1w-5w)": 22})

    body = client.get("/api/selection/progress").json()
    assert body["stage"] == "search"
    assert body["label"] == "检索达人"
    assert body["active"] is True
    assert (body["page"], body["collected"], body["total"]) == (3, 42, 5000)
    assert body["per_tier_counts"]["腰部(1w-5w)"] == 22


def test_failure_remembers_which_step_it_died_on(client):
    progress.begin(progress.LOGIN)
    progress.begin(progress.SEARCH)
    progress.update(page=7, collected=13)
    progress.fail("平台检索失败：HTTP 403")

    body = client.get("/api/selection/progress").json()
    assert body["stage"] == "error"
    assert body["active"] is False
    assert "403" in body["error"]
    # 关键：stage 变成 error 之后，还得知道**停在哪一步**
    assert body["failed_stage"] == "search"
    assert body["failed_label"] == "检索达人"
    # 已经翻过的页码要留着，别一失败就清零
    assert body["page"] == 7


def test_new_run_resets_previous_failure(client):
    progress.begin(progress.SEARCH)
    progress.fail("第一次挂了")

    progress.begin(progress.LOGIN)
    body = client.get("/api/selection/progress").json()
    assert body["stage"] == "login"
    assert body["error"] == ""
    assert body["page"] == 0, "新一次运行不该继承上一次的页码"


def test_snapshot_is_a_copy(client):
    """返回的必须是拷贝：否则调用方一改就污染了进程内状态。"""
    progress.begin(progress.SEARCH)
    snapshot = progress.snapshot()
    snapshot["per_tier_counts"]["乱改"] = 999
    assert "乱改" not in progress.snapshot()["per_tier_counts"]
