# Pgy MCP 服务（WorkBuddy 连接器）

把 Pgy 客户端已有的本地 HTTP 能力（`127.0.0.1:18081`）包装成
[MCP](https://modelcontextprotocol.io) 工具，让 WorkBuddy 之类的 Agent
能在对话里查看并驱动 Pgy。

**这一层不碰浏览器、不碰账号库、不碰凭据。** 浏览器还是 Pgy 客户端在开、在点；
本服务只把语义指令转给后端，再把结果翻译成模型能读的文本或图片。

```
WorkBuddy ──MCP(stdio)──► pgy-mcp.exe ──HTTP──► Pgy 本地服务 ──Playwright──► Edge（全程可见）
```

## 交付物（`release\` 下整个目录发给同事）

| 文件 | 说明 |
|---|---|
| `安装-pgy-mcp.cmd` | **同事双击这一个**：自动装 exe + 写好 WorkBuddy 配置 |
| `install-pgy-mcp.ps1` | 安装脚本本体（被上面那个 cmd 调用） |
| `pgy-mcp.exe` | 服务本体，**零第三方依赖**（只用标准库），约 8.5 MB |
| `mcp.json` | **手动配置用的模板**，只有「不想跑脚本、想自己配」的人才需要 |

### 为什么必须由脚本生成配置，而不是直接发一份 mcp.json

MCP 的 `command` 必须是**绝对路径**，而绝对路径里含用户名（`%USERPROFILE%`）、
盘符、以及每个人把文件放在哪。发一份写死路径的 json，在别人机器上一定是错的——
而且 WorkBuddy 只会显示「连接失败」，**完全不提示是路径问题**，这是最费时间的一类坑。
（用户 2026-10-09 提出：「同事的目录也是随机的 EXE」——正是这个问题。）

## 同事那台机器上的安装步骤（一步 + 两步收尾）

1. 把整个目录拷过去，**双击 `安装-pgy-mcp.cmd`**。
   脚本会：把 exe 装到 `%LOCALAPPDATA%\PgyMCP\pgy-mcp.exe`（用户目录，
   **不需要管理员**——`C:\PgyMCP` 那种位置普通用户建不了）；把配置**合并**进
   `%USERPROFILE%\.workbuddy\mcp.json`（先备份、保留其它连接器、写无 BOM 的 UTF-8）；
   最后跑一次自检并告诉你「现在能不能用」。
2. 打开 WorkBuddy → 左侧「专家·技能·连接器」→「连接器」→ 找到 `pgy`
   → 点 **「信任」**（不点会一直显示失败）→ **重启当前会话或新开会话**（工具才会注入）。
3. 展开 `pgy` 确认工具数是 **7/7**。

**使用前提：Pgy 客户端必须开着**（可以最小化，但不要退出进程——本地服务是它的
子进程，退出客户端就会释放 18081，MCP 随即全部失联）。

### 手动配置（不跑脚本时）

把 `install-pgy-mcp.ps1` 装的 exe 放到任意位置，然后在
`%USERPROFILE%\.workbuddy\mcp.json` 里合并（**不要整文件覆盖**，别把已有的连接器挤掉；
JSON 不支持注释，注意不要写成 `.mcp.json`）：

```json
{ "mcpServers": { "pgy": { "command": "C:\\你的实际路径\\pgy-mcp.exe", "args": [] } } }
```

### 自检（不经过 WorkBuddy，直接验证「能不能连上」）

```
"%LOCALAPPDATA%\PgyMCP\pgy-mcp.exe" --self-test
```

成功会打印已连上的地址、当前账户与连接状态、以及 7 个工具名。
失败会直接说明是「Pgy 客户端没开」还是别的原因。

## P0 工具清单（全部只读）

| 工具 | 用途 |
|---|---|
| `pgy_status` | 当前账户、平台连接、执行器是否空闲、是否人工接管、能否发起新操作 |
| `pgy_progress` | 当前任务实时进度（第几条/共几条、百分比、阶段） |
| `pgy_list_accounts` | 本机已添加的账户（含 id） |
| `pgy_list_deliveries` | 投放留痕（投了什么、投成功没有） |
| `pgy_list_subaccounts` | 平台上的子账户列表（驱动浏览器读取） |
| `pgy_list_plans` | 当前子账户下的推广计划列表（驱动浏览器读取） |
| `pgy_preview` | **截取当前页面画面并返回图片**（密码框已打码）——Agent 在对话里「看见现场」的唯一途径 |

刻意不做的事：**账号的增删改**（`POST/PATCH/DELETE /api/accounts`、退出登录）
不开放给对话——那些接口要传密码，密码不进大模型上下文。账号在 Pgy 客户端里加一次即可。

## 环境变量（一般不用设）

| 变量 | 作用 |
|---|---|
| `PGY_PORT` | 本地服务端口；只有客户端改过端口时才需要在 `mcp.json` 的 `env` 里同步 |

本服务**不读** `PGY_DATA_DIR`：同一时刻只可能有一个 Pgy 在跑（端口独占），
所以「探测端口 + 用 `/api/health` 验明身份」比读配置文件更少出错。

## 开发与出包

```powershell
# 跑用例（本文件不依赖第三方包，无 pytest 也能自检）
PYTHONPATH=. python tests/test_pgy_mcp.py
# 或：python -m pytest tests/test_pgy_mcp.py

# 出包（产物：release\ 下的 pgy-mcp.exe + 安装脚本 + mcp.json 模板；
#       binaries 下带 target triple 的那份留给安装包用）
powershell -NoProfile -ExecutionPolicy Bypass -File build\build-mcp.ps1
# 用指定解释器（PATH 上的 python 没装 PyInstaller 时）：
powershell -NoProfile -ExecutionPolicy Bypass -File build\build-mcp.ps1 -Python "D:\venv\Scripts\python.exe"

# 装到本机自测（临时路径，不碰自己的 .workbuddy）
powershell -NoProfile -ExecutionPolicy Bypass -File pgy_mcp\installer\install-pgy-mcp.ps1 `
  -InstallDir "$env:TEMP\PgyMCP试装" -ConfigPath "$env:TEMP\试装-mcp.json"
```

打包时必须走**控制台子系统**（PyInstaller 默认，不要加 `--noconsole`）：
本程序是 stdio 协议服务，stdout 就是协议通道；GUI 子系统在拿不到有效 stdout
时会把 `sys.stdout` 置成 `None`，工具一个也说不了话。
被 WorkBuddy 拉起时父进程接了管道，控制台子系统**不会**弹窗口。

## 排查

| 现象 | 原因 |
|---|---|
| WorkBuddy 里 `pgy` 一直显示失败 | 还没点「信任」；或没重启会话 |
| 工具调用报「连不上 Pgy 本地服务」 | Pgy 客户端没开（或退出了进程） |
| 协议版本被拒 / 连不上但 exe 自检正常 | 客户端要求的协议版本比本服务新。本服务支持 `2025-06-18 / 2025-03-26 / 2024-11-05`，不认识的新版本会回退到 `2025-06-18`。若客户端较真，把它的版本号加进 `pgy_mcp/protocol.py` 的 `SUPPORTED_PROTOCOL_VERSIONS` 即可 |
| 点了工具但报 409 | 同一个 Pgy 只能有一个操作在跑：可能用户正在客户端里操作，或上一次操作还没结束（用 `pgy_status` 看 `executor_status`） |

## 已知边界（P1/P2 待办）

- 尚无写操作工具（选子账户、进入子账户、填笔记、一键投放）——P1。
- 长任务（一键投放动辄 4 分钟以上）需要拆成「启动 + 轮询」，不能一次调用挂到底——P1。
- 本地 HTTP 接口目前**无鉴权**（只靠 Origin 白名单）。`pgy-mcp` 已预留 `X-Pgy-Token`
  请求头与端点发现逻辑，等后端加上校验即可生效——P2。
- 人工接管闭环（验证码/滑块后恢复）需要 `pgy_resume` / `pgy_abandon`——P1。
