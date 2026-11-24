"""选号编排：会话就绪 → 检索 → 映射 → 导出。

只编排，不实现细节：浏览器会话在 `providers/pugongying/session.py`，
登录在 `login.py`，检索在 `search.py`，字段映射在 `mapper.py`，Excel 在 `export_service.py`。

⚠️ **所有碰 page 的代码都必须通过 `pgy_session.run_with_page()` 提交到工作线程**：
Playwright 的 sync API 不能跨线程使用（详见 `session.py` 顶部说明）。
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from backend.core.mio_core import ROOT
from backend.providers.pugongying import enrich as pgy_enrich
from backend.providers.pugongying import login as pgy_login
from backend.providers.pugongying import pricing
from backend.providers.pugongying import session as pgy_session
from backend.providers.pugongying import type_map
from backend.providers.pugongying.fields import TIERS, tier_index, tier_labels
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
PAGES_PER_RUN = 300
#: **每个关键词**最多翻多少页。
#:
#: 原来只有一个 60 页的总预算、按关键词均分（7 个词就是每个 8 页）—— 实测那 8 页
#: 把 6 个关键词全截断了，高量级因此凑不满，界面还错报成"平台到底了"。
#: 现在改成**每个关键词各有各的额度**，配合"每档凑够 20 就整轮停"的早停，
#: 只有真需要时才花这个时间（7 个词 × 30 页最坏约 4 分钟）。
PAGES_PER_KEYWORD = 30
KOL_PAGE = "https://pgy.xiaohongshu.com/solar/pre-trade/note/kol"
EXPORT_DIR = ROOT / "exports"


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


#: 没有游标时（首次运行 / 刚清库 / 升级丢了游标），每个「档位 × 关键词」
#: 从 1..N 页里**随机**挑一个起点，避免两次「开始检索」给一模一样的名单。
#: 设 1 关闭随机（永远从第 1 页）。
SEARCH_RANDOM_PAGES = max(1, _env_int("PGY_SEARCH_RANDOM_PAGES", 3))
#: 每次检索是否打乱关键词顺序（游标签名按**排序后**的词算，换顺序不会丢游标）。
SHUFFLE_KEYWORDS = (os.environ.get("PGY_SEARCH_SHUFFLE_KEYWORDS", "1")
                    .strip().lower() not in ("0", "false", "no", "off"))


def combo_key(tier_key: str, keyword: str) -> str:
    """「档位 × 关键词」—— 游标、每格翻页数都按这个键。

    ⚠️ **关键词必须进键**（2026-10-13 修）：同一档里不同关键词是**不同的结果集**，
    页码不能互相挪用；兜底那一趟（不带检索词）更是一个**独立的池子**。
    以前键里只有档位，于是兜底那趟会把关键词几趟的页码位置覆盖掉 ——
    下一次「换一批」就拿着"无关键词池的第 7 页"去当"某关键词的第 7 页"用。
    """
    return f"{tier_key} × {keyword or '（不限）'}"


class NotReady(RuntimeError):
    """前置未就绪（无凭据 / 未登录）——文案要能直接照着做。"""


class OperationBusy(RuntimeError):
    """已有操作占着浏览器（检索或登录），当前请求不能开始。"""


def friendly_error(exc: BaseException) -> str:
    """把 Playwright 的英文「页签已关闭」翻成用户看得懂的一句话。

    场景：客户端关闭/重启的瞬间，自动登录还在等页面，Playwright 抛
    `TargetClosedError: Target page, context or browser has been closed`。
    直接在界面上显示这句英文，用户只会以为程序坏了。
    """
    text = str(exc)
    if ("Target page, context or browser has been closed" in text
            or "TargetClosedError" in text
            or "Browser has been closed" in text
            or "Connection closed" in text):
        return ("浏览器窗口被关闭了（或客户端正在退出）。请保持那个自动化浏览器窗口打开，"
                "然后重试。")
    return text


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
#: ⚠️ **必须带 `type=2`**：无参版本只到「市」，`type=2` 才是达人地域的
#: 「国家 → 省 → 市 → 区」完整树（2026-11-19 在平台 JS 里查到并实测）。
_AREA_PATH = "/api/solar/area/get_areas?type=2"
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


def _areas_inner(page) -> list[dict]:
    """平台地域枚举：国家 → 省 → 市 → 区（`type=2`，达人地域专用）。

    `code` 实测与 `name` 相同，筛选要的是**名称**；`value` 用名称（不是 code）：
    2026-11-19 实测送名称能筛对，而 `location` 参数收到字符串/对象会报
    10090102「参数格式校验失败」。
    """
    payload = page.evaluate(_FETCH_JSON, _AREA_PATH)
    items = ((payload or {}).get("data") or {}).get("list") or []

    def node(item: dict) -> dict:
        name = str(item.get("name") or item.get("code") or "")
        return {
            "value": name,
            "label": name,
            "code": str(item.get("code") or ""),
            "level": int(item.get("level") or 0),
            "children": [node(child) for child in (item.get("children") or [])],
        }

    return [node(item) for item in items]


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
    if status == 0 and "/solar/" not in (page.url or ""):
        # 页面可能停在落地页/半加载状态。再明确打开一次检索页；只重试一次，
        # 真断网/代理不通时重试十次也一样，不如尽快把可读原因交给用户。
        try:
            page.goto(KOL_PAGE, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3_000)
            status = pgy_login.login_status(page)
        except Exception as exc:
            log.warning("重新打开检索页失败：%s", str(exc).splitlines()[0][:160])
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
        "读不到平台登录态。请确认屏幕上那个自动化浏览器窗口能正常打开蒲公英"
        "（如果默认浏览器能打开、它却打不开，多半是代理/VPN 只装在默认浏览器里），"
        "然后重试。")


def _note_platform_choice(page) -> None:
    """平台偶尔会弹「请先选择合作品牌」的引导浮层。

    它**不是检索的前置条件**（我们用页面上下文直接调检索接口，实测浮层在时
    接口照样返回 200）。但它会挡住界面，用户看到会以为流程卡住了。
    这里只**记一条日志**，不替用户选品牌 —— 选哪个品牌是业务决定，
    `CLAUDE.md` 第七条：不猜、不替客户做业务选择。
    """
    try:
        if page.get_by_text("请先选择合作品牌", exact=False).count():
            log.info("平台出现「请先选择合作品牌」引导浮层；接口检索不受影响，"
                     "本次忽略（不替用户选择品牌）。")
    except Exception:
        pass


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
    _note_platform_choice(page)

    # 2026-11-23 起不再访问小红书：最后一列的中位数改从蒲公英详情页取
    # （见 `enrich.py` 顶部与 `findings.md` F-60）。所以检索前不需要扫码。


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


def _industry_flat(industries: list[dict]) -> list[dict]:
    """行业树 → 扁平候选列表（一级与二级都算候选，二级带上它的一级）。"""
    flat: list[dict] = []
    for first in industries:
        flat.append({"label": str(first.get("label") or ""), "value": str(first.get("value") or ""),
                     "first": str(first.get("value") or ""), "second": ""})
        for second in first.get("children") or []:
            flat.append({"label": str(second.get("label") or ""),
                         "value": str(second.get("value") or ""),
                         "first": str(first.get("value") or ""),
                         "second": str(second.get("value") or "")})
    return [item for item in flat if item["label"] and item["value"]]


def classify_terms(terms: list[str], *,
                   industries: list[dict] | None = None) -> tuple[list[dict], str]:
    """把用户随手输入的一串词分成「行业」与「关键词」。返回 `(结果, 行业清单来源)`。

    **只有两条可解释的路径**（用户 2026-10-13 定的口径：词要么沿着讲得通的路径
    落到一个平台实体上，要么**拒绝映射**，不许拿字符串硬凑）：

    | 路径 | 判据 | 结果 |
    |---|---|---|
    | 1. 平台正式名 | 与**活的**行业 label 完全相同 | → 按行业筛（`match="exact"`） |
    | 2. 映射表 | `type_map.TYPE_MAP` 里有一行 | 按该行的 `decision` 走 |
    | 都没有 | — | → **拒绝映射**，按笔记关键词搜（`match="no_path"`） |

    ⚠️ **这里原来还有一档 `overlap`（≥2 个连续汉字就算"像"），已删除。**
    它正是「抽象 → 亲子」这类无逻辑映射的来源 —— 详见 `type_map.py` 顶部的说明
    与 `findings.md` F-39：平台的行业树**根本没有**那些词，而字面对得上的
    「亲子家庭 / 贵妇体验」挂在汽车、母婴纸品下面，语义完全是另一回事。

    拒绝映射**不等于把词丢掉**：它降级为笔记关键词继续起作用，同时把
    `reason` / `evidence` 记进明细与运行日志 —— 判断是隐性的（界面不摆），
    但事后必须查得出来（`CLAUDE.md` 第九条）。

    行业清单取不到时（没登录 / 没快照）**不抛异常**：映射表是静态的，它的判定
    仍然有效，只是"与平台正式名完全相同"那条路径失去依据 —— 明细里如实标注来源。
    """
    if industries is None:
        try:
            industries, source = industries_cached()
        except Exception:
            log.warning("行业清单取不到，本次不做「与平台行业名完全相同」的判定",
                        exc_info=True)
            industries, source = [], "unavailable"
    else:
        # 调用方直接给了清单（测试、或将来从快照走）—— 不声明来源，别假称是活的。
        source = "given"
    flat = _industry_flat(industries)
    results: list[dict] = []

    def find_label(label: str) -> dict | None:
        key = type_map.normalize(label)
        for candidate in flat:
            if type_map.normalize(candidate["label"]) == key:
                return candidate
        return None

    for raw in terms:
        term = str(raw or "").strip()
        if not term:
            continue

        # 路径 1：平台自己就叫这个名字（最强、且不需要任何外部知识）
        best = find_label(term)
        if best is not None:
            results.append({
                "term": term, "kind": "industry", "match": "exact",
                "relation": "exact", "score": type_map.relation_score("exact"),
                "matched": best, "candidates": [best],
                "reason": "与平台行业名完全相同", "evidence": "",
            })
            continue

        # 路径 2：映射表
        row = type_map.lookup(term)
        if row is None:
            results.append({
                "term": term, "kind": "keyword", "match": "no_path",
                "relation": "none", "score": 0.0,
                "matched": None, "candidates": [],
                "reason": "既不是平台正式行业名，映射表里也没有这一行 —— "
                          "没有可解释的路径，按笔记关键词搜。",
                "evidence": "",
            })
            continue

        if row["decision"] == "map":
            target = find_label(row.get("platform_label") or "")
            if target is not None:
                results.append({
                    "term": term, "kind": "industry", "match": "map",
                    "relation": row["relation"],
                    "score": type_map.relation_score(row["relation"]),
                    "matched": target, "candidates": [target],
                    "reason": row.get("reason", ""), "evidence": row.get("evidence", ""),
                })
                continue
            # 表里指向的平台实体现在解析不出来（平台改版 / 快照过期）⇒ 降级，
            # **绝不硬塞一个可能已经失效的 ID**。
            results.append({
                "term": term, "kind": "keyword", "match": "refused",
                "relation": row["relation"], "score": 0.0,
                "matched": None, "candidates": [],
                "reason": f"映射表指向平台实体「{row.get('platform_label')}」，"
                          f"但它不在当前行业清单里（平台改版或快照过期）—— 本次不映射。",
                "evidence": row.get("evidence", ""),
            })
            continue

        # refuse（拒绝映射）/ pending（路径合理但通道未接通）
        results.append({
            "term": term, "kind": "keyword",
            "match": "refused" if row["decision"] == "refuse" else "pending",
            "relation": row["relation"],
            "score": type_map.relation_score(row["relation"]),
            "matched": None, "candidates": [],
            "reason": row.get("reason", ""), "evidence": row.get("evidence", ""),
            "platform_label": row.get("platform_label", ""),
            "platform_path": row.get("platform_path", ""),
        })

    return results, source


def resolve_tier_targets(per_tier: int = 20,
                         tier_targets: dict[str, int] | None = None) -> dict[str, int]:
    """把「勾选了哪些量级、每档几条」归一化成一个**完整**的目标表。

    `tier_targets` 为 None 时退回旧行为：六个量级都要，每档 `per_tier` 条
    （MCP 的 `per_tier` 参数走的就是这条路）。

    给了 `tier_targets` 时，**表里没有的量级视为不勾选**（不检索）；
    未知量级名直接报错 —— 宁可当场说清，也不要悄悄少搜一个档。
    """
    labels = tier_labels()
    if tier_targets is None:
        target = max(1, int(per_tier))
        return {label: target for label in labels}

    unknown = sorted(set(tier_targets) - set(labels))
    if unknown:
        raise ValueError(f"未知量级：{'、'.join(unknown)}；可用量级：{'、'.join(labels)}")
    resolved: dict[str, int] = {}
    for label in labels:
        if label not in tier_targets:
            continue
        try:
            value = int(tier_targets[label])
        except (TypeError, ValueError):
            raise ValueError(f"量级「{label}」的条数不是整数：{tier_targets[label]!r}")
        if not 1 <= value <= 500:
            raise ValueError(f"量级「{label}」的条数必须在 1–500 之间（收到 {value}）")
        resolved[label] = value
    if not resolved:
        raise ValueError("至少要勾选一个量级。")
    return resolved


def location_for(regions: list[str] | None = None) -> list[str] | None:
    """把界面勾选的**地域路径**翻成平台 `location` 参数（**最深一级名称列表**）。

    界面存的是网页版同款的路径（如 `"中国 安徽 合肥 蜀山区"`）。平台请求体的
    `location` 只收名称，**发路径会变宽**（实测 `["中国 河北"]` 会带回北京/天津等），
    所以这里只取每条路径的**最后一级名称**；完整路径留给本地 `build_gate()` 核对。

    多选是「或」：多条路径各自取最后一级，去重后一起发。
    """
    names: list[str] = []
    for item in regions or []:
        parts = str(item).split()
        if not parts:
            continue
        name = parts[-1]
        if name and name not in names:
            names.append(name)
    return names or None


def _regions_of(params: SearchParams) -> list[str]:
    """本次检索勾选的地域**路径**（本地把关用）。

    优先用 `region_paths`（界面传的完整路径）；测试或旧调用只给了
    `location` 名称时，退化成名称列表。
    """
    paths = [str(item).strip() for item in (params.region_paths or [])
             if str(item).strip()]
    if paths:
        return paths
    location = params.location
    if isinstance(location, (list, tuple)):
        return [str(item).strip() for item in location if str(item).strip()]
    if isinstance(location, str) and location.strip():
        return [location.strip()]
    return []


def _region_of(params: SearchParams) -> str:
    """本次检索的「地域」标识（游标签名用）。没选地域返回空串。

    按**排序后的完整路径**拼接：多选是「或」，换勾选顺序是同一个查询集合，
    不能因此换一个游标键。
    """
    return "、".join(sorted(_regions_of(params)))


def _report_page(page_num: int, collected: int, total: int, counts: dict) -> None:
    """每翻完一页就把进度报出去（`search_all` 的 `on_page` 钩子）。"""
    progress.update(page=page_num, collected=collected, total=total,
                    per_tier_counts=counts)


def search(params: SearchParams, *, per_tier: int = 20,
           tier_targets: dict[str, int] | None = None,
           start_pages: dict | None = None,
           keywords: list[str] | None = None,
           random_start: bool = True) -> tuple[list[dict], dict]:
    """检索并映射成交付行 —— **勾选的量级各取设定的条数**。

    `tier_targets`（量级名 → 条数）为空时退回旧口径：六个量级各取 `per_tier` 条。
    界面上没勾选的量级不会出现在 `tier_targets` 里，**完全不去问平台**。

    地域：`params.location` 是省/市名称列表（见 `SearchParams.location`）。
    平台侧会按它筛；本地 `build_gate()` 再核对一次 `location` 字段，
    万一平台忽略了这个参数，名单也不会混进外地人。

    ## 为什么改成「按档位逐个问平台」（2026-10-13，findings F-40）

    原来是**拉回一整页（什么粉丝量的人都有）再在本地剔**。平台的排序是
    `comprehensiverank`，高量级的达人在这个序里本来就稀疏 ⇒ 超头部那几档
    **永远靠碰运气**：实测 40w-50w 只收到 3 条、30w-40w 7 条、20w-30w 9 条，
    而用户的要求是**每档必 20，硬性**。

    现在把粉丝量区间**发给平台**（`fansNumberLower`/`fansNumberUpper`，F-40 实测：
    40w-50w 这一档单独就有 **2150** 人），于是每一档变成"在一个**稠密**的池子里取 20 条"——
    档位不再需要靠碰。

    **顺序：档位在外、关键词在内。** 一档凑满 20 就换下一档；关键词一个个试，
    试到要么凑满、要么这个词在这一档里翻到底。

    ## 本地两道把关仍然保留

    1. **粉丝档位**：只收 1w ≤ fans < 50w 的；
    2. **预算**：按「对应档位基准价 × 倍数」算上限，图文/视频任一在预算内即通过。

    平台侧的区间现在**已经替我们挡掉大部分**，本地这两道变成兜底 ——
    但**必须留着**：万一哪天平台又把这个参数悄悄忽略掉（F-40 里另外四种写法就是
    被静默忽略的），本地把关还在，交出去的名单就不会错。

    ## 兜底：检索词凑不满的档位，**不带检索词**再翻一遍

    某一档把检索词全跑完仍不够它自己的目标条数时，就丢开检索词、按那一档的粉丝区间
    再问一次平台（实测那一档整档的人远多于"命中检索词"的几十人）。
    ⚠️ **代价：兜底收进来的人不保证命中检索词** —— 用户 2026-10-13 拍板接受
    （口径是"每个量级必凑满"优先）。哪几档补了、各补几条，记在
    `stats["keyword_fallback"]` 与运行日志里。

    ## 游标：按「档位 × 关键词」记，续到底会自动回到第 1 页

    `start_pages` / `stats["next_pages"]` 的键都是 `combo_key(tier, keyword)` ——
    同一档里不同关键词是不同结果集，页码不能互相挪用；兜底那趟（不带检索词）
    更是独立的池子。`None` 表示从头开始。

    ⚠️ **续到空页会自动回落到第 1 页**：接着上次的位置问、平台一个人都不给，
    说明那个池子已经翻到底了。这时若直接返回空名单，用户看到的是"点了一次却什么都没出"，
    而他并不知道原因（第 1 页那批人他还想再看一轮）。回落是个显式的一次性动作，
    日志里记一行。
    """
    # 勾选的量级 → 每档目标条数。没勾的档**不列进来**，下面的循环就不会问它。
    targets = resolve_tier_targets(per_tier, tier_targets)
    labels = tier_labels()
    active = [label for label in labels if label in targets]
    region_names = _regions_of(params)
    region = "、".join(sorted(region_names))

    # 把关规则来自**唯一实现** `build_gate()` —— 检索链路与样例生成脚本共用同一份，
    # 不允许各自写一套（曾经因此漏掉预算那道，交付表混进了两个都超的达人）。
    # 地域多选是「或」：只要命中勾选的任意一个就算（与平台一致）。
    keep, drops = build_gate(region_names or None)
    tiers = {f"{label}({bracket})": (low, high)
             for low, high, label, bracket in TIERS}

    def start_for(combo: str) -> int:
        """这一格（档位 × 关键词）这次从第几页开始。

        有游标 → 接着游标；没有游标（首次/清库/升级丢了游标）→ 在
        `1..SEARCH_RANDOM_PAGES` 里**随机**挑一页，避免两次「开始检索」
        给出完全相同的名单。`random_start=False`（「从头开始」）固定第 1 页。

        ⚠️ 键是**组合**不是档位（`combo_key()`）：同一档里不同关键词是不同结果集，
        页码不能互相挪用；兜底那一趟更是一个独立的池子。
        """
        cursors = start_pages or {}
        if combo in cursors:
            try:
                return max(1, int(cursors.get(combo) or 1))
            except (TypeError, ValueError):
                return 1
        if random_start and SEARCH_RANDOM_PAGES > 1:
            return random.randint(1, SEARCH_RANDOM_PAGES)
        return 1

    def key_of(item: dict) -> str:
        index = tier_index(item.get("fansNum"))
        return labels[index] if 0 <= index < len(labels) else "档位外"

    # 关键词（findings F-35）：一个请求只能带一个，所以多关键词要**逐个搜再合并**。
    # 名额计数与"已收过的人"必须跨关键词共享 —— 否则每个关键词都各自去凑 20 条，
    # 收出好几倍的量；而且同一个人命中多个关键词时会被重复计入，
    # 变相等于"命中多个提高了优先级"（用户明确说过不该这样）。
    todo = [k.strip() for k in (keywords or []) if k and k.strip()] or [""]
    # 每次打乱关键词顺序：哪个词先把某一档凑满会变，名单也就不会两次一模一样。
    # 游标键（`search_batch` 的 signature）按**排序后**的词算，换顺序不会丢游标。
    if SHUFFLE_KEYWORDS and len(todo) > 1:
        random.shuffle(todo)
        log.info("本次关键词顺序已随机：%s", "，".join(todo))
    #: 这次有没有真的给检索词。**没有检索词时 todo 就是 `[""]`**，
    #: 那一趟本身就是"不带 keyword 的普通检索" ⇒ 不该再兜底一次（否则同一件事问两遍）。
    with_terms = todo != [""]

    counts: dict = {}
    seen: set = set()
    raw: list[dict] = []
    #: 补充主页数据（赞藏 + 近10篇点赞中位数）的结果。见 `enrich.py`。
    enrichment: dict = {}
    pages_read = 0
    cut_by_limit = False
    cut_count = 0
    per_combo_pages: dict[str, int] = {}
    next_pages: dict[str, int] = {}
    starts: dict[str, int] = {}          # 每一格这次实际从第几页开始（报给界面看）
    tier_cut: dict[str, bool] = {key: False for key in active}
    tier_queries: dict[str, int] = {key: 0 for key in active}
    tier_exhausted: dict[str, int] = {key: 0 for key in active}
    budget = PAGES_PER_RUN
    total = 0

    def satisfied() -> bool:
        return all(counts.get(k, 0) >= targets[k] for k in active)

    #: 哪些档位动用了「不带检索词」的兜底，各补进来几条（进 stats，供事后核查）。
    fallback_gained: dict[str, int] = {}

    def inner(page):
        nonlocal pages_read, cut_by_limit, cut_count, budget, total

        def run_combo(tier_key: str, keyword: str) -> int:
            """跑一个「档位 × 关键词」组合，返回这一档**新增**了几条。"""
            nonlocal pages_read, cut_by_limit, cut_count, budget, total
            combo = combo_key(tier_key, keyword)
            before = counts.get(tier_key, 0)
            tier_queries[tier_key] += 1

            def one_pass(start_page: int):
                """从 `start_page` 起翻一趟；返回 `(搜索统计, 本次翻了几页, 本次页数上限)`。"""
                nonlocal pages_read, cut_by_limit, cut_count, budget, total
                low, high = tiers[tier_key]
                cap = min(PAGES_PER_KEYWORD, budget)
                # 带上这一档的粉丝区间 —— 这就是"按档位问平台"的全部内容。
                one = replace(params, keyword=keyword, fans_lower=low, fans_upper=high)
                base = pages_read

                def on_page(page_num, _collected, page_total, tier_counts, _base=base):
                    _report_page(_base + page_num, len(seen), page_total, tier_counts)

                items, st = search_all(
                    page, one, per_tier=targets[tier_key], key_of=key_of,
                    tier_keys=[tier_key],
                    throttle=throttle.default, keep=keep, on_page=on_page,
                    start_page=start_page, max_pages=cap,
                    counts=counts, seen=seen, stall_pages=3)
                raw.extend(items)
                read = int(st.get("pages") or 0)
                pages_read += read
                budget -= read
                total = st.get("total") or total
                return st, read, cap

            start = start_for(combo)
            starts[combo] = start
            st, read, cap = one_pass(start)
            if read == 0 and start > 1:
                # 接着上次的位置问，**一个人都没有** ⇒ 上一批已经翻到底了。
                # 这时**自动回到第 1 页**：否则用户点一次「开始检索」会拿到一份空名单，
                # 而他并不知道这是"续到底"造成的（平台第 1 页那批人他其实还没看过第二轮）。
                log.info("「%s」接着第 %s 页往下已经没人了 ⇒ 这一格回到第 1 页重来", combo, start)
                st, read, cap = one_pass(1)

            per_combo_pages[combo] = read
            next_pages[combo] = int(st.get("next_page") or 1)
            if st.get("reached_end"):
                tier_exhausted[tier_key] += 1
            elif read >= cap:
                # 这一格是被页数上限截断的 ⇒ 后面还有没取到的
                cut_by_limit = True
                tier_cut[tier_key] = True
                cut_count += 1
            return counts.get(tier_key, 0) - before

        for tier_key in active:
            if budget <= 0:
                break
            for keyword in todo:
                if budget <= 0 or counts.get(tier_key, 0) >= targets[tier_key]:
                    break
                run_combo(tier_key, keyword)

            # ── 兜底：检索词在这一档里**全跑完了还是不够** ⇒ 不带检索词再翻这一档 ──
            #
            # 为什么必须兜底（2026-10-13 实测）：**"整档的人"远多于"命中检索词的人"**。
            # 40w-50w 不带检索词时整档 2150 人（F-40），可一挂上检索词就只剩几十
            # （实测：'体验' 15 人、'活人感' 0 人、另有一个词 34 人）——
            # 再怎么翻也到不了 30，界面就一直报"这一档没凑满"。
            #
            # ⚠️ **代价必须记住：兜底收进来的人不保证命中检索词。**
            # 这是用户 2026-10-13 拍板接受的取舍 —— 「每个量级必凑满」优先于
            # 「只收命中检索词的人」。所以这件事**必须留痕**：进 stats.keyword_fallback
            # 与运行日志，交出去的名单事后查得出来哪几条是补的（CLAUDE.md 第九条）。
            if with_terms and budget > 0 and counts.get(tier_key, 0) < targets[tier_key]:
                before = counts.get(tier_key, 0)
                log.info("档位 %s：检索词全跑完只有 %s 条（目标 %s），不带检索词再翻这一档…",
                         tier_key, before, targets[tier_key])
                got = run_combo(tier_key, "")
                if got:
                    fallback_gained[tier_key] = got
                    log.warning("档位 %s 补进 %s 条**不带检索词**的结果"
                                "（这些人不保证命中检索词；见 stats.keyword_fallback）",
                                tier_key, got)

        # ── 补充笔记数据：赞藏 + 近 10 篇点赞中位数（2026-11-10 新模板）──
        #
        # 搜索接口里 `likeCollectCountInfo` 恒为 null，近 10 篇更不在搜索响应里，
        # 所以拿到名单后还要**逐个**补两趟：蒲公英详情接口 + 蒲公英详情页的笔记数据。
        # 两步都过统一节流器；单个人失败留空继续（登录失效/人机验证会当场停下）。
        # 2026-11-23 起全部在蒲公英域内完成，不再访问小红书。
        if raw and page is not None:
            progress.begin(
                progress.ENRICH,
                f"正在补充赞藏与近10篇点赞（共 {len(raw)} 人，逐个打开蒲公英详情页）…")

            def on_enriched(index: int, total: int, item: dict) -> None:
                progress.update(enriched=index, enrich_total=total,
                                current=str(item.get("name") or ""))

            # ⚠️ 不传 `throttle.default`：补主页数据用 `enrich.py` 里**更慢、带长停顿**
            # 的专用节流器（小红书主页连续刷新会触发安全验证，2026-11-10 实测）。
            enrichment.update(pgy_enrich.enrich_items(page, raw, on_item=on_enriched))
        return None

    progress.begin(progress.SEARCH, "正在按量级逐档检索（浏览器窗口不会自己动，属正常）…")
    pgy_session.run_with_page(inner)

    def tier_done(tier_key: str) -> bool:
        """这一档是不是**平台那边真的没更多人了**。

        ⚠️ **"凑满 20"不算到头**（第一版就是这么写错的）：凑满只是我们**主动停手**，
        平台的池子还厚得很（40w-50w 单档 2150 人，这一轮只翻了第 1 页）。
        要是把"凑满"当成到头，界面就会**把「换一批」置灰** —— 用户再想换一批人
        就没路了。判据只能是"这一档问过的每个关键词都翻到了底、且都没被页数截断"。
        """
        if not tier_queries[tier_key]:
            return False
        return (not tier_cut[tier_key]
                and tier_exhausted[tier_key] >= tier_queries[tier_key])

    stats = {
        "pages": pages_read,
        "page_limit": PAGES_PER_RUN,
        # 游标与每格翻页数都按「档位 × 关键词」记（`combo_key()`）。界面要的
        # "本批从第几页开始 / 下一批从第几页继续"是两个代表值（取最小的那个）。
        "start_page": min(starts.values()) if starts else 1,
        "start_pages": dict(starts),
        # 没轮到的那几格（预算先耗尽了）**不进 next_pages** —— 它们本来就没被翻过，
        # 下次自然从第 1 页开始，这是对的（旧版硬塞一个起点反而会漏看最前面的人）。
        "next_pages": dict(next_pages),
        "next_page": min(next_pages.values()) if next_pages else 1,
        # ⚠️ `reached_end` 必须是**可信的**：只有"每一档要么凑满、要么真翻到底"才算到底。
        # 报错的代价是用户以为没救了（或反过来白翻一堆），所以宁可保守。
        # 只看**勾选的档位** —— 没勾的档我们根本没问，不能算它"没到底"。
        "reached_end": all(tier_done(key) for key in active),
        "stopped_by_page_limit": cut_by_limit or budget <= 0,
        "keywords_cut_by_limit": cut_count,
        # 高量级本来就少：凑不满的档位如实报出来，别装作没事。
        "tiers_short": {key: counts.get(key, 0) for key in active
                        if counts.get(key, 0) < targets[key]},
        "total": total,
        "region": region,
        "per_tier_target": per_tier,
        "per_tier_targets": {key: targets[key] for key in active},
        "per_tier_counts": {key: counts.get(key, 0) for key in active},
        "exhausted": not satisfied(),
        "keywords": [k for k in todo if k],
        "pages_per_keyword": per_combo_pages,
        # 哪些档位动用了「不带检索词」的兜底、各补进来几条。**这份名单必须有据可查**：
        # 那些人不保证命中检索词，事后要能说清名单里哪几条是补的（CLAUDE.md 第九条）。
        "keyword_fallback": fallback_gained,
        # 补充主页数据的成功/失败计数（详情接口、小红书主页）。
        "enrichment": enrichment,
    }
    # 排序规则见 `mapper.sort_rows()`（量级 → 符合度 → 图文报价 → 粉丝数）。
    # ⚠️ **匹配度（matchNoteNumber）不参与排序** —— 用户明确：命中多少个关键词
    # 都不影响优先级，优先级就是粉丝数和报价。
    rows = sort_rows(map_page(raw))
    stats["mapped"] = len(rows)
    stats["kept"] = len(rows)
    stats["dropped"] = {"out_of_range": drops["out_of_range"],
                        "out_of_region": drops["out_of_region"],
                        "no_read_median": drops["no_read_median"],
                        "no_inter_median": drops["no_inter_median"],
                        "over_budget": drops["over_budget"]}
    if pricing.load() is None:
        stats["pricing"] = "未配置（预算把关已关闭）"
    else:
        stats["pricing"] = str(pricing.source())

    short = {k: v for k, v in stats["per_tier_counts"].items() if v < targets[k]}
    log.info("检索完成：共 %s 条（翻 %s 页）；地域=%s；各量级 %s；未凑满的量级 %s",
             stats["mapped"], stats["pages"], region or "不限",
             stats["per_tier_counts"], short or "无")
    if drops["samples"]:
        log.info("超预算样例：%s", " | ".join(drops["samples"]))
    if drops["region_samples"]:
        log.info("地域不符样例：%s", " | ".join(drops["region_samples"]))
    if drops["read_median_samples"]:
        log.info("阅读中位数（日常）为空/0 样例：%s",
                 " | ".join(drops["read_median_samples"]))
    if drops["inter_median_samples"]:
        log.info("互动中位数（日常）为空/0 样例：%s",
                 " | ".join(drops["inter_median_samples"]))
    stats["per_tier_short"] = short
    return rows, stats


def search_batch(params: SearchParams, *, per_tier: int = 20, mode: str = "fresh",
                 tier_targets: dict[str, int] | None = None,
                 keywords: list[str] | None = None) -> tuple[list[dict], dict]:
    """按「从头取」或「换一批」取一次名单，并推进游标。

    - `mode="next"`（**界面上「开始检索」现在走的就是它**）：接着该条件
      **上次翻到的位置**往下取，拿到的是没看过的新人。没有游标时＝从第 1 页开始，
      所以第一次跑与 `fresh` 完全一样。
    - `mode="fresh"`：**显式**从第 1 页起 —— 同一组条件必然给**同一批人**
      （排序是平台写死的）。界面上的「从头开始」走这条。

    为什么把 `next` 设成默认（用户 2026-10-13 拍板）：平排序写死 ⇒ 每次都从头取
    等于每次发同一批人，池子里剩下的**永远不可知**；而"换一批"本来就是为这件事做的，
    只是以前要人手点一下。

    游标按「筛选条件 + 检索词」分文件键、再按**「档位 × 关键词」**分格记
    （见 `selection_cursor` 与 `combo_key`）—— 换了行业/检索词就从新的键开始，
    拿"母婴的第 7 页"去翻"美妆的第 7 页"、或把兜底那趟的页码串给关键词那趟，
    都会取回莫名其妙的人。
    """
    from backend.services import selection_cursor

    # ⚠️ 签名按**排序后**的关键词算：`search()` 每次会随机打乱关键词顺序，
    # 但那是同一个查询集合，不能因为顺序变了就换一个游标键（否则每次都从第 1 页）。
    clean_keywords = sorted(k.strip() for k in (keywords or []) if k and k.strip())
    # 地域也是筛选条件：换了地域就是另一个结果集，页码不能挪用。
    # 带地域时给签名加一个前缀；不带地域的旧签名保持原样，不丢已有游标。
    region = _region_of(params)
    signature = "，".join(clean_keywords)
    if region:
        signature = f"地域:{region}｜{signature}" if signature else f"地域:{region}"
    start_pages: dict = {}
    if mode == "next":
        start_pages = selection_cursor.load(params.first_industry, params.second_industry,
                                           signature)
        log.info("接着上次的位置往下翻（一级行业=%r，关键词=%r，各格起点=%s）",
                 params.first_industry, signature, start_pages or "从头")

    extra = {} if tier_targets is None else {"tier_targets": tier_targets}
    rows, stats = search(params, per_tier=per_tier, start_pages=start_pages,
                         keywords=keywords, **extra,
                         # 「从头开始」必须真的从第 1 页；其它情况没有游标时随机起点。
                         random_start=(mode != "fresh"))
    # 写回游标：键是「档位 × 关键词」，与读进来时同一套（见 `combo_key`）。
    selection_cursor.save(params.first_industry, params.second_industry,
                          stats.get("next_pages") or {}, signature)
    return rows, stats


def apply_terms(params: SearchParams, terms: list[str] | None) -> tuple[SearchParams, list[str], list[dict]]:
    """把用户随手输入的一串词分派到「行业」与「关键词」。返回 `(参数, 关键词表, 判断明细)`。

    **判断是隐性的**（用户 2026-10-12：「这些逻辑都是隐性的，不要暴露出来，
    我们自己程序有判断」）—— 界面上不弹确认、不摆分类结果。

    但判断明细**必须返回出去并记账**：猜错了要能事后查出来。
    这是本项目一贯的规矩（`CLAUDE.md` 第九条：每个字段都要能指回出处），
    也是用户自己要的「设置里能有日志给我们开发检查」。

    冲突处置：**下拉里显式选过行业就听它的** —— 人不该被程序猜的结果覆盖。
    识别出多个行业时只用第一个，其余降级为关键词（并记一条 warning）。
    """
    clean = [str(t).strip() for t in (terms or []) if t and str(t).strip()]
    if not clean:
        return params, [], []

    try:
        items, _source = classify_terms(clean)
    except Exception:
        # 行业清单拿不到（没登录/没缓存）不该让整轮检索失败 —— 退化成"全是关键词"。
        log.warning("检索词分类失败，一律按关键词处理", exc_info=True)
        return replace(params, keyword=""), clean, [
            {"term": t, "kind": "keyword", "match": "failed"} for t in clean]

    industries = [i for i in items if i["kind"] == "industry"]

    if params.first_industry or params.second_industry:
        # 下拉里选过 → 程序猜的行业词一律降级，只当关键词
        keywords = [i["term"] for i in items]
        if industries:
            log.info("下拉里已选行业，检索词中的 %s 一律按关键词处理",
                     [i["term"] for i in industries])
    elif industries:
        best = industries[0]["matched"]
        params = replace(params, first_industry=best["first"], second_industry=best["second"])
        keywords = [i["term"] for i in items if i["kind"] == "keyword"]
        keywords += [i["term"] for i in industries[1:]]
        log.info("检索词里识别出行业：%r → %r（%s）%s",
                 industries[0]["term"], best["label"], industries[0]["match"],
                 f"；另有 {len(industries) - 1} 个行业词降级为关键词" if len(industries) > 1 else "")
        if len(industries) > 1:
            log.warning("识别出多个行业，本次只用第一个：%s",
                        [i["term"] for i in industries])
    else:
        keywords = [i["term"] for i in items]

    # 判断明细**必须留痕**：界面不摆这些（F-37 定的"隐性"），所以日志是唯一的出口。
    # 哪天名单看着不对，先来这里看它当时把哪个词当成了什么、为什么。
    for item in items:
        if item["kind"] == "keyword" and item["match"] != "no_path":
            log.info("检索词 %r 未映射到平台实体（%s）：%s",
                     item["term"], item["match"], item.get("reason", ""))
    unmapped = [i["term"] for i in items if i["kind"] == "keyword"]
    if unmapped:
        log.info("按笔记关键词搜的词：%s（判断明细见 stats.classified_terms）", unmapped)

    return replace(params, keyword=""), keywords, items


def run_selection(params: SearchParams, *, per_tier: int = 20, mode: str = "fresh",
                  terms: list[str] | None = None,
                  tier_targets: dict[str, int] | None = None) -> dict:
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
            ensure_ready()   # 必须先登录：取行业清单要它（其中也过小红书登录凭证）
            params, keywords, classified = apply_terms(params, terms)
            extra = {} if tier_targets is None else {"tier_targets": tier_targets}
            rows, stats = search_batch(params, per_tier=per_tier, mode=mode,
                                       keywords=keywords, **extra)
        stats["classified_terms"] = classified
        path = export(rows, params, stats=stats)
    except Exception as exc:
        progress.fail(friendly_error(exc))
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


def areas() -> list[dict]:
    """平台地域枚举（中国 → 省 → 市）。**必须活取**：没有登录态取不到。"""
    return pgy_session.run_with_page(lambda page: _areas_inner(page))


def areas_cached() -> tuple[list[dict], str]:
    """给界面用的地域枚举，返回 `(列表, 来源)`；来源是 `"live"` 或 `"cache"`。

    与行业清单同一套路：活取成功顺手存快照；活取失败但有快照就用快照；
    两者都没有时把异常原样抛出去，由调用方引导去登录。
    """
    from backend.services import area_cache

    try:
        items = areas()
    except Exception:
        cached = area_cache.load()
        if cached is None:
            raise
        log.info("地域枚举活取失败，改用本地快照（%s 个一级地区）", len(cached))
        return cached, "cache"
    area_cache.save(items)
    return items, "live"


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
