"""访问 Pgy 本地 HTTP 接口的薄客户端。

只做三件事：拼 URL、把错误码翻译成可读的异常、在服务重启后重新探测端口。
**不做任何业务判断**——流程语义全在 backend 里，这里猜就是重复实现。

超时口径与后端一致（backend/providers/spotlight/mio_partner.py 的
CALL_TIMEOUT_SECONDS / LONG_ACTIONS）：查询类动作后端 300 秒快速失败线，
纯本地读取（不碰浏览器）几十毫秒就回。客户端比后端多留 10 秒，避免两边
掐着同一个数字互相超时。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import discovery
from .errors import PgyError

__all__ = ["PgyClient", "PgyError", "BROWSER_READ_TIMEOUT_SECONDS", "LOCAL_READ_TIMEOUT_SECONDS"]

# 后端对「碰浏览器的非长任务」的快速失败线是 300 秒，客户端留 10 秒余量。
BROWSER_READ_TIMEOUT_SECONDS = 310.0
# 纯本地读取（会话快照、账号列表、留痕）：不碰浏览器，几十毫秒级。
LOCAL_READ_TIMEOUT_SECONDS = 30.0


class PgyClient:
    """一个进程一份；port 由构造时或首次调用时探测确定。"""

    def __init__(self, base_url: str | None = None, port: int = 0, token: str = ""):
        self.base_url = base_url
        self.port = port
        # token 是 P2 的加固项，当前后端不校验；先留着，届时只改后端与这里。
        self.token = token

    # ---- 连接管理 -------------------------------------------------------

    def ensure(self) -> str:
        """确保有一个可用的 base_url；没有就现在探。"""
        if self.base_url:
            return self.base_url
        self.base_url, self.port = discovery.resolve()
        return self.base_url

    def forget(self) -> None:
        """丢掉缓存的地址：服务可能换过端口、或刚被重启。"""
        self.base_url = None
        self.port = 0

    # ---- 请求 -----------------------------------------------------------

    def get(self, path: str, timeout: float = LOCAL_READ_TIMEOUT_SECONDS) -> dict:
        # GET 一律可重放：后端的所有 GET 都是只读快照，重来一次不会改变任何东西。
        return self.request("GET", path, timeout=timeout, retry_safe=True)

    def post(self, path: str, body: dict | None = None,
             timeout: float = BROWSER_READ_TIMEOUT_SECONDS,
             retry_safe: bool = False) -> dict:
        return self.request("POST", path, body=body, timeout=timeout, retry_safe=retry_safe)

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float = LOCAL_READ_TIMEOUT_SECONDS,
                retry_safe: bool = False) -> dict:
        """发一次请求。连接层失败（服务重启/端口换了）时最多重探一次。

        `retry_safe` 只对**重复执行无害**的调用为 True（读、查询、截图）。
        写操作（真的会建计划/投创意）绝不重放——后端自己也立了同样的规矩
        （app.py 里 `create_and_publish` 不走「窗口没了就静默重试」的自愈链路），
        重放的代价是平台上多投一次。
        """
        try:
            return self._send(method, path, body, timeout)
        except _TransportDown:
            if not retry_safe:
                raise PgyError(
                    "本地服务连接中断。若 Pgy 客户端刚被重启，请重试；"
                    "写操作不会自动重放，避免重复投放。",
                    code="TRANSPORT_DOWN", status=0, retryable=True) from None
            self.forget()
            try:
                return self._send(method, path, body, timeout)
            except _TransportDown:
                raise PgyError(
                    "本地服务连接中断。请确认 Pgy 客户端仍在运行后重试。",
                    code="TRANSPORT_DOWN", status=0, retryable=True) from None

    def _send(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        base = self.ensure()
        url = f"{base}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Pgy-Token"] = self.token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except (urllib.error.URLError, OSError) as exc:
            raise _TransportDown(str(exc)) from None
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return payload if isinstance(payload, dict) else {"items": payload}

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> PgyError:
        """把后端的错误信封翻成 PgyError。

        后端所有 4xx/5xx 都是 `{"detail", "code", "retryable", "operation_id"}`
        （见 app.py 的 OperationHttpError 处理），detail 已经是写给人看的中文，
        直接透传，不要在这里二次加工——重写文案会让排障时对不上日志。
        """
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        detail, code, retryable, operation_id = "", "", False, ""
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or "")
                code = str(payload.get("code") or "")
                retryable = bool(payload.get("retryable"))
                operation_id = str(payload.get("operation_id") or "")
        except json.JSONDecodeError:
            detail = raw.strip()
        if not detail:
            detail = f"本地服务返回 HTTP {exc.code}"
        return PgyError(detail, code=code, status=int(exc.code or 0),
                         retryable=retryable, operation_id=operation_id)


class _TransportDown(Exception):
    """内部信号：连接层失败，交给 request() 决定能不能重探。"""
