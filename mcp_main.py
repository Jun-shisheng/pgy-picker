"""Pgy MCP 服务（WorkBuddy 连接器）的打包入口。

与 backend_sidecar.py 同一个套路：PyInstaller 的入口是一个**扁平脚本**，
不直接指向包内的 __main__.py——这样 onefile 打包时不会因为包内相对导入的
上下文差异而出问题。

产物：frontend/src-tauri/binaries/pgy-mcp-x86_64-pc-windows-msvc.exe
（由 build/build-mcp.ps1 生成，再由 build-installer.ps1 随客户端一起分发）

注意：**不要**用 --noconsole 打包。本程序是 stdio 协议服务，stdout 就是协议通道；
GUI 子系统拿不到有效 stdout 时 sys.stdout 会是 None，工具一个也说不了话。
"""
from __future__ import annotations

import sys

from pgy_mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
