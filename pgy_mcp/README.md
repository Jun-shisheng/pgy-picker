# Pgy MCP 服务（连接器）

把 PgyPicker 客户端已有的本地 HTTP 能力（`127.0.0.1:18081`）包装成
[MCP](https://modelcontextprotocol.io) 工具，让支持 stdio MCP 的 Agent
（WorkBuddy、Claude Desktop 等）能在对话里查看并驱动 PgyPicker。

**这一层不碰浏览器、不碰账号库、不碰凭据。** 浏览器由 PgyPicker 客户端打开和操作；
本服务只把语义指令转给后端，再把结果翻译成模型能读的文本。

```
MCP Client ──MCP(stdio)──► pgy-mcp.exe ──HTTP──► PgyPicker 本地服务 ──Playwright──► Edge（全程可见）
```

**本服务对蒲公英只读**：只做达人检索、采集与导出，不发起邀约、不建合作、不投放。

## 交付物（`release\` 下整个目录发给使用者）

| 文件 | 说明 |
|---|---|
| `安装-pgy-mcp.cmd` | **双击这一个**：自动装 exe + 写好 MCP 客户端配置 |
| `install-pgy-mcp.ps1` | 安装脚本本体（被上面那个 cmd 调用） |
| `pgy-mcp.exe` | 服务本体，零第三方依赖（只用标准库），约 8.5 MB |
| `mcp.json` | **手动配置用的模板**，只有「不想跑脚本、想自己配」的人才需要 |

### 为什么必须由脚本生成配置，而不是直接发一份 mcp.json

MCP 的 `command` 必须是**绝对路径**，而绝对路径里含用户名（`%USERPROFILE%`）、
盘符、以及每个人把文件放在哪。发一份写死路径的 json，在别人机器上一定是错的——
而且 MCP 客户端只会显示「连接失败」，**完全不提示是路径问题**。

## 安装步骤（一步 + 两步收尾）

1. 把整个目录拷过去，**双击 `安装-pgy-mcp.cmd`**。
   脚本会：把 exe 装到 `%LOCALAPPDATA%\PgyMCP\pgy-mcp.exe`（用户目录，
   **不需要管理员**）；把配置**合并**进 MCP 客户端的 `mcp.json`
   （先备份、保留其它连接器、写无 BOM 的 UTF-8）；最后跑一次自检并告诉你「现在能不能用」。
2. 打开 MCP 客户端，找到自定义连接器 / MCP 服务器页面，找到 `pgy` → 点 **「信任」**
   → **重启当前会话或新开会话**（工具才会注入）。
3. 展开 `pgy` 确认工具数是 **5/5**。

**使用前提：PgyPicker 客户端必须开着**（可以最小化，但不要退出进程——本地服务是它的
子进程，退出客户端就会释放 18081，MCP 随即全部失联）。

### 手动配置（不跑脚本时）

把 `pgy-mcp.exe` 放到任意位置，然后在客户端的 MCP 配置里合并（**不要整文件覆盖**）：

```json
{
  "mcpServers": {
    "pgy": {
      "type": "stdio",
      "command": "C:\\你的实际路径\\pgy-mcp.exe",
      "args": []
    }
  }
}
```

> JSON 不支持注释；反斜杠要写两个。`type` 必须是 `stdio`。

### 自检（不经过 MCP 客户端，直接验证「能不能连上」）

```
"%LOCALAPPDATA%\PgyMCP\pgy-mcp.exe" --self-test
```

成功会打印已连上的地址、当前账户与连接状态、以及 5 个工具名。
失败会直接说明是「PgyPicker 客户端没开」还是别的原因。

## 工具清单（全部只读）

| 工具 | 用途 |
|---|---|
| `pgy_status` | 本地服务、账号、平台登录态、执行器是否空闲、能否发起新操作 |
| `pgy_list_industries` | 列出蒲公英一级 / 二级行业（检索要的是**行业 ID**，不是名称） |
| `pgy_start_selection` | **启动**一次达人检索与导出，立即返回；之后用进度工具轮询 |
| `pgy_selection_progress` | 当前阶段、翻了第几页、各量级已取多少条、交付 Excel 路径 |
| `pgy_login` | 触发一次登录（打开可见浏览器窗口；验证码由人完成） |

刻意不做的事：**账号的增删改**不开放给对话——那些接口要传密码，密码不进大模型上下文。
账号在 PgyPicker 客户端里加一次即可。

## 环境变量（一般不用设）

| 变量 | 作用 |
|---|---|
| `PGY_PORT` | 本地服务端口；只有客户端改过端口时才需要在 `mcp.json` 的 `env` 里同步 |

本服务**不读** `PGY_DATA_DIR`：同一时刻只可能有一个 PgyPicker 在跑（端口独占），
所以「探测端口 + 用 `/api/health` 验明身份」比读配置文件更少出错。

## 开发与出包

```powershell
# 跑用例（本文件不依赖第三方包，无 pytest 也能自检）
$env:PYTHONPATH="."; python tests/test_pgy_mcp.py

# 出包（产物：release\ 下的 pgy-mcp.exe + 安装脚本 + mcp.json 模板）
powershell -NoProfile -ExecutionPolicy Bypass -File build\build-mcp.ps1
```

打包时必须走**控制台子系统**（PyInstaller 默认，不要加 `--noconsole`）：
本程序是 stdio 协议服务，stdout 就是协议通道；GUI 子系统在拿不到有效 stdout
时会把 `sys.stdout` 置成 `None`，工具一个也说不了话。

## 排查

| 现象 | 原因 |
|---|---|
| MCP 客户端里 `pgy` 一直显示失败 | 还没点「信任」；或没重启会话 |
| 工具调用报「连不上 PgyPicker 本地服务」 | 客户端没开（或退出了进程） |
| 协议版本被拒 / 连不上但 exe 自检正常 | 客户端要求的协议版本比本服务新。本服务支持 `2025-06-18 / 2025-03-26 / 2024-11-05`，不认识的新版本会回退到 `2025-06-18` |
| 点了工具但报 409 | 同一时刻只允许一个操作：可能用户正在客户端里操作，或上一次操作还没结束（用 `pgy_status` 看 `executor_status`） |
| 列表里完全找不到 `pgy` | 检查配置里有 `"type": "stdio"`；完全退出客户端再重开（配置在启动时读取） |

## 已知边界

- 本地 HTTP 接口目前**无鉴权**（只靠 Origin 白名单）。`pgy-mcp` 已预留 `X-Pgy-Token`
  请求头与端点发现逻辑，等后端加上校验即可生效。
- 长任务拆成「启动 + 轮询」两步，避免一次调用挂到底。
- 人工接管闭环（验证码 / 滑块后恢复）仍在完善中。
