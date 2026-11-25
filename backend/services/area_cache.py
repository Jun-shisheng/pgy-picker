"""地域枚举（省/市）的本地快照。

与 `industry_cache` 同一套路：平台的地域枚举只能在**有登录态的浏览器里**问出来
（`/api/solar/area/get_areas`，见 `selection_service._areas_inner`）。新机器还没登录时
选号页的地域下拉会是空的 —— 存一份快照，至少先把选项显示出来。

**刷新时机只有一个：活取成功时顺手写。** 不后台轮询、不为了刷缓存去启动浏览器
（项目设计约束 第二、三条）。

放在 `ROOT` 下，**必须在 `.gitignore` 里**（与行业缓存同理）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from backend.core.pgy_core import ROOT, _atomic_write

log = logging.getLogger("pgy.area_cache")

CACHE_FILE = ROOT / "areas-cache.json"
#: v2 = 带区县的完整树（`get_areas?type=2`）。旧版 v1 只到「市」，读到要丢弃重取。
VERSION = 2
SOURCE = "/api/solar/area/get_areas?type=2"


def load() -> list[dict] | None:
    """读缓存。文件不存在、内容坏掉、或空列表，一律返回 None。"""
    try:
        raw = CACHE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("地域缓存读取失败，按无缓存处理：%s", CACHE_FILE, exc_info=True)
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("地域缓存不是合法 JSON，按无缓存处理：%s", CACHE_FILE)
        return None
    if not isinstance(payload, dict) or int(payload.get("version") or 0) < VERSION:
        # 旧版只有省/市，没有区县 —— 宁可当作没有缓存，也别把残缺的下拉交出去。
        log.info("地域缓存版本过旧（%s），丢弃并重新获取", payload.get("version") if isinstance(payload, dict) else "?")
        return None
    items = payload.get("areas")
    if not isinstance(items, list) or not items:
        return None
    return items


def save(areas: list[dict]) -> None:
    """原子写一份快照。写不进去只记日志 —— 缓存失败绝不能影响本次检索。"""
    if not areas:
        return
    payload = {
        "version": VERSION,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "areas": areas,
    }
    try:
        _atomic_write(CACHE_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("已更新地域缓存（%s 个一级地区）", len(areas))
    except OSError:
        log.warning("地域缓存写入失败（不影响本次使用）：%s", CACHE_FILE, exc_info=True)
