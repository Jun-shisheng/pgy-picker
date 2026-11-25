# PgyPicker · 蒲公英选号器

> 面向 **小红书蒲公英平台**（`pgy.xiaohongshu.com`）的 Windows 本地**达人筛选与导出**工具。
> 按行业、笔记关键词、地域、粉丝量级检索达人 → 采集交付字段 → 导出 Excel。
>
> **只读、节流、人工在回路**：不发起邀约、不建合作、不评论、不私信、不投放。

<p>
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows-0078D4">
  <img alt="stack" src="https://img.shields.io/badge/Tauri%202%20·%20React%2019%20·%20FastAPI%20·%20Playwright-2F6F4E">
</p>

> ⚠️ 本项目仅供学习与**经授权**的业务场景使用。使用前请先阅读 [合规与免责声明](#合规与免责声明)。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能](#功能)
- [刻意不做的事](#刻意不做的事)
- [架构](#架构)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [配置](#配置)
- [Excel 输出](#excel-输出)
- [MCP 连接器](#mcp-连接器)
- [目录结构](#目录结构)
- [开发与打包](#开发与打包)
- [合规与免责声明](#合规与免责声明)
- [贡献](#贡献)
- [许可证](#许可证)

---

## 它解决什么问题

投放团队在蒲公英上选达人，通常是**手工翻页 + 逐条记录 + 复制到 Excel**。人一多就会出现：

- 粉丝量级配不平：高量级在默认排序里很稀疏，翻半天凑不满；
- 报价靠人眼对表：不同粉丝档位的报价上限不同，逐条核对容易漏；
- 字段散落：昵称、粉丝、赞藏、类型、报价、主页链接要来回切页面抄；
- 重复劳动：每次换一批人，又得从头翻。

PgyPicker 把这条链路做死成一条**垂直采集流水线**：

```
检索需求（行业 / 关键词 / 地域 / 勾选的量级与每档条数）
   ↓
在蒲公英按「量级区间 + 检索条件」逐个问平台
   ↓
本地两道把关：粉丝档位 + 该档报价上限
   ↓
逐人补齐详情页字段（小红书号 / 赞藏 / 类型 / 报价 / 近 10 篇点赞中位数）
   ↓
导出 Excel（一人一行，10 列）
```

## 功能

| 能力 | 说明 |
|---|---|
| **行业检索** | 从平台拉取一级 / 二级行业枚举，按行业 ID 精确检索 |
| **笔记关键词** | 一个输入框随手写一串词，程序自动判断哪些是行业、哪些是笔记关键词，多词逐个搜再合并去重 |
| **量级配额** | 六个粉丝档位（1w–50w）可逐档勾选、逐档设条数（默认每档 20）；某档光靠词凑不满时，会按该档区间再翻一遍补满，并明确标注「不保证命中检索词」 |
| **地域筛选** | 国家 / 省 / 市 / 区四列级联多选（多选为「或」），本地再按完整路径严格核对 |
| **预算把关** | 平台报价上限 = 该粉丝档位的基准价 × 倍数（默认 1.3）；图文、视频任一在预算内即保留，超额的单元格加底色 |
| **翻页游标** | 默认「接着上次的位置往下取」，避免每次都拿到同一批人；也可显式从头取 |
| **详情补齐** | 逐人打开蒲公英详情页，读取小红书号、赞藏、类型标签、报价，以及「日常笔记 / 笔记案例」最新 10 篇的点赞中位数 |
| **Excel 导出** | 单表、一人一行、10 列，不带任何元信息表 |
| **MCP 连接器** | 把本地能力包装成 5 个只读工具，可在支持 stdio MCP 的 Agent 里用自然语言驱动 |

## 刻意不做的事

这不是「功能还没做」，而是**产品边界**：

1. **对平台只读。** 不发起合作邀约、不建合作、不评论、不私信、不投放，也不接受后续以开关形式加回来。
2. **只访问 `pgy.xiaohongshu.com`。** 近 10 篇点赞中位数也取自蒲公英详情页，**不访问 `www.xiaohongshu.com`**，不需要小红书账号或扫码。
3. **不绕过风控。** 登录、短信验证、人机验证一律由人在可见浏览器窗口里完成；不实现验证码识别、打码或任何伪装。
4. **不并发轰炸。** 所有平台请求都必须经过统一节流器（最小间隔 + 随机抖动 + 指数退避）。
5. **不明文保存凭据。** 账号密码只进 Windows 凭据管理器，不写入 SQLite / JSON / 日志 / 源码，任何接口都不返回密码。
6. **不做个人画像拼接。** 只采集平台已对资质账号可见的达人信息，不做平台未展示数据的推断。

## 架构

```
┌──────────────────────────────┐
│  Tauri 2 桌面壳（Rust）        │
│  ┌────────────────────────┐  │
│  │ React 19 前端（Vite）    │  │
│  └───────────┬────────────┘  │
└──────────────┼───────────────┘
               │ HTTP 127.0.0.1:18081
┌──────────────▼───────────────┐        ┌──────────────────────┐
│ pgy-backend.exe              │◄───────│ pgy-mcp.exe           │
│ FastAPI + uvicorn（sidecar）  │  HTTP  │ stdio MCP 薄客户端     │
│ 唯一业务内核                  │        └──────────▲───────────┘
└──────────────┬───────────────┘                   │ MCP
               │ Playwright / CDP                   │
┌──────────────▼───────────────┐        ┌──────────┴───────────┐
│ 系统 Edge（专属配置目录）       │        │ MCP Client            │
│ 全程可见，登录态复用           │        │ （WorkBuddy / Claude  │
└──────────────────────────────┘        │   Desktop / …）        │
                                        └──────────────────────┘
```

- **业务语义只存在于 `backend/`**。React 前端与 `pgy_mcp` 都是薄客户端。
- 后端只听 `127.0.0.1:18081`；`/api/health` 带固定身份标记，防「端口通了但连错人」。
- sidecar 带父进程守护：客户端退出后自动释放端口，但**不关浏览器、不动登录态**。

## 技术栈

| 层 | 选型 |
|---|---|
| 桌面壳 | Tauri 2（Rust）+ NSIS 安装包 |
| 前端 | React 19 + TypeScript + Vite + Tailwind + TanStack Query/Router |
| 业务内核 | Python + FastAPI + uvicorn（PyInstaller onefile sidecar） |
| 浏览器自动化 | Playwright（连接系统 Edge，专属配置目录） |
| 本地存储 | JSON 文件（设置 / 报价 / 游标 / 缓存） |
| Excel 渲染 | openpyxl |
| MCP 连接器 | Python stdlib（仅标准库，打包为 `pgy-mcp.exe`） |
| 测试 | pytest |

## 快速开始

### 环境要求

- Windows 10 / 11
- Python **3.11+**
- Node.js **20+**
- Rust toolchain（只有构建 Tauri 桌面壳时需要）
- Microsoft Edge（程序会用它打开可见的自动化窗口）
- 一个**有蒲公英资质**的账号（登录由人完成）

### 跑起来（开发态）

```powershell
# 1) 后端依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt

# 2) 启动本地业务内核（监听 127.0.0.1:18081）
python backend_sidecar.py

# 3) 另开一个终端，启动前端
cd frontend
npm install
npm run dev
```

浏览器打开 `http://localhost:5173`，前端会连本地后端。首次使用到「账户」页填写蒲公英账号密码（只写入 Windows 凭据管理器），然后在「选号」页发起检索。

> 开发期不需要反复关闭自动化浏览器：程序每次开工只保留一个自动化页签并复用。需要人工收页签时，运行 `python build\clean_tabs.py --apply`。

## 配置

| 项 | 默认值 | 说明 |
|---|---|---|
| 数据目录 | `%LOCALAPPDATA%\Pgy` | 可用环境变量 `PGY_DATA_DIR` 覆盖 |
| 后端端口 | `127.0.0.1:18081` | 避开常见的 18080 |
| 报价配置 | `<数据目录>\pricing.json` | 真实价格自行填写；仓库里只有不含价格的 `pricing.example.json` |
| 翻页间隔 | 1.5 秒（可配 0.5–30） | 越保守越安全 |

### 报价档位

```powershell
# 把样板复制到数据目录，填入你所在团队的基准价
copy backend\providers\pugongying\pricing.example.json "$env:LOCALAPPDATA\Pgy\pricing.json"
```

格式：

```json
{
  "multiplier": 1.3,
  "tiers": [
    { "label": "头部KOL", "bracket": "20w-30w", "min": 200000, "max": 300000, "image": 0, "video": 0 }
  ]
}
```

- `multiplier`：报价上限倍数（默认 1.3）。
- `image` / `video`：该档位的图文 / 视频基准价；`0` 表示未配置。
- **拿不到配置时预算把关会整体关闭**，界面上会明确告警。

> 仓库**不包含任何真实价格**。`pricing.local.json`、`account.seed.txt` 等本地文件均已在 `.gitignore` 中排除。

## Excel 输出

单表、一人一行，固定 10 列：

| # | 列 | 来源 |
|---|---|---|
| 1 | 昵称 | 蒲公英详情接口 `name` |
| 2 | 小红书号 | 详情页昵称下方的数字（`redId`，不是 24 位内部 `userId`） |
| 3 | 粉丝量（w） | 详情接口 `fansNum` |
| 4 | 赞藏（w） | 详情接口 `likeCollectCountInfo` |
| 5 | 类型 | 全部内容类目 + 特征标签去重 |
| 6 | ip | 达人地域 |
| 7 | 图文报价 | 详情接口 |
| 8 | 视频报价 | 详情接口 |
| 9 | 主页链接 | 由蒲公英 `userId` 拼接 |
| 10 | 主页近10篇内容的点赞中位数 | 蒲公英详情页「日常笔记 / 笔记案例」最新 10 篇 |

超预算的单元格会加底色；缺失报价写英文 `None` 并加同样标注。

## MCP 连接器

`pgy_mcp/` 是一个**只读**的 MCP（stdio）服务，把本地后端包装成 5 个工具：

| 工具 | 用途 |
|---|---|
| `pgy_status` | 本地服务、账号、登录态与当前身份 |
| `pgy_list_industries` | 一级 / 二级行业枚举（检索前先取 ID） |
| `pgy_start_selection` | 启动一次检索与导出（立即返回，用进度工具轮询） |
| `pgy_selection_progress` | 检索进度、当前阶段、交付 Excel 路径 |
| `pgy_login` | 触发一次登录（打开可见浏览器窗口，验证由人完成） |

安装、配置与排错见 [`pgy_mcp/README.md`](pgy_mcp/README.md) 和 [`pgy_mcp/安装说明.md`](pgy_mcp/安装说明.md)。

## 目录结构

```
backend/                     # 唯一的业务内核（FastAPI）
  providers/pugongying/      #   蒲公英检索、字段映射、详情补齐
  services/                  #   节流、凭据、会话、导出、缓存、选号游标
  core/                      #   数据目录与原子写盘
frontend/                    # React UI + Tauri 桌面壳
  src/features/              #   选号页、账户页、设置页
  src-tauri/                 #   Rust 壳、图标、打包配置
pgy_mcp/                     # MCP 连接器（薄客户端，仅标准库）
build/                       # sidecar / MCP / 安装包构建脚本
tests/                       # pytest
```

## 开发与打包

```powershell
# 自动化测试
python -m pytest

# 前端类型检查 + 构建
cd frontend; npm run build

# 打 sidecar（改 backend/ 后必须重打，否则跑的是旧副本）
powershell -NoProfile -ExecutionPolicy Bypass -File build\build-sidecar.ps1

# 打 MCP 连接器
powershell -NoProfile -ExecutionPolicy Bypass -File build\build-mcp.ps1

# 出安装包（产物在 release\，不入库）
powershell -NoProfile -ExecutionPolicy Bypass -File build-installer.ps1
```

构建顺序：**先工作区测试通过 → 再出安装包**。版本号在客户端 / sidecar / MCP 三处必须一致（`build/bump-version.ps1` 提供校验）。

## 合规与免责声明

- 本项目对蒲公英平台**只有读操作**，且仅访问 `pgy.xiaohongshu.com`。
- 所有请求经过统一节流；遇到频率限制会指数退避并如实上报，不静默重试。
- 登录、短信验证、人机验证一律由人完成，项目**不提供**任何验证码识别、打码或绕过手段。
- 请仅在你**有权访问**的账号与数据范围内使用，并自行遵守小红书 / 蒲公英的服务条款及所在地法律法规。
- 平台接口与页面结构可能随时变化；项目按「实测优先」维护，但不保证长期可用。
- 软件按 **MIT** 许可证「按原样」提供，不附带任何明示或暗示的担保。因使用本软件产生的一切后果由使用者自行承担。

## 贡献

欢迎 Issue 与 PR。提交前请确保：

1. `python -m pytest` 全绿，`npm run build` 通过；
2. 新增字段必须先在真实页面上实测，未验证的字段标注清楚，不要直接写进产品代码；
3. **不要**加入平台写操作、验证码绕过、并发轰炸或凭据明文存储——这些是产品边界，不是待办；
4. 提交信息说清楚「改了什么、为什么」，涉及接口/字段变化时附上实测证据。

## 许可证

[MIT](LICENSE) © PgyPicker contributors
