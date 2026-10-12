"""选号编排：会话就绪 → 检索 → 映射 → 导出。

只编排，不实现细节：浏览器会话在 `providers/pugongying/session.py`，
登录在 `login.py`，检索在 `search.py`，字段映射在 `mapper.py`，Excel 在 `export_service.py`。

⚠️ **所有碰 page 的代码都必须通过 `pgy_session.run_with_page()` 提交到工作线程**：
Playwright 的 sync API 不能跨线程使用（详见 `session.py` 顶部说明）。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from backend.core.mio_core import ROOT
from backend.providers.pugongying import login as pgy_login
from backend.providers.pugongying import pricing
from backend.providers.pugongying import session as pgy_session
from backend.providers.pugongying.fields import tier_index, tier_labels
from backend.providers.pugongying.mapper import build_gate, map_page, sort_rows
from backend.providers.pugongying.search import SearchParams, search_all
from backend.services import credential_service, progress, settings_service, throttle
from backend.services.export_service import export_excel

log = logging.getLogger("pgy.selection")

DEFAULT_ROLE = "代理商"
#: 单次检索的**翻页安全上限**（不是"一次等多久"的目标值）。
#:
#: 用户 2026-10-12 明确要求「每个量级必定要有 20 条」⇒ 正常情况下会**一直翻到凑齐为止**，
#: 这个数字只用来防止"物理上凑不齐时无限翻下去"（例如超头部 40w-50w 整池子实测只有
#: 十几条）。60 页 ≈ 3.5 分钟，**这个耗时用户已明确接受**。
#:
#: 中途的「连续 6 页无新增就停」兜底仍然有效，所以真到不了 20 也不会真跑满 60 页。
PAGES_PER_RUN = 60
KOL_PAGE = "https://pgy.xiaohongshu.com/solar/pre-trade/note/kol"
EXPORT_DIR = ROOT / "exports"


class NotReady(RuntimeError):
    """前置未就绪（无凭据 / 未登录）——文案要能直接照着做。"""


class OperationBusy(RuntimeError):
    """已有操作占着浏览器（检索或登录），当前请求不能开始。"""


# Playwright 的 sync API 只能在一个工作线程里跑（见 session.py），所以「碰浏览器」
# 这件事本质上是串行的。这里用一把**非阻塞**的锁把它显式化。
_operation_lock = threading.Lock()
_current_operation = ""


def is_busy() -> bool:
    """现在有没有操作占着浏览器。给「启动即返回」那条路做前置检查用。"""
    if _operation_lock.acquire(blocking=False):
        _operation_lock.release()
        return False
    return True


@contextmanager
def operation_guard(label: str):
    """占住浏览器操作权，占不到就立刻抛错。

    **故意用非阻塞 acquire**：若改成排队等待，界面上点一下「去登录」会毫无反应，
    几分钟后才忽然动起来 —— 用户只会以为程序卡死了。当场说清「正在检索达人，请等它结束」
    比静默排队好得多。
    """
    global _current_operation
    if not _operation_lock.acquire(blocking=False):
        busy = _current_operation or "执行另一项操作"
        raise OperationBusy(f"当前正在{busy}，请等它结束后再试。")
    _current_operation = label
    try:
        yield
    finally:
        _current_operation = ""
        _operation_lock.release()


# ---- 工作线程里的实现（只被 run_with_page 调用）----

_INDUSTRY_PATH = "/api/pgy/kol/get_industry_tag"
_FETCH_JSON = """
async (path) => {
  const r = await fetch(path, {credentials: "include"});
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return {_raw: t.slice(0, 500)}; }
}
"""


def _industries_inner(page) -> list[dict]:
    payload = page.evaluate(_FETCH_JSON, _INDUSTRY_PATH)
    items = ((payload or {}).get("data") or {}).get("industryTag") or []
    out = []
    for item in items:
        out.append({
            "value": item.get("value") or "",
            "label": item.get("label") or "",
            "children": [{"value": c.get("value") or "", "label": c.get("label") or ""}
                         for c in (item.get("children") or [])],
        })
    return out


def _resolve_login(page, account: str, password: str) -> str:
    """把页面弄成登录态。返回 `"already"`（本来就登着）或 `"logged_in"`（这次登的）。

    **登录判定只有这一份实现**：选号链路与「账户 → 去登录」按钮都走这里，
    免得两处各判一套、日后改一处漏一处（`CLAUDE.md` 第五条）。

    ⚠️ **区分「明确未登录」与「读不到」**：`login_status` 返回 0 表示这次没问着
    （页面刚 goto 完还没稳），不是「未登录」。把它当未登录会触发一次**多余的登录尝试**
    ——在那个没有登录弹窗的页面上必然失败，刷一堆吓人的日志（实测踩到）。
    """
    status = pgy_login.login_status(page)
    for _ in range(3):
        if status != 0:
            break
        page.wait_for_timeout(2_000)
        status = pgy_login.login_status(page)
    # 顺手记下来：这样别处查状态就不必再走一次浏览器（那会被检索堵住）。
    _remember_login_state(status)

    if status == 200:
        log.info("已是登录态")
        return "already"
    if status == 401:
        log.info("平台明确回 401：未登录，执行登录流程")
        if not pgy_login.login(page, account, password, role=""):
            raise NotReady(
                "登录未成功。可能是出现了验证码/短信验证——请在那个**看得见的浏览器窗口**里"
                "人工完成后重试。")
        return "logged_in"
    # 读不到就**不下结论**：报出来让人看一眼，而不是盲目重登或假装没事。
    raise NotReady(
        "读不到平台登录态（连续 3 次都没问着）。请确认那个浏览器窗口还能正常打开"
        "蒲公英页面，然后重试。")


def _ensure_ready_inner(page, account: str, password: str, role: str) -> None:
    """为**检索**做准备：登录 + 选身份 + 落到检索页。"""
    _resolve_login(page, account, password)

    pgy_login.enter_role(page, role)
    if "/note/kol" not in (page.url or ""):
        try:
            page.goto(KOL_PAGE, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5_000)
        except Exception as exc:
            log.warning("打开检索页未正常完成：%s", exc)


def _login_only_inner(page, account: str, password: str, role: str) -> dict:
    """只管登录，**不去检索页** —— 给「账户 → 去登录」按钮用。"""
    result = _resolve_login(page, account, password)
    pgy_login.enter_role(page, role)
    return {"already": result == "already"}


# ---- 对外接口 ----

def ensure_ready(role: str = DEFAULT_ROLE) -> None:
    """保证有一个**已登录**的浏览器会话。凭据只在内存里传递。"""
    credentials = credential_service.load()
    if credentials is None:
        raise NotReady(
            "没有可用的蒲公英账号凭据。请把账号密码放进系统凭据管理器"
            "（开发期可放 资料和说明/账号密码.txt，首次运行会自动导入）。")
    account, password = credentials
    progress.begin(progress.LOGIN, "正在确认登录态（需要时会在浏览器窗口里登录）…")
    pgy_session.run_with_page(_ensure_ready_inner, account, password, role)


def _report_page(page_num: int, collected: int, total: int, counts: dict) -> None:
    """每翻完一页就把进度报出去（`search_all` 的 `on_page` 钩子）。"""
    progress.update(page=page_num, collected=collected, total=total,
                    per_tier_counts=counts)


def search(params: SearchParams, *, per_tier: int = 20,
           start_page: int = 1) -> tuple[list[dict], dict]:
    """检索并映射成交付行 —— **每个量级各取 `per_tier` 条**。

    **本地两道把关**（平台的粉丝量筛选参数尚未破译，见 `search.py`）：

    1. **粉丝档位**：只收 1w ≤ fans < 50w 的（KOC 与 >50w 档没有对应报价行）；
    2. **预算**：按「对应档位基准价 × 倍数」算上限，图文/视频任一在预算内即通过。

    两道都在**翻页时**判，而不是取完再滤 —— 否则剔掉的人会让名额不够，只能交出短名单。

    按量级分配名额的理由见 `search_all` 的文档；高量级凑不满时**如实交少**并报数。
    """
    # 把关规则来自**唯一实现** `build_gate()` —— 检索链路与样例生成脚本共用同一份，
    # 不允许各自写一套（曾经因此漏掉预算那道，交付表混进了两个都超的达人）。
    keep, drops = build_gate()
    labels = tier_labels()

    def key_of(item: dict) -> str:
        index = tier_index(item.get("fansNum"))
        return labels[index] if 0 <= index < len(labels) else "档位外"

    progress.begin(progress.SEARCH, "正在翻页检索（浏览器窗口不会自己动，属正常）…")
    raw, stats = pgy_session.run_with_page(
        lambda page: search_all(
            page, params, per_tier=per_tier, key_of=key_of, tier_keys=labels,
            throttle=throttle.default, keep=keep, on_page=_report_page,
            start_page=start_page, max_pages=PAGES_PER_RUN))
    # 排序规则见 `mapper.sort_rows()`（量级 → 符合度 → 图文报价 → 粉丝数）
    rows = sort_rows(map_page(raw))
    stats["mapped"] = len(rows)
    stats["kept"] = len(rows)
    stats["dropped"] = {"out_of_range": drops["out_of_range"],
                        "no_median": drops["no_median"],
                        "over_budget": drops["over_budget"]}
    if pricing.load() is None:
        stats["pricing"] = "未配置（预算把关已关闭）"
    else:
        stats["pricing"] = str(pricing.source())

    short = {k: v for k, v in stats["per_tier_counts"].items() if v < per_tier}
    log.info("检索完成：共 %s 条（翻 %s 页）；各量级 %s；未凑满的量级 %s",
             stats["mapped"], stats["pages"], stats["per_tier_counts"], short or "无")
    if drops["samples"]:
        log.info("超预算样例：%s", " | ".join(drops["samples"]))
    if drops["no_median"]:
        log.info("因中位数为 0／缺失而剔除 %s 条；样例：%s",
                 drops["no_median"], " | ".join(drops["median_samples"]))
    stats["per_tier_short"] = short
    return rows, stats


def search_batch(params: SearchParams, *, per_tier: int = 20,
                 mode: str = "fresh") -> tuple[list[dict], dict]:
    """按「从头取」或「换一批」取一次名单，并推进游标。

    - `mode="fresh"`：从第 1 页开始 —— 同一组条件会得到**同一批人**（排序是平台写死的）；
       同时把游标重置到 `stats["next_page"]`。
    - `mode="next"`：接着该条件**上次翻到的位置**往下取，拿到的是没看过的新人。

    游标按筛选条件分别记（见 `selection_cursor`），换了行业就自动从头来 ——
    否则会拿"母婴的第 7 页"去翻"美妆的第 7 页"。
    """
    from backend.services import selection_cursor

    start_page = 1
    if mode == "next":
        start_page = selection_cursor.load(params.first_industry, params.second_industry)
        log.info("换一批：从第 %s 页接着翻（一级行业=%r）", start_page, params.first_industry)

    rows, stats = search(params, per_tier=per_tier, start_page=start_page)
    selection_cursor.save(params.first_industry, params.second_industry,
                          stats.get("next_page", start_page + 1))
    return rows, stats


def run_selection(params: SearchParams, *, per_tier: int = 20, mode: str = "fresh") -> dict:
    """跑完整一次选号：登录 → 检索 → 导出。

    **这是唯一的实现**：同步接口 `POST /api/selection/run` 与「启动即返回」的
    `POST /api/selection/start` 都走这里（后者只是把它丢到后台线程），
    免得两条路各写一套编排（`CLAUDE.md` 第五条）。

    进度与失败原因**就地**写进 `progress`，HTTP 层只负责把异常翻成错误码 ——
    这样 MCP 那条"启动完就返回、稍后查进度"的路才拿得到结果与停在哪一步。
    """
    try:
        # 与「账户 → 去登录」共用一把锁：浏览器只有一个工作线程，
        # 同时开两个操作只会互相排队，不如当场告诉用户谁在占着。
        with operation_guard("检索达人"):
            ensure_ready()
            rows, stats = search_batch(params, per_tier=per_tier, mode=mode)
        path = export(rows, params, stats=stats)
    except Exception as exc:
        progress.fail(str(exc))
        raise

    payload = {
        "rows": rows,
        "stats": stats,
        "export": export_payload(path),
        "throttle": throttle.default.snapshot(),
    }
    progress.finish(payload)
    return payload


def login_now(role: str = DEFAULT_ROLE) -> dict:
    """「账户 → 去登录」按钮的入口：**只登录**，不碰检索页。

    登录在那个**屏幕上看得见的浏览器窗口**里跑，人机验证由人在窗口里完成
    （`CLAUDE.md` 第二、三条：不实现任何绕过）。所以这个调用可能挂着好几分钟，
    界面要如实说明「正在等你在浏览器里完成验证」。
    """
    credentials = credential_service.load()
    if credentials is None:
        raise NotReady("还没有配置蒲公英账号。请先在「账户」页填写账号密码。")
    account, password = credentials
    # 刻意**不写选号进度**：那块状态是给「一次检索」画的步骤条。
    # 启动时的自动登录也走这里，写进去会在选号页留下一个莫名其妙的「已完成」。
    with operation_guard("登录"):
        return pgy_session.run_with_page(_login_only_inner, account, password, role)


_login_state: int = 0
_login_state_at: float = 0.0


def _remember_login_state(status: int) -> None:
    """记下**真问到的**登录态（0 = 没问着，不覆盖已知值）。"""
    global _login_state, _login_state_at
    if status:
        _login_state = status
        _login_state_at = time.time()


def login_status(*, probe: bool = True) -> int:
    """当前登录态：200=已登录 / 401=未登录 / 0=还不确定。

    ⚠️ **真去问必须走单工作线程**（`pgy_session.login_status()` → `submit`），
    而一次检索会把它占住几分钟。于是「查一下状态」这种本该瞬时返回的请求被拖到
    客户端超时 —— 界面上就冒出一句与真因毫不相干的报错
    （实测踩到：检索明明 200 成功返回，界面却报「响应超时」）。

    所以：**有长操作在跑时不真问**，直接给最近一次问到的值；平时才真问。
    界面上要据此如实标注这个值是什么时候问的（`login_checked_age_s`）。
    """
    if probe and not is_busy():
        try:
            _remember_login_state(pgy_session.login_status())
        except Exception:
            # 问不到就保留上一次的值 —— 不能把一个已知状态清成"未登录"。
            log.warning("查询登录态失败，沿用上一次的结果", exc_info=True)
    return _login_state


def login_checked_age_s() -> float | None:
    """上次真问到登录态是多久以前；从没问到过返回 None。"""
    if not _login_state_at:
        return None
    return round(time.time() - _login_state_at, 1)


def industries() -> list[dict]:
    """平台的一级/二级行业枚举（`firstIndustry`/`secondIndustry` 要的是 ID）。

    **必须活取**：没有登录态就取不到（所以会抛 `NotReady`）。
    要给界面用的请走 `industries_cached()`，那个会退回本地快照。
    """
    return pgy_session.run_with_page(lambda page: _industries_inner(page))


def industries_cached() -> tuple[list[dict], str]:
    """给界面用的行业枚举，返回 `(列表, 来源)`；来源是 `"live"` 或 `"cache"`。

    活取成功就顺手存快照；取不到就用上次的快照。活取的异常**原样抛出去**
    （由调用方决定怎么报），只有「活取失败但手上有快照」这一种情况在这里消化掉
    —— 对用户来说，看到一份旧清单比看到一个空下拉有用得多。
    """
    from backend.services import industry_cache

    try:
        items = industries()
    except Exception:
        cached = industry_cache.load()
        if cached is None:
            raise
        log.info("行业枚举活取失败，改用本地快照（%s 个一级行业）", len(cached))
        return cached, "cache"
    industry_cache.save(items)
    return items, "live"


def export_payload(path: Path) -> dict:
    """交付物在接口响应里的形状。

    ⚠️ **不要用 `path.relative_to(ROOT)` 算相对目录**：装机后 `ROOT` 是
    `%LOCALAPPDATA%\\Pgy`，而默认导出目录在「文档」下 —— **根本不在 ROOT 底下**，
    `relative_to` 会抛 `ValueError`。更糟的是它发生在**检索已经成功、Excel 也已经
    写出来之后**，用户看到的却是「选号失败」，会以为白跑一趟。
    （开发态因为 `PGY_DATA_DIR` 指到项目根，导出目录恰好落在 ROOT 里，一直没暴露。）
    """
    return {"path": str(path), "name": path.name, "dir": str(path.parent)}


def export(rows: list[dict], params: SearchParams, *, stats: dict | None = None,
           out_dir: Path | None = None) -> Path:
    """写出交付 Excel。文件名带时间戳，避免覆盖上一次。

    目录来自**设置**（用户可在界面上改，见 `settings_service`），不在代码里写死
    —— 装到客户机器上时默认落在安装目录下的 `exports/`。

    ⚠️ `params` / `stats` 现在**不写进表里**了：2026-10-12 用户要求交付物只有达人信息，
    元信息那张表已删（见 `export_service` 顶部）。参数保留是因为调用方需要一致的签名，
    而且这些信息在「设置 → 运行日志」里本来就有。
    """
    out_dir = out_dir or settings_service.export_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"达人提报-{stamp}.xlsx"
    progress.begin(progress.EXPORT, "正在按客户表头写出 Excel…")
    return export_excel(rows, path)
