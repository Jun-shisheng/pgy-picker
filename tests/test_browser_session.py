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

# conftest 的 autouse fixture 会把 run_with_page 换成"禁止碰浏览器"的替身；
# 这两个用例要测的正是 run_with_page 自己的重试逻辑，所以把它存一份原函数。
_REAL_RUN_WITH_PAGE = session.run_with_page


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


def test_closed_page_is_rebuilt_and_retried_once(monkeypatch):
    """页签被关掉时自动重建会话并重试一次（2026-11-10 客户端实测报错）。

    用户看到的原来是 `TargetClosedError: Target page, context or browser has been
    closed`。浏览器操作全只读，重放一次安全；重试前只丢内存会话，不关浏览器窗口。
    """
    attempts: list[int] = []
    dropped: list[int] = []

    def fake_submit(fn):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError(
                "Page.wait_for_timeout: Target page, context or browser has been closed")
        return "recovered"

    monkeypatch.setattr(session, "run_with_page", _REAL_RUN_WITH_PAGE)
    monkeypatch.setattr(session, "submit", fake_submit)
    monkeypatch.setattr(session, "_drop_current", lambda: dropped.append(1))

    assert session.run_with_page(lambda page: None) == "recovered"
    assert len(attempts) == 2, "关闭错误只重试一次"
    assert dropped == [1], "重试前要丢掉失效的会话对象"


def test_other_errors_are_not_retried(monkeypatch):
    attempts: list[int] = []

    def fake_submit(fn):
        attempts.append(1)
        raise RuntimeError("平台检索失败：HTTP 429")

    monkeypatch.setattr(session, "run_with_page", _REAL_RUN_WITH_PAGE)
    monkeypatch.setattr(session, "submit", fake_submit)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        session.run_with_page(lambda page: None)
    assert len(attempts) == 1, "非关闭类错误不能盲目重放"


def test_close_session_does_not_close_browser_or_page(monkeypatch):
    """程序关闭后浏览器必须继续开着（用户 2026-11-10 明确要求）。

    之前 `_close_inner` 会 page.close() + browser.close()，客户端一关，
    自动化 Edge 和登录态一起没了；现在只丢内存引用。
    """
    from types import SimpleNamespace

    class FakePage:
        def close(self):
            raise AssertionError("程序关闭时不应关闭页签")

    class FakeBrowser:
        def close(self):
            raise AssertionError("程序关闭时不应关闭浏览器")

    monkeypatch.setattr(session, "_current",
                        SimpleNamespace(page=FakePage(), browser=FakeBrowser()))
    session._close_inner()
    assert session._current is None
