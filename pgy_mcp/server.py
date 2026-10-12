"""MCP 服务主循环：连一条 stdio 通道，把工具调用转给 Pgy 本地接口。

启动方式（由 WorkBuddy 按 mcp.json 拉起，不需要人工运行）：

    {"mcpServers": {"pgy": {"command": "C:\\\\PgyMCP\\\\pgy-mcp.exe"}}}

命令行参数（只为人工排障，不参与正常协议流程）：
    --self-test    连一次 Pgy 本地服务并打印状态，验证「能不能用」
    --list-tools   打印本服务暴露的工具清单
    --version      打印版本

关于打包形态：**必须按控制台子系统打包（PyInstaller 默认，不要加 --noconsole）**。
被 WorkBuddy 拉起时父进程会把 stdin/stdout 接成管道，控制台子系统 + 管道重定向
不会弹任何窗口；而 --noconsole 会让解释器在拿不到有效 stdout 时把 sys.stdout
置成 None，协议直接没法说话。手工双击运行时能看到的那个黑框是排障福利，不是缺陷。
"""

from __future__ import annotations

import sys

from . import __version__, SERVER_NAME
from .client import PgyClient, PgyError
from .protocol import Session, log, read_message, write_message
from .tools import TOOLS, call_tool


def _binary_stdin():
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise RuntimeError(
            "stdin 不可用：本进程必须以 stdio 方式由 MCP 客户端拉起（或带 --self-test 人工运行）。"
        )
    return stream


def _binary_stdout():
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        raise RuntimeError(
            "stdout 不可用：打包时用了 --noconsole（GUI 子系统）导致协议通道缺失，"
            "请改回控制台子系统重新打包。"
        )
    return stream


def build_session(client: PgyClient) -> Session:
    def list_tools():
        return TOOLS

    def call(name: str, arguments: dict):
        return call_tool(client, name, arguments)

    return Session(list_tools=list_tools, call_tool=call, server_version=__version__)


def serve() -> int:
    stdin = _binary_stdin()
    stdout = _binary_stdout()
    client = PgyClient()
    session = build_session(client)
    log(f"{SERVER_NAME}-mcp {__version__} 已启动，等待客户端握手…")
    while True:
        message = read_message(stdin)
        if message is None:
            log("stdin 已关闭，退出。")
            return 0
        try:
            response = session.handle(message)
        except Exception as exc:  # 分发层自身的意外：记下来，但不让会话死掉
            log(f"处理消息时发生意外：{type(exc).__name__}: {exc}")
            continue
        if response is not None:
            write_message(stdout, response)


def self_test() -> int:
    """人工自检：不经过 WorkBuddy，直接验证「能不能连上 Pgy」。"""
    from . import discovery

    print(f"{SERVER_NAME}-mcp {__version__}")
    try:
        base_url, port = discovery.resolve()
    except discovery.PgyNotRunning as exc:
        print(f"[失败] {exc}")
        return 1
    print(f"[成功] 已连上 Pgy 本地服务：{base_url}（端口 {port}）")
    client = PgyClient(base_url=base_url, port=port)
    # ⚠️ 状态读 `/api/selection/status`（真实登录态），**不是 `/api/session`** ——
    # 后者在当前后端里是硬编码死桩，永远回「未连接」，拿它自检等于自欺。
    try:
        status = client.get("/api/selection/status")
    except PgyError as exc:
        print(f"[失败] 读取登录状态出错：{exc.as_text()}")
        return 1
    has_credentials = "已配置" if status.get("has_credentials") else "未配置"
    logged_in = "已登录" if status.get("logged_in") else "未登录"
    print(f"[成功] 状态可读：账号凭证={has_credentials}，平台登录={logged_in}，"
          f"身份={status.get('role') or '—'}")
    print(f"[信息] 本服务对外暴露 {len(TOOLS)} 个工具：")
    for tool in TOOLS:
        print(f"  - {tool['name']}")
    print("一切就绪：把 mcp.json 配好并在 WorkBuddy 里点「信任」即可。")
    return 0


def list_tools() -> int:
    for tool in TOOLS:
        print(f"{tool['name']}\n    {tool['description']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(f"{SERVER_NAME}-mcp {__version__}")
        return 0
    if "--self-test" in args:
        return self_test()
    if "--list-tools" in args:
        return list_tools()
    try:
        return serve()
    except RuntimeError as exc:
        log(str(exc))
        return 2
    except KeyboardInterrupt:
        return 0
