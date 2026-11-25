"""统一节流器 —— 所有对平台的请求都必须穿过它。

为什么是**功能需求**而不是优化项：采集是长尾高频动作，比一次性投放更怕快。
裸发请求既容易触发风控，也让我们失去"这一刻发了多快"的可观测性。

设计：
- **最小间隔 + 随机抖动**：整齐的节奏本身就是可被识别的特征。
- **指数退避**：撞上限流时越退越久，而不是原速重试。
- **默认保守**：M1 的 T-03 阈值标定完成前，默认值必须"明显偏慢"。
"""

from __future__ import annotations

import logging
import random
import threading
import time

log = logging.getLogger("pgy.throttle")


class Throttle:
    """串行节流器。多线程共用一个实例也安全（内部加锁）。"""

    def __init__(self, min_interval: float = 1.5, jitter: float = 0.8,
                 max_interval: float = 60.0) -> None:
        # ⚠️ 默认值刻意保守。T-03 标定出安全阈值之前不要调快。
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_interval = max_interval
        self._interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self.calls = 0
        self.total_wait = 0.0

    def __call__(self) -> None:
        """等够间隔再返回。

        ⚠️ **不能把"空闲时间"当额度攒起来**（2026-11-10 实测踩到）：
        原实现是 `target = _last + interval + jitter`。检索结束后闲置 50 秒再去
        补主页数据时，前 ~30 次调用会认为"早该发了"，**连续裸发、一次不等** ——
        小红书就是在这个突发里触发了安全验证。现在用 `max(_last, now)`：
        空闲再久，下一次调用也至少等一个完整间隔，不会补发攒下的额度。
        """
        with self._lock:
            now = time.monotonic()
            target = (max(self._last, now)
                      + self._interval
                      + random.uniform(0, self.jitter))
            wait = max(0.0, target - now)
            self.calls += 1
            self.total_wait += wait
            self._last = target
        if wait > 0:
            time.sleep(wait)

    def backoff(self) -> float:
        """遇到限流/异常时调用：间隔翻倍（有上限），返回新间隔。"""
        with self._lock:
            self._interval = min(self.max_interval, max(self.min_interval, self._interval * 2))
            current = self._interval
        log.warning("节流退避：间隔上调到 %.1fs", current)
        return current

    def reset(self) -> None:
        """恢复默认间隔（一次成功的长序列之后调用）。"""
        with self._lock:
            self._interval = self.min_interval

    def snapshot(self) -> dict:
        return {"interval": round(self._interval, 2), "calls": self.calls,
                "total_wait": round(self.total_wait, 1)}


# 进程级默认实例：采集链路共用同一个，避免各处各配一套。
default = Throttle()
