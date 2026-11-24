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
#: 旧版本读小红书主页时用过的域名。**2026-11-23 起不再访问**；
#: 这里只用于清理历史残留页签，不代表还会发请求。
XHS_HOST = "www.xiaohongshu.com"


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
    """清掉自动化留下的多余页签，返回关掉了几个。

    为什么必须做：以前每跑一次就 `new_page()` 且从不回收，跑几次就堆出十几个
    标签页（用户实测反馈）。这里做的是**固定的"查完销毁"**：开工前先收干净，
    只复用同一个蒲公英页签；**旧版本残留的小红书页签也一并关掉**
    （2026-11-23 起不再访问小红书，这里只做清理）。

    **只关本工具自己域名下的页签** —— 专属配置里理论上都是我们的，但按域名收窄
    是更稳的写法，将来万一混进别的东西也不会误关。
    """
    ours = []
    for candidate in context.pages:
        try:
            url = candidate.url or ""
            if OUR_HOST in url or XHS_HOST in url:
                ours.append(candidate)
        except Exception:
            continue
    keep = next((p for p in ours if OUR_HOST in (p.url or "")), None)
    closed = 0
    for stale in ours:
        if stale is keep:
            continue
        try:
            stale.close()
            closed += 1
        except Exception:
            pass
    if closed:
        log.info("已清理上一轮残留的 %s 个页签（蒲公英只保留 1 个复用）", closed)
    return closed


#: 网络类错误——用户读得懂、也知道该去查什么。Playwright 的原始报错全是英文
#: 的 `net::ERR_*`，直接丢给界面等于没说。
_NETWORK_ERROR_HINTS = {
    "ERR_NAME_NOT_RESOLVED": "域名解析失败（DNS）",
    "ERR_CONNECTION_REFUSED": "连接被拒绝",
    "ERR_CONNECTION_TIMED_OUT": "连接超时",
    "ERR_TIMED_OUT": "连接超时",
    "ERR_PROXY_CONNECTION_FAILED": "代理连接失败",
    "ERR_TUNNEL_CONNECTION_FAILED": "代理隧道连接失败",
    "ERR_INTERNET_DISCONNECTED": "网络已断开",
    "ERR_ADDRESS_UNREACHABLE": "地址不可达",
    "ERR_SSL_PROTOCOL_ERROR": "SSL 握手失败",
}


def _network_message(exc: Exception) -> str:
    text = str(exc)
    for code, hint in _NETWORK_ERROR_HINTS.items():
        if code in text:
            return (f"浏览器打不开蒲公英（{hint}）。如果默认浏览器能打开，"
                    f"请检查那个自动化浏览器是否走了同一个代理/VPN，"
                    f"或稍等网络恢复后重试。")
    return (f"浏览器打开蒲公英失败：{text.splitlines()[0][:200]}。"
            f"请确认那个浏览器窗口能正常打开网站后重试。")


