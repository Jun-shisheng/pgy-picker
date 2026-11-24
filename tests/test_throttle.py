"""节流器的「空闲额度」不能攒着补发。

2026-11-10 实测事故：检索结束后闲置 50 秒再补主页数据，旧实现把这段空闲
算成"早该发"的额度，前 ~30 次调用一次不等，小红书当场触发安全验证。
"""

from __future__ import annotations

from backend.services import throttle as throttle_mod


def test_idle_time_is_not_accumulated_as_burst_credit(monkeypatch):
    clock = [1000.0]
    sleeps: list[float] = []

    monkeypatch.setattr(throttle_mod.time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(throttle_mod.time, "sleep", fake_sleep)

    limiter = throttle_mod.Throttle(min_interval=2.0, jitter=0.0)
    limiter()                     # 第一次：正常等 2 秒
    clock[0] += 100.0             # 闲置 100 秒
    limiter()                     # 关键：仍然要等 2 秒，不能把空闲当额度

    assert sleeps == [2.0, 2.0], f"空闲后仍然要按间隔等，实际 {sleeps}"


def test_interval_never_below_minimum(monkeypatch):
    clock = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(throttle_mod.time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(throttle_mod.time, "sleep", fake_sleep)

    limiter = throttle_mod.Throttle(min_interval=2.5, jitter=2.0)
    for _ in range(5):
        limiter()
        clock[0] += 0.1

    assert all(2.5 <= s <= 4.5 for s in sleeps), sleeps


def test_zero_interval_does_not_sleep(monkeypatch):
    """补主页按用户要求关掉间隔：0/0 时不应有任何 sleep。"""
    sleeps: list[float] = []
    monkeypatch.setattr(throttle_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    limiter = throttle_mod.Throttle(min_interval=0.0, jitter=0.0)
    limiter()
    limiter()
    limiter()

    assert sleeps == []
