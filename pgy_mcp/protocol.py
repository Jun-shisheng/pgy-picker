"""MCP 的 stdio 传输与 JSON-RPC 分发（**手写，不依赖官方 SDK**）。

为什么手写：本服务只暴露 tools 一种能力，协议面很窄；而官方 SDK 会带进
pydantic/anyio/httpx 等一串依赖，让 onefile exe 明显变大、启动变慢
（WorkBuddy 每开一个新会话都会重新拉起这个进程），也把打包时依赖错配的
风险引进来。手写的代价是必须把协议本身钉死在用例里——见
tests/test_pgy_mcp.py 的协议用例。

传输细节（这两条错了就是「连不上」，而且现象很难查）：
- **一行一条 JSON**（不是 LSP 那种 Content-Length 分帧），UT 编码；
- **stdout 只能出现协议消息**。任何调试输出、print、库的 warning 混进
  stdout 都会让客户端解析失败，所以本包所有诊断信息一律走 stderr。

协议版本：客户端在 initialize 里报它支持的版本；我们认识就照抄回去，
不认识就回自己最新的。这样新旧客户端都能握手成功。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable

from . import __version__, SERVER_NAME

# 我们认识并愿意应答的协议版本（由新到旧）。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    """诊断输出。**必须走 stderr**：stdout 是协议通道，混进一行字就断连。"""
    try:
        sys.stderr.write(f"[pgy-mcp] {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def negotiate_protocol_version(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def read_message(stream) -> dict | None:
    """从二进制流读一条消息；返回 None 表示流已结束（客户端退出）。

    跳过空行与无法解析的行（记一行 stderr）——为了一行坏数据就让整个会话死掉
    是最糟的取舍：用户看到的是「连上了但工具全不可用」。
    """
    while True:
        raw = stream.readline()
        if not raw:
            return None
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            log(f"收到无法解析的行（已跳过）：{exc}")
            continue
        if not isinstance(message, dict):
            log("收到非对象的 JSON（已跳过）")
            continue
        return message


def write_message(stream, message: dict) -> None:
    """写一条消息：单行 JSON + 立即 flush（不 flush 就是「发出去没回音」）。"""
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(payload + b"\n")
    stream.flush()


def ok(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    payload: dict = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": payload}


def tool_error(text: str) -> list[dict]:
    """工具内的业务失败：按 MCP 规范回**成功的结果** + isError。

    不要把它做成 JSON-RPC error：那样多数客户端会把整轮对话打断，用户只看到
    「工具调用失败」，看不到后端写下的那句「为什么失败」。我们宁可让模型读到
    原文，再如实转述给人。
    """
    return [{"type": "text", "text": text}]


class Session:
    """一条 stdio 连接的会话状态。

    只保存握手结果——本服务不在对话之间保留任何业务状态，业务状态全在
    Pgy 后端（同一个账户、同一把操作锁），所以这里多存一份只会带来不一致。
    """

    def __init__(self, list_tools: Callable[[], Iterable[dict]],
                 call_tool: Callable[[str, dict], list[dict]], server_version: str = __version__):
        self._list_tools = list_tools
        self._call_tool = call_tool
        self.server_version = server_version
        self.initialized = False
        self.protocol_version = LATEST_PROTOCOL_VERSION

    # ---- 分发 -----------------------------------------------------------

    def handle(self, message: dict) -> dict | None:
        """处理一条消息；返回要写回的响应，通知类消息返回 None。"""
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if not isinstance(method, str):
            if is_notification:
                return None
            return error(request_id, INVALID_REQUEST, "缺少 method 字段")

        if method == "initialize":
            return ok(request_id, self._initialize(message.get("params") or {}))
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "ping":
            return ok(request_id, {})
        if method == "tools/list":
            return ok(request_id, {"tools": list(self._list_tools())})
        if method == "tools/call":
            return self._tools_call(request_id, message.get("params") or {})

        # 其它一律按规范回「方法不存在」。通知（没有 id）不回任何东西——
        # 给通知回响应是协议违规，会被严格的客户端判成错包。
        if is_notification:
            return None
        return error(request_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")

    def _initialize(self, params: dict) -> dict:
        self.protocol_version = negotiate_protocol_version(params.get("protocolVersion"))
        self.initialized = True
        return {
            "protocolVersion": self.protocol_version,
            # 只声明 tools：声明了却没有实现的能力，会让客户端调过来拿到 -32601。
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self.server_version},
            # 给模型读的一句话，说明这个服务是干什么的。
            "instructions": (
                "Pgy 蒲公英选号助手（小红书蒲公英达人筛选与导出）。这些工具用来在对话里"
                "查看并驱动本机正在运行的 Pgy 客户端：查状态、列行业、按行业启动一次"
                "达人检索并导出 Excel、轮询进度、触发登录。对平台只读，不做任何投放动作。"
                "浏览器窗口全程可见，人机验证必须由人到窗口上完成；一次只支持一个账户、一个操作。"
                "工具失败时请原样转述返回的中文，不要改写。"
                "交付名单的列以工具给出的 Excel 文件为准：回复里不要自行增删列，"
                "也不要把预览数据重排成另一张表。"
            ),
        }

    def _tools_call(self, request_id: Any, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return error(request_id, INVALID_PARAMS, "tools/call 缺少 name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return error(request_id, INVALID_PARAMS, "arguments 必须是对象")
        try:
            content = self._call_tool(name, arguments)
        except Exception as exc:
            # 业务失败与「工具自己崩了」在这里合并成工具结果：模型能读到原因，
            # 完整堆栈写 stderr 供排障，不污染对话。
            from .errors import PgyError

            if isinstance(exc, PgyError):
                text = exc.as_text()
            else:
                log(f"工具 {name} 抛出未预期异常：{type(exc).__name__}: {exc}")
                text = f"工具 {name} 执行失败（{type(exc).__name__}）。详情见 Pgy 客户端「设置 → 运行日志」。"
            return ok(request_id, {"content": tool_error(text), "isError": True})
        return ok(request_id, {"content": content, "isError": False})
