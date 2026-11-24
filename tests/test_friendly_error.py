"""Playwright 的英文关闭错误要翻成人话（2026-11-10 实测报错）。"""

from __future__ import annotations

from backend.services.selection_service import friendly_error


def test_closed_target_is_translated():
    exc = RuntimeError(
        "Page.wait_for_timeout: Target page, context or browser has been closed")
    text = friendly_error(exc)
    assert "浏览器窗口被关闭" in text
    assert "Target page" not in text


def test_other_errors_are_left_alone():
    exc = RuntimeError("平台检索失败：HTTP 429")
    assert friendly_error(exc) == "平台检索失败：HTTP 429"