def _goto(page, url: str, *, timeout: int = 60_000, attempts: int = 2) -> None:
    """打开页面并等 DOM 就绪。**网络/代理问题要抛可读错误**，不能静默吞掉。

    每次只重试一次：Edge 冷启动时偶发的 `ERR_ABORTED` 多半是重定向竞态，
    重试一次就够；真正的断网/代理问题重试十次也没用，只会让用户干等。
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as exc:
            last = exc
            text = str(exc)
            if "interrupted by another navigation" in text:
                # 平台自己跳到了别的 URL（例如登录后跳 /solar/...）——等它落定即可，
                # 这不是失败。以前这里当成异常吞掉，随后 login_status 读到 0，
                # 用户看到的是"读不到登录态"，真因却是导航竞态。
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                return
            log.warning("打开 %s 失败（第 %s/%s 次）：%s", url, attempt, attempts,
                        text.splitlines()[0][:200])
            if attempt < attempts:
                page.wait_for_timeout(1_200 * attempt)
    raise BrowserSessionError(_network_message(last or RuntimeError("unknown")))


def _ensure_inner(start_url: str) -> Session:
    """在工作线程里建立/复用会话。

    ⚠️ **复用已有页签时不再强制 `goto(start_url)`**：以前每次建会话都导航一次，
    会和平台自己的登录后跳转撞车（`Navigation interrupted by another navigation`），
    紧接着 `login_status` 读到 0，表现出来就是"自动登录有时候无法访问网站"。
    """
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

    # ⚠️ **不要调 `page.set_viewport_size()`**（2026-11-10 修）：
    # 那会在一个**可见的外部 Edge 窗口**上强行覆盖 CSS 视口尺寸。窗口被最大化、
    # 或系统显示缩放不是 100% 时，页面只按固定 1440×900 排版，右侧就多出一块空白；
    # 更糟的是点击坐标按被覆盖的视口换算，登录按钮可能点不中。
    # 让页面跟随真实窗口尺寸即可。

    if not (page.url or "").startswith(PGY_HOME.rstrip("/")):
        _goto(page, start_url)
    else:
        # 已经在蒲公英：只等当前页面落定，**不要重新导航**（会打断登录后的跳转）。
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        # 上一轮可能停在 about:blank / chrome-error 的占位页上；URL 对但内容空
        # 时补一次导航。`body` 没有文字基本就是没加载出来。
        try:
            body = page.evaluate("() => (document.body && document.body.innerText || '').trim()")
        except Exception:
            body = ""
        if not body:
            _goto(page, start_url)

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
    """收工：**只丢掉内存里的会话对象，页签和浏览器一律不动**。

    用户 2026-11-10 明确要求：「程序关闭后浏览器是正常的，不要关闭和退出」。
    之前这里会 `page.close()` + `browser.close()`，客户端一关，自动化 Edge 和
    登录态一起没了 —— 与 `CLAUDE.md` 第二条（关客户端只结束自动化接管）也不符。

    现在：
    - 不关页签（关最后一个页签会把窗口带走）；
    - 不关浏览器（Edge 是独立进程，继续留着，登录态也留着）；
    - 下次启动 `_ensure_inner` 会通过端点记录重连同一个浏览器；
    - 需要人工收干净时用 `python build\\clean_tabs.py --apply`。

    ⚠️ **刻意不调 `pw.stop()`**：Playwright 的 sync 启动器每进程只能启一次
    （见 `_playwright()`），停了之后就再也起不来了。驱动的收尾交给
    `shutdown_playwright()`，只在进程真要退出时调；它只停驱动，不关浏览器。
    """
    global _current
    _current = None


def close_session() -> None:
    """收工：**只释放内存里的会话对象，浏览器/页签/登录态都不动**。

    用户 2026-11-10 明确要求：程序关闭后浏览器保持正常打开，不要关闭和退出。
    所以这里不再关页签、也不 `browser.close()`；下次启动会重连同一个浏览器。
    """
    with _lock:
        submit(_close_inner)


def _prepare_for_exit_inner() -> None:
    """退出前把自动化页从**小红书/验证页**切回蒲公英首页。

    为什么要做：客户端关闭时如果页签正停在小红书主页/登录/验证页，
    下一次打开浏览器的人可能看到一个半加载、转圈、点不动的页面。
    切回 PGY 首页只动**我们自己的自动化页签**；浏览器窗口和登录态保持不动。
    页面本身已经卡死时 goto 会超时，失败就忽略，不阻断退出。
    """
    session = _current
    if session is None or session.page is None:
        return
    try:
        url = session.page.url or ""
        if XHS_HOST in url or "website-login" in url or "captcha" in url:
            log.info("退出前把自动化页从 %s 切回蒲公英首页", url[:100])
            session.page.goto(PGY_HOME, wait_until="domcontentloaded", timeout=3_000)
    except Exception as exc:
        log.info("退出前切换页面失败（忽略）：%s", str(exc).splitlines()[0][:120])


def prepare_for_exit() -> None:
    """给客户端退出路径用：先收拾自动化页，再停驱动（不关浏览器）。"""
    with _lock:
        submit(_prepare_for_exit_inner)


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


def _drop_current() -> None:
    """只丢掉内存里的会话对象，**不关浏览器窗口**。

    用于「页签/连接已经失效」的恢复路径：浏览器窗口可能还开着（用户没关），
    只是上一轮的 CDP 连接对象已经不能用了。下次 `_ensure_inner` 会重新
    `connect_over_cdp` 到同一个端点，或按需重新拉起浏览器。
    """
    global _current
    with _lock:
        _current = None


def _is_closed_error(exc: BaseException) -> bool:
    text = str(exc)
    return ("Target page, context or browser has been closed" in text
            or "TargetClosedError" in text
            or "Browser has been closed" in text
            or "Connection closed" in text)


def run_with_page(fn, *args, start_url: str = PGY_HOME, **kwargs):
    """在工作线程里确保会话就绪，再执行 `fn(page, *args, **kwargs)`。

    **这是外部唯一允许接触 page 的入口** —— page 不能离开工作线程（见模块顶部说明）。

    ⚠️ **页签/浏览器被关掉时自动重建并重试一次**（2026-11-10 修）：
    客户端启动自动登录时，如果上一轮的浏览器窗口刚好被关掉/重启，Playwright 会抛
    `TargetClosedError: Target page, context or browser has been closed`，
    界面看到的就是一句莫名其妙的英文报错。浏览器操作全部只读，重放一次是安全的；
    重试前只丢掉内存里的会话对象，**不关用户还开着的浏览器窗口**。
    """
    def inner():
        session = _ensure_inner(start_url)
        return fn(session.page, *args, **kwargs)

    try:
        return submit(inner)
    except Exception as exc:
        if not _is_closed_error(exc):
            raise
        log.warning("浏览器页签/连接已关闭，重建会话后重试一次：%s",
                    str(exc).splitlines()[0][:160])
        _drop_current()
        return submit(inner)
