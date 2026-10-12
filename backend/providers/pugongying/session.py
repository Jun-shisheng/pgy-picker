"""蒲公英浏览器会话：接管**专属配置**浏览器，提供已登录的 Playwright page。

边界（CLAUDE.md 第二、四条）：
- **绝不接管用户日常浏览器**：始终用我们自己的 `profiles/pugongying`。
- 凭据只在内存里传给页面填表，**不进日志、不进数据库、不进证据文件**。

为什么用「页面上下文里 fetch」而不是「另起 HTTP 客户端直连接口」：
页面里的 fetch 天然带着平台的 Cookie 与同源上下文，与网站在做的事情完全一致，
少一层自己维护会话的开销；也让「人看得见的窗口」始终是唯一事实来源。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from backend.core.mio_core import ROOT
from backend.services import mio_system_browser as sb

log = logging.getLogger("pgy.pugongying.session")

PROFILE_DIR = ROOT / "profiles" / "pugongying"
PGY_HOME = "https://pgy.xiaohongshu.com/"
KOL_PAGE = "https://pgy.xiaohongshu.com/solar/pre-trade/note/kol"
USER_INFO_PATH = "/api/solar/user/info"


class BrowserSessionError(RuntimeError):
    """会话层面的可操作错误（browser 起不来 / 没登录等）。"""


# ──────────────────────────────────────────────────────────────────────────
# 单工作线程：Playwright 的 sync API **不能跨线程使用**（对象绑定在创建它的
# 线程/事件循环上），而 FastAPI 的接口默认跑在线程池里。所以所有浏览器操作
# 都必须提交到**同一个线程**执行 —— 这也是姊妹项目「单工作线程串行」的由来。
# 副作用是好的：平台侧天然串行，不会因为我们并发而触发风控。
# ──────────────────────────────────────────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pgy-browser")


def submit(fn, *args, **kwargs):
    """把一个浏览器操作提交到工作线程并等结果。**所有 page 操作都要走这里。**"""
    return _executor.submit(fn, *args, **kwargs).result()


@dataclass
class Session:
    pw: object
    browser: object
    context: object
    page: object
    endpoint: object


_lock = threading.Lock()
_current: Session | None = None
_pw = None


def _playwright():
    """Playwright 的 sync 启动器：**每个进程只允许启一次**。

    ⚠️ 踩过的坑（2026-10-12 实际线上事故）：`sync_playwright()` 会把它的 event loop
    **一直跑在调用它的那个线程里**（greenlet 里 `run_until_complete` 不退出），于是
    同一线程里第二次调用会命中 Playwright 自己的检查，抛

        It looks like you are using Playwright Sync API inside the asyncio loop.

    ——一句**跟真实原因毫不相干**的报错（真相只是"你又 start 了一次"）。

    所以这里缓存成单例：会话丢了只重连浏览器，**绝不再 start 一次**。
    进程退出时由 `shutdown_playwright()` 负责收尾。
    """
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
    return _pw


def _profile_dir() -> Path:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return PROFILE_DIR


def current() -> Session | None:
    """**只能在浏览器工作线程里调用**（会碰 page）。"""
    session = _current
    if session is None:
        return None
    try:
        session.page.title()   # 廉价探活：页面被用户关掉就当作没有
        return session
    except Exception:
        return None


def _login_status_inner() -> int:
    session = _current
    if session is None:
        return 0
    try:
        return int(session.page.evaluate(
            f"async () => (await fetch('{USER_INFO_PATH}', "
            "{credentials:'include'})).status"))
    except Exception:
        return 0


def login_status() -> int:
    """登录态：200=已登录 / 401=未登录 / 0=问不到。可跨线程调用。"""
    return submit(_login_status_inner)


OUR_HOST = "pgy.xiaohongshu.com"


def _sweep_extra_pages(context) -> int:
    """清掉自动化留下的多余页签，只留一个，返回关掉了几个。

    为什么必须做：以前每跑一次就 `new_page()` 且从不回收，跑几次就堆出十几个
    标签页（用户实测反馈）。这里做的是**固定的"查完销毁"**：开工前先收干净，
    只复用同一个页签，而不是无限开新的。

    **只关本工具自己域名下的页签** —— 专属配置里理论上都是我们的，但按域名收窄
    是更稳的写法，将来万一混进别的东西也不会误关。
    """
    ours = []
    for candidate in context.pages:
        try:
            if OUR_HOST in (candidate.url or ""):
                ours.append(candidate)
        except Exception:
            continue
    closed = 0
    for stale in ours[1:]:
        try:
            stale.close()
            closed += 1
        except Exception:
            pass
    if closed:
        log.info("已清理上一轮残留的 %s 个页签（只保留 1 个复用）", closed)
    return closed


def _ensure_inner(start_url: str) -> Session:
    """在工作线程里建立/复用会话。"""
    global _current
    existing = current()
    if existing is not None:
        return existing
    if _current is not None:
        # 会话对象还在、但页签已经探不到（用户把窗口关了 / 浏览器崩了）。
        # 记一笔：否则下次只看到"重连了一下"，不知道中间发生过什么。
        log.warning("原会话的页签已不可用（浏览器窗口被关掉或崩溃？），重建浏览器连接")
        _current = None

    pw = _playwright()
    endpoint = sb.ensure_edge_endpoint(
        _profile_dir(), start_url=start_url, profile_kind="dedicated",
        allow_restart=True, timeout=45.0)
    browser = pw.chromium.connect_over_cdp(endpoint.cdp_url, timeout=30_000)
    context = browser.contexts[0] if browser.contexts else browser.new_context()

    # 先收干净，再决定复用还是新开 —— 顺序不能反，否则新开的那个会立刻被当残留关掉。
    _sweep_extra_pages(context)
    ours = [p for p in context.pages if OUR_HOST in (p.url or "")]
    page = ours[0] if ours else context.new_page()

    page.set_viewport_size({"width": 1440, "height": 900})
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        log.warning("打开 %s 未正常完成：%s", start_url, exc)
    _current = Session(pw=pw, browser=browser, context=context, page=page, endpoint=endpoint)
    log.info("已建立蒲公英浏览器会话：%s（页签数 %s）",
             getattr(endpoint, "summary", endpoint), len(context.pages))
    return _current


def ensure_session(*, start_url: str = PGY_HOME) -> Session:
    """拿到一个可用会话（必要时启动/接管浏览器）。**不保证已登录**。

    默认从**落地页**进：登录入口在那里。直接开检索页在未登录时会跳走，
    导致「账号登录」按钮找不到（第一次就栽在这）。

    返回的 Session 只能在 `submit()` 里使用（见本模块顶部关于线程的说明）。
    """
    with _lock:
        return submit(_ensure_inner, start_url)


def _close_inner() -> None:
    """收工：关掉我们自己的自动化页签，只留浏览器窗口本身。

    ⚠️ **刻意不调 `pw.stop()`**：Playwright 的 sync 启动器每进程只能启一次
    （见 `_playwright()`），停了之后就再也起不来了。驱动的收尾交给
    `shutdown_playwright()`，只在进程真要退出时调。
    """
    global _current
    session, _current = _current, None
    if session is None:
        return
    try:
        # **关自己那个页签**（"查完销毁"）。这正是用户要的：不无限堆页签。
        # 关掉最后一个页签会让浏览器窗口退出——那也没关系，下次开工
        # `ensure_edge_endpoint` 会重新拉起，而且省得留一个空窗口在屏幕上。
        if session.page is not None:
            session.page.close()
    except Exception:
        pass
    for closer in (getattr(session.browser, "close", None),):
        try:
            if closer:
                closer()
        except Exception:
            pass


def close_session() -> None:
    """收工：关掉自动化页签与连接，**不动用户自己的东西**。

    注意与「关客户端不动浏览器」的关系：那条规矩说的是**不关用户的浏览器/页签**。
    这里关的是本工具在**专属配置**里自己开的那个自动化页签，属于"查完销毁"，
    两件事不冲突。
    """
    with _lock:
        submit(_close_inner)


def shutdown_playwright() -> None:
    """**只在进程真要退出时调**：停掉 Playwright 驱动进程。

    调过之后再想建会话就必须重新 start —— 而那正是上面那个报错的来源，
    所以这个方法不能出现在任何"可能继续工作"的路径上。
    """
    global _pw
    with _lock:
        def inner():
            global _pw
            pw, _pw = _pw, None
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
        submit(inner)


def run_with_page(fn, *args, start_url: str = PGY_HOME, **kwargs):
    """在工作线程里确保会话就绪，再执行 `fn(page, *args, **kwargs)`。

    **这是外部唯一允许接触 page 的入口** —— page 不能离开工作线程（见模块顶部说明）。
    """
    def inner():
        session = _ensure_inner(start_url)
        return fn(session.page, *args, **kwargs)
    return submit(inner)
