"""Pgy 本地能力的 MCP（Model Context Protocol）封套。

这一层**不碰浏览器、不碰账号库、不碰凭据**，只做一件事：把 Pgy 客户端已经
暴露在 127.0.0.1 上的本地 HTTP 接口（见 `backend/app.py`）翻译成 MCP 工具，
让 WorkBuddy 之类的外部 Agent 能以语义指令驱动 Pgy。

在整套工具里的层次位置：

    backend/app.py        本地 FastAPI：浏览器动作白名单、会话状态机、运行日志
    pgy_mcp/*            本包，只做「HTTP ⇄ MCP 工具」的翻译
    WorkBuddy             外部 Agent，作为 MCP Client 调用这里的工具（任何支持
                           stdio MCP 的客户端都可以接）

刻意只用标准库（json / urllib / sys），理由：
- 打包成 onefile exe 后体积小、启动快——WorkBuddy 每开一个新会话都会把它拉起来；
- 不引入与解释器不同源的二进制依赖。本项目被 PATH 里混进外来 libcrypto 导致
  sidecar 启动即 ImportError 的坑坑过一次（见 build/build-sidecar.ps1 的 PATH 收敛），
  少一个第三方包就少一次这种事故。
"""

__version__ = "1.2.5"

# MCP 握手时回报给客户端的服务名。WorkBuddy 的连接器列表里会显示它。
SERVER_NAME = "pgy"
