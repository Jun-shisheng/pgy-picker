"""定位正在运行的 Pgy 本地服务。

后端只监听 127.0.0.1，端口默认 18081、可被 `PGY_PORT` 覆盖。端口是**约定的**
而不是被分配的，所以「连上了」不等于「连对人了」——18081 完全可能被别的程序
（甚至另一个登录用户的本程序）占着。客户端的 Rust 侧已经踩过这个坑并立了
`probe_backend_identity`，这里沿用同一套判据：先握手，再确认对面确实是本程序。

刻意不从 `PGY_DATA_DIR` 里读端点记录：数据目录在开发态是项目根、装机态是
`%LOCALAPPDATA%\\Pgy`，两套不一样；而同一时刻只可能有一个 Pgy 在跑（端口
是独占的），所以「探测端口 + 验明身份」比「读配置文件」更少出错。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .errors import PgyError

# 后端默认端口，与 backend_sidecar.py 的 DEFAULT_PORT、Rust 侧 BACKEND_PORT 一致。
DEFAULT_PORT = 18081

# 握手只用来看「对面是不是我们的后端」，所以给得短：探测失败要快速回落。
PROBE_TIMEOUT_SECONDS = 2.0

# /api/health 的固定应答（backend/app.py:health）。用它验明身份，两个词都要在，
# 与 Rust 侧 probe_backend_identity 的判据保持一致（同时含 service 与 pgy）。
# 判据用 "pgy" 而不是泛泛的 "playwright"：机器上可能同时跑着别的浏览器自动化服务，
# 必须能区分，否则会对着别人的后端报错。
IDENTITY_MARKERS = ("service", "pgy")


class PgyNotRunning(PgyError):
    """本机没有可用的 Pgy 本地服务（客户端没开 / 端口被别人占着）。

    继承 PgyError 而不是 RuntimeError：工具层只认一种错误类型，否则「连不上」
    会以另一种异常逃到协议层，被兜底文案盖成「工具执行失败」，把真正的原因
    （客户端没开）藏起来。
    """

    def __init__(self, message: str):
        super().__init__(message, code="PGY_NOT_RUNNING", retryable=True)


def candidate_ports() -> list[int]:
    """按优先级给出要尝试的端口：环境变量 > 默认。"""
    ports: list[int] = []
    raw = os.environ.get("PGY_PORT", "").strip()
    if raw:
        try:
            ports.append(int(raw))
        except ValueError:
            pass
    if DEFAULT_PORT not in ports:
        ports.append(DEFAULT_PORT)
    return ports


def probe(port: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict | None:
    """探一次端口：是我们的后端就回健康信息，否则回 None。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # 不设 Origin 头：backend/app.py 的来源校验只拦「带了不被允许的 Origin」
        # 的请求，本机脚本本来就不该伪装成浏览器。
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    flat = raw.lower()
    if not all(marker in flat for marker in IDENTITY_MARKERS):
        # 端口通了，但答话的不是 Pgy（可能是别的程序，或另一个用户的服务）。
        return None
    return payload


def resolve(timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, int]:
    """返回 (base_url, port)；都探不到就抛 PgyNotRunning。

    抛出的文案是给**人**看的：多数情况下原因就是「Pgy 客户端没开着」。
    """
    for port in candidate_ports():
        if probe(port, timeout=timeout) is not None:
            return f"http://127.0.0.1:{port}", port
    raise PgyNotRunning(
        "连不上 Pgy 本地服务。请确认：1) Pgy 客户端已经启动（可以最小化，但不要退出进程，"
        "退出客户端会一起关掉本地服务）；2) 若改过端口，设置 PGY_PORT 环境变量与客户端一致。"
    )
