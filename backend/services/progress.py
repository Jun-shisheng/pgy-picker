"""选号过程的进度快照。

**为什么需要**：`POST /api/selection/run` 是同步接口（几分钟才返回），中间没有任何
状态出口 —— 界面只能转一个按钮，用户看不到「进行到哪一步、是不是卡住了、是不是已经
废了」。更糟的是采集**不驱动界面**（在页面上下文里直接调平台接口，见 `search.py`），
所以那个可见的浏览器窗口在翻页期间**一动不动**，看上去就跟死了一样。

这里把过程写进一块**进程内**的状态，由 `GET /api/selection/progress` 读走。

**不落盘、不记日志**：这是给界面看的瞬时状态，进程重启即归零；写日志只会把
运行日志冲成一片噪声（日志是留给事后排障的，不是给界面当数据源的）。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

# 阶段取值。界面按这个画步骤条，所以**增删要同步改前端**。
IDLE = "idle"
LOGIN = "login"
SEARCH = "search"
EXPORT = "export"
DONE = "done"
ERROR = "error"

# 给界面直接显示的中文标签（别让前端再维护一份映射）
STAGE_LABELS = {
    IDLE: "空闲",
    LOGIN: "登录平台",
    SEARCH: "检索达人",
    EXPORT: "生成 Excel",
    DONE: "已完成",
    ERROR: "已中断",
}

_lock = threading.Lock()
_state: dict[str, Any] = {}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _reset(keep_started: bool = False) -> None:
    started = _state.get("started_at", "") if keep_started else _now()
    _state.clear()
    _state.update({
        "stage": IDLE, "label": STAGE_LABELS[IDLE], "detail": "",
        "page": 0, "collected": 0, "total": 0, "per_tier_counts": {},
        "started_at": started, "updated_at": _now(),
        "active": False, "error": "",
        # 失败时用来回答「停在哪一步」——否则 stage 一变成 error，这个信息就没了
        "failed_stage": "", "failed_label": "",
        # 跑完之后的结果（产物路径、统计、行数据）。**必须有**：MCP 是「启动 + 查进度」
        # 两个工具，查进度那个得能把结果交出来，否则 Agent 永远拿不到表在哪。
        "result": {},
    })


_reset()


def begin(stage: str, detail: str = "") -> None:
    """进入某个阶段。第一次 begin 会重置并记下开始时间。"""
    with _lock:
        if not _state.get("active"):
            _reset()
        _state.update({
            "stage": stage, "label": STAGE_LABELS.get(stage, stage), "detail": detail,
            "active": True, "error": "", "updated_at": _now(),
        })


def update(**fields: Any) -> None:
    """更新计数类字段（页码、已取条数…）。"""
    with _lock:
        if not _state.get("active"):
            return
        _state.update(fields)
        _state["updated_at"] = _now()


def finish(result: dict | None = None) -> None:
    """跑完了。`result` 是这一次的产物（导出的表在哪、取了多少条），交给查进度的人。"""
    with _lock:
        _state.update({
            "stage": DONE, "label": STAGE_LABELS[DONE], "detail": "",
            "active": False, "error": "",
            "result": result or {},
            "updated_at": _now(),
        })


def fail(message: str) -> None:
    """失败：**把失败前所处的阶段单独记下来**，界面才能说清「停在哪一步」。"""
    with _lock:
        previous = _state.get("stage", IDLE)
        _state.update({
            "stage": ERROR, "label": STAGE_LABELS[ERROR],
            "detail": message, "active": False, "error": message,
            "failed_stage": "" if previous == ERROR else previous,
            "failed_label": "" if previous == ERROR else STAGE_LABELS.get(previous, ""),
            "updated_at": _now(),
        })


def reset() -> None:
    with _lock:
        _reset()


def snapshot() -> dict:
    with _lock:
        # 拷贝一份再返回：调用方（FastAPI 序列化）在锁外跑，别让它读到半更新状态。
        return {**_state,
                "per_tier_counts": dict(_state.get("per_tier_counts") or {}),
                "result": _state.get("result") or {}}
