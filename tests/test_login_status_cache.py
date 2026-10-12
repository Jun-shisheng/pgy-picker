"""「查状态」不能去碰浏览器 —— 那正是那个 bug 的根源。

**由来**：`/api/selection/status` 原来每次都真去问平台，而问平台要走**单工作线程**
（`pgy_session.login_status` → `submit`）。一次检索把它占住三分半，于是「查一下状态」
被堵到客户端 30 秒超时，界面上弹出一句与真因毫不相干的话
（实测：检索明明 `200` 成功返回，界面却报「选号响应超时」）。
"""

from __future__ import annotations

import pytest

from backend.services import selection_service


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selection_service, "_login_state", 0)
    monkeypatch.setattr(selection_service, "_login_state_at", 0.0)


def _fake_probe(monkeypatch, result: int, calls: list):
    def fake():
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(selection_service.pgy_session, "login_status", fake)


def test_probes_when_idle_and_caches(monkeypatch):
    calls: list = []
    _fake_probe(monkeypatch, 200, calls)

    assert selection_service.login_status() == 200
    assert calls == [1]
    assert selection_service.login_checked_age_s() is not None


def test_does_not_touch_the_browser_while_an_operation_is_running(monkeypatch):
    calls: list = []
    _fake_probe(monkeypatch, 401, calls)
    selection_service._remember_login_state(200)     # 上一次问到的值

    with selection_service.operation_guard("检索达人"):
        assert selection_service.login_status() == 200, "忙碌时应给上一次的值"

    assert calls == [], "检索期间绝不能去碰浏览器 —— 那会被堵到超时"


def test_resumes_probing_after_the_operation_finishes(monkeypatch):
    calls: list = []
    _fake_probe(monkeypatch, 401, calls)

    with selection_service.operation_guard("检索达人"):
        selection_service.login_status()
    assert selection_service.login_status() == 401
    assert calls == [1], "忙完之后要能重新问"


def test_a_failed_probe_keeps_the_known_value(monkeypatch):
    """问不到**不能**把已知状态清成「未登录」—— 那是在编造。"""
    selection_service._remember_login_state(200)
    calls: list = []
    _fake_probe(monkeypatch, RuntimeError("页面被关了"), calls)

    assert selection_service.login_status() == 200
    assert calls == [1]


def test_no_probe_never_calls_the_browser(monkeypatch):
    calls: list = []
    _fake_probe(monkeypatch, 200, calls)

    assert selection_service.login_status(probe=False) == 0
    assert calls == []


def test_partial_result_does_not_overwrite_known_state():
    """`_resolve_login` 里读到的 0 只是"这次没问着"，不是"未登录"。"""
    selection_service._remember_login_state(200)
    selection_service._remember_login_state(0)
    assert selection_service.login_status(probe=False) == 200
