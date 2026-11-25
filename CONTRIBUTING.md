# 贡献指南

感谢你愿意参与 PgyPicker。提交 Issue / PR 前，请先读一遍下面的约定。

## 先看这些

- [README](README.md)：功能、边界、安装与配置
- [最新 Release](https://github.com/Jun-shisheng/pgy-picker/releases)：安装包与版本说明
- [问题模板](.github/ISSUE_TEMPLATE)：报 bug / 提建议请用模板，信息更全，处理更快

## 产品边界（不是待办）

以下几条是**产品定义**，不接受以开关或 PR 的形式加回来：

1. **对平台只读**。不发起邀约、不建合作、不评论、不私信、不投放。
2. **只访问 `pgy.xiaohongshu.com`**。不访问小红书主站，不要求小红书登录或扫码。
3. **不绕过风控**。登录、短信验证、人机验证一律由人在可见浏览器窗口里完成；不实现验证码识别、打码或任何伪装。
4. **所有平台请求必须经过节流器**。禁止并发轰炸或直接裸发请求。
5. **凭据只进 Windows 凭据管理器**。不写入 SQLite / JSON / 日志 / 源码，任何接口都不返回密码。
6. **MCP 是薄客户端**。业务语义只存在于 `backend/`，不要在 MCP 层复制业务判断。

涉及以上边界的 PR 会被直接关闭。

## 开发环境

```powershell
# 后端
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt

# 前端
cd frontend
npm install
npm run dev

# 后端服务（另开终端）
python backend_sidecar.py
```

数据目录默认 `%LOCALAPPDATA%\Pgy`，可用 `PGY_DATA_DIR` 覆盖；后端端口 `127.0.0.1:18081`。

## 提交前必须通过

```powershell
python -m pytest          # 全绿
cd frontend; npm run build
```

改了 `backend/` 后，开发态请直接用源码跑；打包版必须重打 sidecar
（`build\build-sidecar.ps1`），否则跑的还是旧副本。

## 字段与接口改动

- **实测先行**：任何字段在拿到真实页面/接口返回之前，不要写进产品代码，先标「待验证」。
- 平台接口与页面结构以实测为准，搜索到的第三方资料只用于提假设。
- 涉及字段映射的改动，请在 PR 描述里附上原始响应片段（**删掉账号、Cookie、token 等敏感信息**）。

## PR 约定

- 一个 PR 只做一件事，说清「改了什么、为什么」。
- 新增行为请补测试；修 bug 请尽量补一条能复现的用例。
- 不要提交 `release/`、`build/` 产物、`profiles/`、`exports/`、日志或任何真实业务数据。
- 不要提交真实报价、客户材料或平台原始响应。

## 安全问题

请**不要**在公开 Issue 里报告安全漏洞。走
[Security Advisories](https://github.com/Jun-shisheng/pgy-picker/security/advisories/new)
私密报告，详见 [SECURITY.md](SECURITY.md)。
