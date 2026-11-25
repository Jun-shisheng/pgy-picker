"""行业枚举的本地快照。

**为什么需要它**：平台那套一级/二级行业枚举只能在**有登录态的浏览器里**问出来
（见 `selection_service._industries_inner`）。新机器还没登录时，选号页的下拉就是空的
——用户看到的就是一个只有「不限」的空框，不知道是自己错了还是程序坏了。
存一份快照，界面至少能先把选项显示出来。

**刷新时机只有一个：活取成功时顺手写。** 不后台轮询、不为了刷缓存去启动浏览器
——那会白白多打平台的接口（项目设计约束 第二、三条）。

放在 `ROOT` 下而不是数据目录：开发态 `ROOT` 就是仓库根，与 `settings.json` 同级。
⚠️ 因此它**必须在 `.gitignore` 里**，否则本机运行数据会被提交进仓库。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from backend.core.pgy_core import ROOT, _atomic_write

log = logging.getLogger("pgy.industry_cache")

CACHE_FILE = ROOT / "industries-cache.json"
VERSION = 1
SOURCE = "/api/pgy/kol/get_industry_tag"


def load() -> list[dict] | None:
    """读缓存。文件不存在、内容坏掉、或空列表，一律返回 None（按「没有缓存」处理）。

    读坏了不该让选号页崩 —— 缓存本来就是可再生的，丢掉重取即可。
    """
    try:
        raw = CACHE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("行业缓存读取失败，按无缓存处理：%s", CACHE_FILE, exc_info=True)
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("行业缓存不是合法 JSON，按无缓存处理：%s", CACHE_FILE)
        return None
    items = payload.get("industries") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return None
    return items


def save(industries: list[dict]) -> None:
    """原子写一份快照。写不进去只记日志 —— 缓存失败绝不能影响本次检索。"""
    if not industries:
        return
    payload = {
        "version": VERSION,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "industries": industries,
    }
    try:
        _atomic_write(CACHE_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("已更新行业缓存（%s 个一级行业）", len(industries))
    except OSError:
        log.warning("行业缓存写入失败（不影响本次使用）：%s", CACHE_FILE, exc_info=True)
