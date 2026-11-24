"""补齐交付表里「搜索接口拿不到」的两列。

客户最新模板（`资料和说明/最新模板/ai需求.xlsx`，2026-11-10；2026-11-16 加「小红书号」）要 10 列：

| 列 | 来源 | 为什么不能只用搜索接口 |
|---|---|---|
| `小红书号` | 蒲公英详情接口 `GET /api/solar/cooperator/user/blogger/{userId}`（`redId`） | 搜索接口偶尔带 `redId`，**详情接口稳定有**，统一以详情为准 |
| `赞藏（w）` | 蒲公英详情接口（`likeCollectCountInfo`） | 搜索接口里 `likeCollectCountInfo` **恒为 null** |
| `主页近10篇内容的点赞中位数` | 蒲公英详情页「笔记数据 / 笔记案例」 | 搜索接口没有「最近 10 篇笔记」的点赞数 |

**2026-11-23 变更（用户拍板）**：中位数不再打开小红书主页，改从**蒲公英自己的详情页**取。

原因：小红书从 2026-11-10 起把匿名看主页改成「先登录」（`findings.md` F-48），
补主页会不断触发风控、还要人反复扫码。蒲公英详情页本身就带「日常笔记 / 笔记案例」
的逐篇点赞数，而且**就在已经登录的蒲公英域内**，完全不需要碰小红书。

口径变化（已向用户说明并接受）：
- 原来：小红书主页「最近 10 篇非置顶笔记」的点赞中位数；
- 现在：蒲公英详情页「日常笔记 + 笔记案例」合并后，**按发布时间最新的 10 篇**的点赞中位数。
  蒲公英接口不返回「置顶」标记，所以不再做置顶排除；低频达人的 30 天窗口可能
  不足 10 篇，此时用「笔记案例」的最新 8 篇补齐，仍不足 10 篇就按实际篇数算。

边界（`CLAUDE.md` 第一、二、三、七条）：

- **只读**：只导航蒲公英详情页、只读页面自己发的 GET 响应；不点赞、不关注、不评论、不私信；
- **域名收窄**：只打 `pgy.xiaohongshu.com`，**不再访问 `www.xiaohongshu.com`**；
- **节流**：每次详情请求、每次详情页导航都先过统一节流器；
- **不合成签名**：平台的 `notes_rate` / `notes_detail` 需要页面自己的 `x-s` 签名头，
  直连会 406。**不实现签名算法**，而是让浏览器正常打开详情页、页面自己发请求，
  我们只旁听响应（`findings.md` F-60）；
- **撞上人机验证就停**：由 `NotesBlocked` 上报，**不实现任何绕过**。

⚠️ 点赞数来自蒲公英接口的 `likeNum`，是平台统计值（通常比主页展示的缩写更精确）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.core.mio_core import ROOT
from backend.services.throttle import Throttle

log = logging.getLogger("pgy.pugongying.enrich")

#: 蒲公英详情接口：赞藏、小红书号、昵称、粉丝、报价、标签。
DETAIL_PATH = "/api/solar/cooperator/user/blogger/{user_id}"
#: 蒲公英达人详情页：让页面自己发 `notes_rate` / `notes_detail`（带平台签名）。
DETAIL_PAGE = "https://pgy.xiaohongshu.com/solar/pre-trade/blogger-detail/{user_id}"
#: 旁听这两个接口的响应；页面加载一次会各发一次（有时 notes_rate 会重复发两次）。
NOTES_RATE_MARKER = "notes_rate"
NOTES_DETAIL_MARKER = "notes_detail"

#: 交付表最后一列取「最近几篇」。
RECENT_NOTES = 10


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


#: 补笔记数据的节流（与翻页检索分开）。
#:
#: 用户 2026-11-10 拍板：「补充速度有点慢，把每个间隔时间去掉，直接下一个」。
#: 因此默认不额外等待（interval=0、jitter=0），一个接一个走；
#: 仍保留 `Throttle` 这层壳（调用计数/可配置），不是业务代码里裸发。
#: 现在每次是「打开一个蒲公英详情页」（约 3 秒），比原来的接口调用慢，但仍在同一域内。
#: 想恢复慢节奏可用环境变量：PGY_ENRICH_INTERVAL / PGY_ENRICH_JITTER
#:   PGY_ENRICH_PAUSE_EVERY / PGY_ENRICH_PAUSE_MIN / PGY_ENRICH_PAUSE_MAX
#:   PGY_ENRICH_LONG_PAUSE_EVERY / ..._MIN / ..._MAX
ENRICH_INTERVAL = max(0.0, _env_float("PGY_ENRICH_INTERVAL", 0.0))
ENRICH_JITTER = max(0.0, _env_float("PGY_ENRICH_JITTER", 0.0))
PAUSE_EVERY = max(0, _env_int("PGY_ENRICH_PAUSE_EVERY", 0))
PAUSE_MIN = max(0.0, _env_float("PGY_ENRICH_PAUSE_MIN", 6.0))
PAUSE_MAX = max(PAUSE_MIN, _env_float("PGY_ENRICH_PAUSE_MAX", 12.0))
LONG_PAUSE_EVERY = max(0, _env_int("PGY_ENRICH_LONG_PAUSE_EVERY", 0))
LONG_PAUSE_MIN = max(0.0, _env_float("PGY_ENRICH_LONG_PAUSE_MIN", 25.0))
LONG_PAUSE_MAX = max(LONG_PAUSE_MIN, _env_float("PGY_ENRICH_LONG_PAUSE_MAX", 45.0))
WARMUP_MIN = max(0.0, _env_float("PGY_ENRICH_WARMUP_MIN", 0.0))
WARMUP_MAX = max(WARMUP_MIN, _env_float("PGY_ENRICH_WARMUP_MAX", 0.0))
#: 打开详情页后等页面自己发笔记接口的最长时间。实测 3 秒内到齐；给足余量。
NOTES_WAIT_SECONDS = max(5.0, _env_float("PGY_NOTES_WAIT", 20.0))

notes_throttle = Throttle(min_interval=ENRICH_INTERVAL, jitter=ENRICH_JITTER)

#: 笔记中位数本地缓存：**避免重跑时把同一个人再打开一次详情页**。
#: 缓存只存 `userId → {median, at}`，不含任何账号信息；3 天过期（点赞变化很慢）。
#: `PGY_NOTES_CACHE_DAYS=0` 可关闭缓存（每次都真去详情页）。
CACHE_PATH = ROOT / "notes-cache.json"
#: ⚠️ 版本 2：旧 `homepage-cache.json` 里是**小红书主页口径**，绝不能复用。
CACHE_VERSION = 2
CACHE_DAYS = max(0.0, _env_float("PGY_NOTES_CACHE_DAYS", 3.0))
#: 「这个人没有笔记」也缓存，但只放 1 天，避免把暂时空主页永久判死。
CACHE_NONE_DAYS = max(0.0, _env_float("PGY_NOTES_CACHE_NONE_DAYS", 1.0))

#: 蒲公英详情页被风控/要求验证时的特征词（与小红书那套分开，这里只针对蒲公英）。
_BLOCK_MARKERS = (
    "安全验证", "请完成验证", "请完成下列验证", "滑动验证",
    "访问过于频繁", "操作频繁", "网络环境存在风险", "疑似机器",
)


class NotesBlocked(RuntimeError):
    """蒲公英详情页触发人机验证/风控 —— **停下等人处理**，不绕过。"""


class DetailUnauthorized(RuntimeError):
    """蒲公英登录态在采集途中失效 —— 停下让人重新登录，别把整份名单跑成空。"""


def load_cache(path: Path | str = CACHE_PATH) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"version": CACHE_VERSION, "items": {}}
    if (not isinstance(payload, dict)
            or payload.get("version") != CACHE_VERSION
            or not isinstance(payload.get("items"), dict)):
        # 旧版本（含小红书主页口径的 v1）一律当空，不能拿旧口径冒充新口径。
        return {"version": CACHE_VERSION, "items": {}}
    return payload


def save_cache(data: dict, path: Path | str = CACHE_PATH) -> None:
    """原子写：先写 `.tmp` 再替换，避免进程被杀时留下半个 JSON。"""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        # 缓存写不进去只影响"少省一次请求"，不影响本次采集结果。
        log.info("笔记缓存写入失败（不影响本次采集）：%s", type(Exception).__name__)


def cache_get(data: dict, user_id: str) -> tuple[bool, int | None]:
    """返回 `(是否命中, 中位数)`。过期的条目按未命中处理。"""
    entry = (data.get("items") or {}).get(str(user_id))
    if not isinstance(entry, dict):
        return False, None
    try:
        at = datetime.fromisoformat(str(entry.get("at") or ""))
    except Exception:
        return False, None
    age_days = max(0.0, (datetime.now() - at).total_seconds() / 86_400)
    ttl = CACHE_DAYS if entry.get("median") is not None else CACHE_NONE_DAYS
    if ttl <= 0 or age_days > ttl:
        return False, None
    return True, entry.get("median")


def cache_put(data: dict, user_id: str, median: int | None) -> None:
    data.setdefault("items", {})[str(user_id)] = {
        "median": median,
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def median_of(values: Iterable[int]) -> int | None:
    """中位数（偶数个取中间两个的平均，**四舍五入**到整数）。空集合返回 None。

    ⚠️ 用 `floor(x + 0.5)` 而不是 Python 的 `round()`：`round(2.5) == 2`
    （银行家舍入），客户看到会以为算错了。
    """
    numbers = sorted(int(v) for v in values)
    if not numbers:
        return None
    middle = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[middle]
    return int(math.floor((numbers[middle - 1] + numbers[middle]) / 2 + 0.5))


def recent_note_like_median(notes: Iterable[dict], limit: int = RECENT_NOTES) -> int | None:
    """从蒲公英笔记列表算「按发布时间最新的 `limit` 篇」的点赞中位数。

    `notes` 的每项来自 `notes_rate.notes` 或 `notes_detail.list`：
    `{noteId, publishTime | date, likeNum, ...}`。

    规则：
    1. 只看 `likeNum` 能读成整数的笔记；
    2. 按发布时间倒序取前 `limit` 篇（同一天保持接口返回顺序）；
    3. 不足 `limit` 篇就按实际篇数算；一篇都没有返回 None。

    ⚠️ 蒲公英接口**不返回置顶标记**，所以不再做「排除置顶」。这不是遗漏，
    是口径变更的一部分（见模块顶部说明）。
    """
    parsed: list[tuple[str, int]] = []
    for note in notes:
        likes = note.get("likeNum")
        if likes is None:
            continue
        try:
            likes = int(likes)
        except (TypeError, ValueError):
            continue
        stamp = str(note.get("publishTime") or note.get("date") or "")
        parsed.append((stamp, likes))

    parsed.sort(key=lambda item: item[0], reverse=True)
    return median_of(likes for _, likes in parsed[:limit])


def fetch_detail(page, user_id: str) -> dict | None:
    """在蒲公英页面上下文里请求详情接口（带着登录 Cookie）。"""
    result = page.evaluate(
        """async (path) => {
          const r = await fetch(path, {credentials: 'include'});
          const t = await r.text();
          let body = null;
          try { body = JSON.parse(t); } catch (e) { body = {_raw: t.slice(0, 300)}; }
          return {status: r.status, body};
        }""",
        DETAIL_PATH.format(user_id=user_id),
    )
    status = int(result.get("status") or 0)
    payload = result.get("body") or {}
    if status == 401:
        raise DetailUnauthorized(
            "蒲公英登录态在采集途中失效了。请在那个浏览器窗口里重新登录后重试。")
    if status != 200 or not isinstance(payload, dict) or payload.get("success") is False:
        log.warning("详情接口失败 userId=%s HTTP=%s msg=%s",
                    user_id, status, (payload or {}).get("msg"))
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _notes_from_payload(payload: Any) -> list[dict]:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    notes = data.get("notes")
    if not isinstance(notes, list):
        notes = data.get("list")
    return [n for n in (notes or []) if isinstance(n, dict)]


def open_detail_page(context):
    """开一个**专用页签**看蒲公英详情页。主蒲公英页签留在检索页不动。

    ⚠️ **不要在这里调 `page.route()`**（2026-11-10 用户实测）：
    Playwright 的请求拦截底层是 CDP 的 `Fetch.enable`。客户端/驱动突然退出时，
    被拦截的请求可能停在 `requestPaused` 没人继续，那个页签就**死锁**了。
    """
    page = context.new_page()
    page.set_default_timeout(30_000)
    page.set_default_navigation_timeout(45_000)
    return page


def close_detail_page(page) -> None:
    try:
        page.close()
    except Exception:
        pass


def read_recent_notes(page, user_id: str,
                      timeout_s: float = NOTES_WAIT_SECONDS) -> list[dict]:
    """打开蒲公英详情页，**旁听页面自己发的**笔记接口，返回合并后的笔记列表。

    为什么不直接在页面里 fetch：
    - `notes_rate` / `notes_detail` 需要平台页面的 `x-s` / `x-t` 签名头，直连返回 406；
    - 按 `CLAUDE.md`「不绕风控」，**不实现签名算法**；
    - 让浏览器正常打开详情页，页面按它自己的方式请求，我们只读响应 —— 与人在看页面一致。

    一次导航会触发 `notes_rate`（日常笔记，通常 15 篇）与 `notes_detail`
    （笔记案例，最新 8 篇）。两者按 `noteId` 合并去重，低频达人的篇数更接近 10。
    """
    captured: dict[str, list[list[dict]]] = {"rate": [], "detail": []}
    seen: set[str] = set()

    def on_response(resp):
        url = resp.url or ""
        if NOTES_RATE_MARKER in url:
            key = "rate"
        elif NOTES_DETAIL_MARKER in url:
            key = "detail"
        else:
            return
        try:
            payload = resp.json()
        except Exception:
            return
        seen.add(key)
        notes = _notes_from_payload(payload)
        if notes:
            captured[key].append(notes)

    page.on("response", on_response)
    try:
        url = DETAIL_PAGE.format(user_id=user_id)
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        # 两个接口通常 3 秒内到齐。判据是「都回应过」，不是「都有非空笔记」——
        # 否则没有笔记的达人会白等满 timeout。
        #
        # `notes_rate` 是主数据（约 15 篇）；`notes_detail` 只用来给低频达人补篇数。
        # 主数据到了之后再给次数据 2 秒；等不到就先用主数据，避免页面改版时
        # 每个人白等 20 秒（120 人就是 40 分钟）。
        deadline = time.time() + timeout_s
        detail_grace_deadline: float | None = None
        while time.time() < deadline:
            if "rate" in seen:
                if detail_grace_deadline is None:
                    detail_grace_deadline = time.time() + min(2.0, timeout_s)
                if "detail" in seen or time.time() >= detail_grace_deadline:
                    break
            elif "detail" in seen and "rate" not in seen:
                # 只见到次数据：再等一会儿主数据；等不到就用现有的。
                if detail_grace_deadline is None:
                    detail_grace_deadline = time.time() + min(2.0, timeout_s)
                if time.time() >= detail_grace_deadline:
                    break
            page.wait_for_timeout(300)
        # 给较慢的那个接口一点尾量，但不无限等。
        page.wait_for_timeout(500)

        try:
            body_text = page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 2000)")
            final_url = page.url or ""
        except Exception:
            body_text, final_url = "", ""
        if "/login" in final_url or "website-login" in final_url:
            raise DetailUnauthorized(
                "蒲公英要求重新登录后才能查看达人详情。请在那个浏览器窗口里登录后重试。")
        for marker in _BLOCK_MARKERS:
            if marker in body_text:
                raise NotesBlocked(
                    f"蒲公英详情页触发了「{marker}」。请在浏览器窗口里人工完成后重试；"
                    "程序不会尝试绕过验证。")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # 合并去重：同一篇笔记可能在两个接口里都出现，保留先到的一份（字段互补）。
    merged: dict[str, dict] = {}
    for bucket in (captured["rate"], captured["detail"]):
        for notes in bucket[:1]:          # 重复响应只取第一份
            for note in notes:
                key = str(note.get("noteId") or note.get("title")
                          or note.get("publishTime") or note.get("date") or len(merged))
                if key not in merged or (merged[key].get("likeNum") is None
                                         and note.get("likeNum") is not None):
                    merged[key] = note
    if not merged:
        log.warning("详情页没有读到笔记 userId=%s（可能没有笔记，或接口未触发）", user_id)
    return list(merged.values())


def enrich_items(page, items: list[dict], *,
                 throttle: Callable[[], None] | None = None,
                 on_item: Callable[[int, int, dict], None] | None = None) -> dict:
    """给每个达人补 `likeCollectCountInfo` 与 `homepageLikeMedian`。

    单个达人失败**不拖垮整轮**：记 warning、留空、继续下一个人；
    但登录失效（401）与详情页人机验证会**立即停下**，因为这两种情况下继续
    采集只会得到一份大面积空白的名单。
    """
    stats = {
        "total": len(items),
        "detail_ok": 0,
        "detail_failed": 0,
        "notes_ok": 0,
        "notes_failed": 0,
        "notes_cached": 0,
        "missing_id": 0,
    }
    if not items:
        return stats

    # 补笔记数据用**自己那条节流器**，不跟翻页检索抢同一条节奏。
    throttle = throttle or notes_throttle
    cache = load_cache()
    pause_desc = []
    if PAUSE_EVERY:
        pause_desc.append(f"每 {PAUSE_EVERY} 人停 {PAUSE_MIN:.0f}–{PAUSE_MAX:.0f} 秒")
    if LONG_PAUSE_EVERY:
        pause_desc.append(f"每 {LONG_PAUSE_EVERY} 人长休 {LONG_PAUSE_MIN:.0f}–{LONG_PAUSE_MAX:.0f} 秒")
    throttle_desc = ("无额外间隔（按用户要求直接下一个）"
                     if ENRICH_INTERVAL <= 0 and ENRICH_JITTER <= 0
                     else f"{ENRICH_INTERVAL:.1f}–{ENRICH_INTERVAL + ENRICH_JITTER:.1f} 秒/次")
    log.info("补充笔记数据：%s 人；节流 %s；%s；笔记缓存 %s 天"
             "（命中则跳过详情页）。全部在蒲公英域内完成，不访问小红书。"
             "遇到安全验证会停下等人处理（可用 PGY_ENRICH_* / PGY_NOTES_CACHE_DAYS 调整）",
             len(items), throttle_desc,
             "、".join(pause_desc) if pause_desc else "无额外停顿",
             CACHE_DAYS)

    warmup = random.uniform(WARMUP_MIN, WARMUP_MAX)
    if warmup > 0:
        log.info("开始补充笔记数据前先等待 %.1f 秒…", warmup)
        time.sleep(warmup)

    context = page.context
    detail_page = open_detail_page(context)
    try:
        for index, item in enumerate(items, start=1):
            user_id = str(item.get("userId") or item.get("redId") or "").strip()
            if not user_id:
                stats["missing_id"] += 1
                log.warning("达人没有 userId，跳过笔记数据：%r", item.get("name"))
                continue

            # ① 蒲公英详情：赞藏 + 与 UI 一致的名称/粉丝/地区/报价/标签
            if throttle is not None:
                throttle()
            try:
                detail = fetch_detail(page, user_id)
            except DetailUnauthorized:
                raise
            except Exception:
                log.warning("详情接口异常 userId=%s（继续下一位）", user_id, exc_info=True)
                detail = None
            if detail:
                stats["detail_ok"] += 1
                for key in ("name", "fansNum", "likeCollectCountInfo", "location",
                            "picturePrice", "videoPrice", "contentTags", "featureTags",
                            "tradeType", "redId", "userId"):
                    if detail.get(key) is not None:
                        item[key] = detail[key]
            else:
                stats["detail_failed"] += 1

            # ② 蒲公英详情页：最近 10 篇笔记的点赞中位数
            #    先在本地缓存里找：同一个人 3 天内算过就不再打开详情页。
            cached, median = cache_get(cache, user_id)
            if cached:
                item["homepageLikeMedian"] = median
                stats["notes_cached"] += 1
                log.info("笔记中位数命中缓存 userId=%s = %s", user_id, median)
            else:
                if throttle is not None:
                    throttle()
                try:
                    notes = read_recent_notes(detail_page, user_id)
                    median = recent_note_like_median(notes)
                    item["homepageLikeMedian"] = median
                    if median is None:
                        stats["notes_failed"] += 1
                        log.warning("没有读到可用的点赞数 userId=%s（%s 篇）",
                                    user_id, len(notes))
                    else:
                        stats["notes_ok"] += 1
                    # 读到的结果（包括"没有笔记"）都记下来；下次不用再开详情页。
                    cache_put(cache, user_id, median)
                    save_cache(cache)
                except (NotesBlocked, DetailUnauthorized):
                    # 风控/掉登录现场不写缓存，也不继续刷。
                    raise
                except Exception as exc:
                    stats["notes_failed"] += 1
                    log.warning("读取详情页笔记失败 userId=%s：%s", user_id,
                                str(exc).splitlines()[0][:160])

            if on_item is not None:
                on_item(index, len(items), item)

            # 每 N 人停一会儿：连续打开大量详情页本身也是个机器人特征。
            # 默认不做额外停顿（用户 2026-11-10：「直接一股脑往下」）；
            # 若显式配置了 PGY_ENRICH_PAUSE_EVERY / LONG_PAUSE_EVERY，长休覆盖短休。
            pause = 0.0
            if PAUSE_EVERY and index < len(items) and index % PAUSE_EVERY == 0:
                pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
            if LONG_PAUSE_EVERY and index < len(items) and index % LONG_PAUSE_EVERY == 0:
                pause = max(pause, random.uniform(LONG_PAUSE_MIN, LONG_PAUSE_MAX))
            if pause > 0:
                log.info("已补充 %s/%s 人；按拟人节奏休息 %.1f 秒…",
                         index, len(items), pause)
                time.sleep(pause)
    finally:
        close_detail_page(detail_page)
    return stats
