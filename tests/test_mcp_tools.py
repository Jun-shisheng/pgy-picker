"""MCP 工具集：交给 WorkBuddy 的那一层。

**这个文件的由来**：原来唯一的工具 `pgy_status` 读的是 `/api/session` ——
那在 `app.py` 里是**硬编码死桩**（永远 `connection_status: "disconnected"`）。
结果是：平台明明登录着，Agent 却一直跟同事说「未连接」。
比"没有工具"更糟的是**给模型一个会撒谎的工具**，所以这一条用测试钉死。
"""

from __future__ import annotations

import json

import pytest

from pgy_mcp import tools
from pgy_mcp.errors import PgyError


class FakeClient:
    """记录被调过哪些路径，并按路径回放预置响应。"""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path, timeout=None):
        self.calls.append(("GET", path, None))
        return self.responses[path]

    def post(self, path, body=None, timeout=None, retry_safe=False):
        self.calls.append(("POST", path, body))
        return self.responses[path]

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]


def _texts(result: list[dict]) -> str:
    return "\n".join(item["text"] for item in result)


# ── pgy_status ──────────────────────────────────────────────────────────────

def test_status_uses_the_real_login_state_not_the_session_stub():
    client = FakeClient({
        "/api/health": {"status": "ok", "service": "ready", "product": "pgy"},
        "/api/selection/status": {"has_credentials": True, "logged_in": True, "role": "代理商"},
        "/api/account": {"has_credentials": True, "username_masked": "p***"},
    })
    text = _texts(tools.tool_status(client, {}))

    assert "/api/session" not in client.paths, (
        "不能再用 /api/session —— 那是硬编码死桩，永远回「未连接」")
    assert "已登录" in text
    assert "未登录" not in text
    assert "p***" in text


def test_status_guides_to_the_next_step_when_not_logged_in():
    client = FakeClient({
        "/api/health": {"status": "ok", "service": "ready"},
        "/api/selection/status": {"has_credentials": True, "logged_in": False},
        "/api/account": {"has_credentials": True, "username_masked": "p***"},
    })
    text = _texts(tools.tool_status(client, {}))

    assert "pgy_login" in text
    # 登录要人过验证码这件事必须写在给模型看的话里
    assert "手动" in text or "人工" in text


def test_status_warns_when_the_budget_gate_is_off():
    """没配报价档位 ⇒ 预算把关关着 ⇒ 名单可能是错的。Agent 必须说出来。"""
    client = FakeClient({
        "/api/health": {"status": "ok", "service": "ready"},
        "/api/selection/status": {"has_credentials": True, "logged_in": True,
                                  "pricing_configured": False},
        "/api/account": {"has_credentials": True, "username_masked": "p***"},
    })
    text = _texts(tools.tool_status(client, {}))

    assert "报价档位未配置" in text
    assert "如实告诉用户" in text


def test_status_stays_quiet_when_pricing_is_configured():
    client = FakeClient({
        "/api/health": {"status": "ok", "service": "ready"},
        "/api/selection/status": {"has_credentials": True, "logged_in": True,
                                  "pricing_configured": True},
        "/api/account": {"has_credentials": True, "username_masked": "p***"},
    })
    text = _texts(tools.tool_status(client, {}))

    assert "报价档位未配置" not in text


def test_status_tells_the_model_not_to_ask_for_passwords():
    client = FakeClient({
        "/api/health": {"status": "ok", "service": "ready"},
        "/api/selection/status": {"has_credentials": False, "logged_in": False},
        "/api/account": {"has_credentials": False, "username_masked": ""},
    })
    text = _texts(tools.tool_status(client, {}))

    assert "凭据管理器" in text, "必须说明密码只进凭据管理器"
    assert "不经过" in text, "必须明确告诉模型不要索要或代填密码"
    assert "账户" in text, "要指向客户端里的「账户」页"


# ── pgy_start_selection ─────────────────────────────────────────────────────

def test_start_passes_arguments_through_and_returns_immediately():
    client = FakeClient({"/api/selection/start": {"started": True, "mode": "next"}})
    tools.tool_start_selection(client, {
        "first_industry": "698c373db570b54469f1b874",
        "per_tier": 30,
        "mode": "next",
    })

    _, path, body = client.calls[0]
    assert path == "/api/selection/start"
    assert body == {"first_industry": "698c373db570b54469f1b874", "second_industry": "",
                    "per_tier": 30, "mode": "next"}


