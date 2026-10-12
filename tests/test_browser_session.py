"""浏览器会话的启动不变量。

**这个文件的由来**：2026-10-12 用户报「怎么之前可以登录，现在又不行了」，
报错是

    It looks like you are using Playwright Sync API inside the asyncio loop.

真相跟 asyncio 毫无关系：`sync_playwright()` 会把它的 event loop 一直跑在
**调用它的那个线程**里，于是同一线程里第二次调用必然命中 Playwright 自己的这个检查。
只要会话丢过一次（浏览器窗口被关掉）之后再建一次，就会撞上。

所以这里钉死：**启动器是进程内单例**。
"""

from __future__ import annotations

import pytest

from backend.providers.pugongying import session


@pytest.fixture()
def fake_playwright(monkeypatch: pytest.MonkeyPatch):
    """把 `sync_playwright()` 换成计数替身 —— 测试里绝不能真起驱动进程。

    注意真实签名是两层：`sync_playwright()` 返回上下文管理器，`.start()` 才拿到 pw。
    """
    calls: list[int] = []

    class FakeManager:
        def start(self):
            calls.append(1)
            return object()

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: FakeManager())
    monkeypatch.setattr(session, "_pw", None)
    yield calls
    # 别把假的 _pw 留给下一个用例
    session._pw = None


def test_starter_is_started_only_once(fake_playwright):
    first = session._playwright()
    second = session._playwright()

    assert first is second
    assert len(fake_playwright) == 1, (
        "第二次 sync_playwright().start() 就是那个 asyncio loop 报错的来源；"
        "会话重建时必须复用它")


def test_closing_a_session_keeps_the_starter_alive(monkeypatch, fake_playwright):
    """收工不能把驱动停掉 —— 停了之后就再也建不了会话了。"""
    starter = session._playwright()

    monkeypatch.setattr(session, "_current", None)
    session._close_inner()          # 没有会话时应是空操作

    assert session._playwright() is starter
    assert len(fake_playwright) == 1


def test_shutdown_releases_the_starter(monkeypatch, fake_playwright):
    """进程退出路径：停掉驱动，并把单例清空。"""
    stopped: list[int] = []

    class FakePW:
        def stop(self):
            stopped.append(1)

    monkeypatch.setattr(session, "_pw", FakePW())
    monkeypatch.setattr(session, "_current", None)

    session.shutdown_playwright()

    assert stopped == [1]
    assert session._pw is None