def test_start_leaves_the_mode_default_to_the_backend():
    """**MCP 不替后端定"默认取哪一批"** —— 不传 `mode`，由业务内核自己决定。

    这正是 项目设计约束 第五条说的"在 MCP 里猜业务就是重复实现"：以前这里写死了
    `"next" if ... else "fresh"`，后端把默认改成 `next` 之后它就漂了。
    """
    client = FakeClient({"/api/selection/start": {"started": True}})
    tools.tool_start_selection(client, {})

    _, _, body = client.calls[0]
    assert "mode" not in body, "没显式要求就别替后端做决定"
    assert body["per_tier"] == 20
    assert body["first_industry"] == ""


def test_start_tells_the_model_to_poll_and_not_to_restart():
    client = FakeClient({"/api/selection/start": {"started": True, "mode": "fresh"}})
    text = _texts(tools.tool_start_selection(client, {}))

    assert "pgy_selection_progress" in text, "必须告诉模型接着查进度"
    assert "不会等" in text, "必须说明它是启动即返回"
    # 浏览器窗口不动，是这个项目最容易被误判成"卡死"的地方
    assert "不会自己翻页" in text or "不驱动界面" in text


# ── pgy_selection_progress ──────────────────────────────────────────────────

def test_progress_reports_failure_with_the_step_it_died_on():
    client = FakeClient({"/api/selection/progress": {
        "stage": "error", "label": "已中断", "error": "平台检索失败：HTTP 403",
        "failed_stage": "search", "failed_label": "检索达人",
        "started_at": "10:00:00", "updated_at": "10:02:00",
    }})
    text = _texts(tools.tool_selection_progress(client, {}))

    assert "检索达人" in text, "必须说清停在哪一步"
    assert "403" in text
    assert "不要自动重试" in text


def test_progress_hands_over_the_export_path_and_limits_row_dump():
    rows = [{"昵称": f"达人{i}"} for i in range(100)]
    client = FakeClient({"/api/selection/progress": {
        "stage": "done", "label": "已完成", "started_at": "10:00:00", "updated_at": "10:05:00",
        "result": {
            "rows": rows,
            "stats": {"collected": 100, "pages": 5, "per_tier_counts": {"头部": 25}},
            "export": {"path": "D:\\Pgy\\exports\\达人提报-1.xlsx", "name": "达人提报-1.xlsx"},
        },
    }})
    text = _texts(tools.tool_selection_progress(client, {}))

    assert "D:\\Pgy\\exports\\达人提报-1.xlsx" in text
    # 100 行不能整个塞进对话
    assert "达人99" not in text
    assert f"前 {tools.RESULT_PREVIEW_ROWS} 条" in text


def test_progress_hides_internal_flags_and_forbids_invented_columns():
    """内部排序标记不能透给模型，并且要明确禁止「自己加列」。

    `__flags__` 是给排序/底色用的（tier_index / fans / over_budget…），不是交付列。
    WorkBuddy 实测会把它翻译成额外列 —— 展示层必须过滤，并把「以 Excel 列为准」
    写进返回文本（工具输出就是写给模型看的操作手册）。
    """
    rows = [{"昵称": "某达人", "小红书号": "123",
             "__flags__": {"tier_index": 0, "fans": 150000, "over_budget": []}}]
    client = FakeClient({"/api/selection/progress": {
        "stage": "done", "label": "已完成", "started_at": "10:00:00", "updated_at": "10:05:00",
        "result": {"rows": rows, "stats": {}, "export": {"path": "D:\\x.xlsx"}},
    }})
    text = _texts(tools.tool_selection_progress(client, {}))

    assert "__flags__" not in text
    assert "tier_index" not in text
    assert "小红书号" in text, "交付列要照常回显"
    assert "不要自己增删列" in text


def test_progress_when_idle_points_at_the_start_tool():
    client = FakeClient({"/api/selection/progress": {"stage": "idle", "label": "空闲"}})
    text = _texts(tools.tool_selection_progress(client, {}))

    assert "pgy_start_selection" in text


# ── 协议层 ──────────────────────────────────────────────────────────────────

def test_unknown_tool_raises_a_clear_error():
    with pytest.raises(PgyError) as excinfo:
        tools.call_tool(FakeClient({}), "nope", {})
    assert "nope" in str(excinfo.value)


def test_every_declared_tool_has_a_handler_and_a_schema():
    for tool in tools.TOOLS:
        assert tool["name"] in tools.HANDLERS, f"{tool['name']} 声明了却没有实现"
        assert tool["description"].strip(), f"{tool['name']} 缺描述（那是写给模型的手册）"
        assert tool["inputSchema"]["type"] == "object"
        assert "annotations" in tool
    assert set(tools.HANDLERS) == {tool["name"] for tool in tools.TOOLS}
