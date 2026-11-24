"""MCP 工具集：把 Pgy 本地服务的能力交给外部 Agent（WorkBuddy）。

**只读**：本项目对蒲公英只读，是做数据采集/选号的，不涉及任何投放动作，
所以工具集里**永远不会有平台写操作**（`CLAUDE.md` 第一条）。

**这里不写业务**：工具只做三件事 —— 拼 URL、把结果翻成人话、把错误原样上抛。
流程语义全在 `backend/`（`CLAUDE.md` 第五条）。**尤其不要在这里"猜"**：
比如不要替模型把行业名称翻译成 ID，那是平台参数翻译，属于业务。

工具描述是**写给模型看的操作手册**，不是写给人看的注释：
一次一个账户、浏览器窗口全程可见、人机验证必须由人点、采集期间浏览器不会自己动
—— 这些约束写进描述，模型才不会自作主张地重试或试图绕过。

新增工具时照抄这个三段式：
    1. 实现函数 `tool_xxx(client, arguments) -> list[dict]`
    2. `TOOLS` 里加一项（name / description / inputSchema / annotations）
    3. `HANDLERS` 里注册
"""

from __future__ import annotations

import json

from .client import BROWSER_READ_TIMEOUT_SECONDS, LOCAL_READ_TIMEOUT_SECONDS, PgyClient, PgyError

#: 一次选号要登录 + 翻页 + 写 Excel，可能几分钟；给足余量（后端自己也有更短的快速失败线）。
SELECTION_TIMEOUT_SECONDS = 10 * 60.0

#: 查进度时要回显多少行达人数据。**不要整份回**：一次能取几百行，
#: 全塞进 MCP 消息会把对话上下文撑爆，而 Agent 真正需要的是"表在哪 + 大概长什么样"。
RESULT_PREVIEW_ROWS = 20


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _delivery_rows(rows: list[dict]) -> list[dict]:
    """只留交付列，剔掉 `__flags__` 这类内部标记。

    `__flags__`（tier_index / rank_group / over_budget / fans …）是后端排序与
    单元格底色用的，**不是交付列**。原样透给模型，模型很容易在工作区回复里
    把它「翻译」成额外列（WorkBuddy 实测就会自己加列）。展示层负责过滤，
    业务语义仍只在 `backend/`。
    """
    return [{k: v for k, v in row.items() if not str(k).startswith("__")}
            for row in rows]


def _read_only() -> dict:
    return {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
            "openWorldHint": False}


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------


def tool_status(client: PgyClient, arguments: dict) -> list[dict]:
    """本地服务是否在跑、有没有账号、平台登录了没有。

    ⚠️ 状态**取自 `/api/selection/status`**（真实登录态），不是 `/api/session`
    —— 后者在当前后端里是个硬编码死桩，永远返回「未连接」，
    拿它当状态会让 Agent 一直以为没登录。
    """
    health = client.get("/api/health")
    status = client.get("/api/selection/status", timeout=LOCAL_READ_TIMEOUT_SECONDS)
    account = client.get("/api/account", timeout=LOCAL_READ_TIMEOUT_SECONDS)

    has_credentials = bool(status.get("has_credentials"))
    logged_in = bool(status.get("logged_in"))

    lines = ["蒲公英达人选号 · 当前状态", ""]
    lines.append(f"- 本地服务：{health.get('status', '?')}（{health.get('service', '?')}）")
    lines.append(f"- 账号：{'已配置 ' + str(account.get('username_masked') or '') if has_credentials else '未配置'}")
    lines.append(f"- 平台登录：{'已登录' if logged_in else '未登录'}")
    if status.get("role"):
        lines.append(f"- 身份：{status['role']}")

    lines.append("")
    if not has_credentials:
        lines.append("下一步：需要人在这台机器上打开 Pgy 客户端，到「账户」页填写账号密码"
                     "（密码只进 Windows 凭据管理器，**不经过对话，也不经过本工具**）。")
    elif not logged_in:
        lines.append("下一步：调 `pgy_login` 触发登录。会打开一个**可见的浏览器窗口**，"
                     "如果出现验证码，需要**人在那台机器上手动完成**。")
    else:
        lines.append("下一步：可以调 `pgy_list_industries` 拿行业，再调 `pgy_start_selection` 开始选号。")

    # 报价档位没配 ⇒ 预算把关是关着的 ⇒ 交付表会混进超预算的达人。
    # 这件事必须让 Agent 说出来，否则用户拿到的名单是错的却不知道。
    if status.get("pricing_configured") is False:
        lines.append("")
        lines.append("⚠️ **报价档位未配置**：预算把关当前是关着的，"
                     "超出报价上限的达人也可能进名单。请把这个风险如实告诉用户。")

    lines.append("")
    lines.append("说明：本工具对蒲公英**只读**，只做达人数据采集与导出，不会发起任何投放动作。")
    return [_text("\n".join(lines)), _text("原始数据：\n" + _dump(status))]


def tool_list_industries(client: PgyClient, arguments: dict) -> list[dict]:
    """一级/二级行业枚举。**平台要的是 ID 不是名称**，所以选号前必须先调这个。"""
    payload = client.get("/api/selection/industries", timeout=BROWSER_READ_TIMEOUT_SECONDS)
    industries = payload.get("industries") or []
    source = payload.get("source")

    lines = [f"共 {len(industries)} 个一级行业：", ""]
    for item in industries:
        children = item.get("children") or []
        names = "、".join(str(c.get("label")) for c in children[:8])
        if len(children) > 8:
            names += f" …（共 {len(children)} 个）"
        lines.append(f"- {item.get('label')}（ID: {item.get('value')}）")
        if names:
            lines.append(f"    二级：{names}")

    lines.append("")
    if source == "cache":
        lines.append("⚠️ 这份清单是**上次存下来的快照**（这次没能连上平台），"
                     "可能不是最新的；登录后重新调一次可刷新。")
    lines.append("给 `pgy_start_selection` 传的是上面括号里的 **ID**，不是中文名称 —— "
                 "传名称平台会返回 0 条。")
    return [_text("\n".join(lines)),
            _text("完整数据（含二级行业的 ID）：\n" + _dump(industries))]


def tool_start_selection(client: PgyClient, arguments: dict) -> list[dict]:
    """**启动**一次选号，立刻返回。之后用 `pgy_selection_progress` 查结果。"""
    body = {
        "first_industry": str(arguments.get("first_industry") or ""),
        "second_industry": str(arguments.get("second_industry") or ""),
        "per_tier": int(arguments.get("per_tier") or 20),
    }
    # `mode` **只在调用方明确给了才透传** —— "默认取哪个"是后端（唯一业务内核）的语义，
    # MCP 这边再判一次就是重复实现（CLAUDE.md 第五条），而且一定会漂。
    requested_mode = str(arguments.get("mode") or "")
    if requested_mode:
        body["mode"] = requested_mode
    # 本地启动动作本身不碰浏览器，很快；真正的检索在后台跑。
    result = client.post("/api/selection/start", body, timeout=LOCAL_READ_TIMEOUT_SECONDS)

    lines = ["已启动检索，**不会等在这里**。", ""]
    lines.append(f"- 一级行业：{body['first_industry'] or '不限'}")
    lines.append(f"- 二级行业：{body['second_industry'] or '不限'}")
    lines.append(f"- 每个量级取：{body['per_tier']} 条")
    lines.append("- 模式：**从头取（第 1 页）**" if requested_mode == "fresh"
                 else "- 模式：默认 —— 接着这组条件上次翻到的位置往下取（给没看过的人）")
    lines.append("")
    lines.append("接下来请**每隔十几秒**调一次 `pgy_selection_progress`，直到 stage 变成 "
                 "`done` 或 `error`。")
    lines.append("可能需要几分钟 —— 采集有节流（宁可慢，不触发平台风控），请勿催促或重复启动。")
    lines.append("")
    lines.append("⚠️ 期间那个浏览器窗口**不会自己翻页或滚动**，这是正常的："
                 "采集是在页面里直接调平台接口，不驱动界面。")
    return [_text("\n".join(lines)), _text(_dump(result))]


def tool_selection_progress(client: PgyClient, arguments: dict) -> list[dict]:
    """查选号进度；跑完后把交付物位置与统计交出来。"""
    payload = client.get("/api/selection/progress", timeout=LOCAL_READ_TIMEOUT_SECONDS)
    stage = str(payload.get("stage") or "idle")
    result = payload.get("result") or {}
    export = result.get("export") or {}
    stats = result.get("stats") or {}

    if stage == "idle":
        return [_text("当前没有进行中的选号，也没有上一次的结果。"
                      "可以调 `pgy_start_selection` 开始一次。")]

    lines = [f"状态：{payload.get('label') or stage}"
             f"（{payload.get('started_at')} → {payload.get('updated_at')}）", ""]

    if stage == "search":
        lines.append(f"- 已翻 {payload.get('page')} 页，已取 {payload.get('collected')} 条"
                     f"（平台共 {payload.get('total')} 条）")
        counts = payload.get("per_tier_counts") or {}
        if counts:
            lines.append("- 各量级：" + "、".join(f"{k} {v} 条" for k, v in counts.items()))
    elif payload.get("detail"):
        lines.append(f"- {payload['detail']}")

    if stage == "error":
        stop = payload.get("failed_label") or payload.get("failed_stage") or "未知"
        lines.append("")
        lines.append(f"❌ **已中断，停在「{stop}」**：{payload.get('error')}")
        lines.append("常见原因：未登录（先调 `pgy_login`）、需要人工过验证码、"
                     "或平台侧异常。**请如实把原因转述给用户，不要自动重试。**")
    elif stage == "done":
        lines.append("")
        lines.append("✅ 已完成。")
        if export.get("path"):
            lines.append(f"- 交付表：{export['path']}")
        if stats:
            lines.append(f"- 共 {stats.get('collected', '?')} 条，翻 {stats.get('pages', '?')} 页")
            counts = stats.get("per_tier_counts") or {}
            if counts:
                lines.append("- 各量级：" + "、".join(f"{k} {v} 条" for k, v in counts.items()))
            short = stats.get("per_tier_short") or {}
            if short:
                lines.append(f"- ⚠️ 没凑满的量级：{_dump(short)}（高量级本来就稀少，如实交少）")
            if stats.get("reached_end"):
                lines.append("- 平台这边已经翻到底了，没有更多新人了。")

        rows = result.get("rows") or []
        if rows:
            preview = rows[:RESULT_PREVIEW_ROWS]
            lines.append("")
            lines.append(f"前 {len(preview)} 条（共 {len(rows)} 条，完整名单见上面的 Excel）：")
            lines.append(_dump(_delivery_rows(preview)))
            lines.append("")
            lines.append("⚠️ 以上只是预览；**交付表的列以那个 Excel 文件为准**。"
                         "回复时原样转述，不要自己增删列、也不要把 rows 重排成表格 —— "
                         "rows 里没有的列就是没有。")
    else:
        lines.append("")
        lines.append("还在进行中，请稍后再查。")

    return [_text("\n".join(lines))]


def tool_login(client: PgyClient, arguments: dict) -> list[dict]:
    """触发一次登录（**会让平台上出现一个可见的浏览器窗口，需要人工过验证码**）。

    凭据由人在客户端里配置，**本工具不接收也不返回任何账号密码**（`CLAUDE.md` 第四条）。
    """
    result = client.post("/api/account/login", {}, timeout=SELECTION_TIMEOUT_SECONDS)
    already = bool(result.get("already"))
    if already:
        return [_text("平台已经是登录态，无需重新登录。")]
    return [_text("登录成功。接下来可以调 `pgy_list_industries` 拿行业，"
                  "再调 `pgy_start_selection` 开始选号。")]


# --------------------------------------------------------------------------
# 工具声明与分发
# --------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "pgy_status",
        "description": (
            "查看蒲公英达人选号的当前状态：本地服务是否在运行、有没有配置账号、"
            "平台是否已登录、当前身份。**开始任何操作前、以及任何一步失败后都应该先调它。**"
            "如果提示未配置账号，需要**人**在装了客户端的机器上到「账户」页填写 —— "
            "账号密码只进 Windows 凭据管理器，不经过对话也不经过本工具，你不要尝试索要或代填。"
            "注意：本工具对蒲公英只读，只做达人数据采集，不会发起任何投放动作。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _read_only(),
    },
    {
        "name": "pgy_list_industries",
        "description": (
            "列出蒲公英的一级/二级行业（选号可用的类目）。"
            "**选号前必须先调这个**：平台要的是行业 ID，不是中文名称 —— "
            "给 `pgy_start_selection` 传名称会得到 0 条结果。"
            "返回里会标明这份清单是刚去平台取的，还是上次存下来的快照。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _read_only(),
    },
    {
        "name": "pgy_start_selection",
        "description": (
            "**启动**一次达人检索与导出，立即返回 —— 它不会等检索跑完。"
            "启动后必须每隔十几秒调 `pgy_selection_progress` 查进度，"
            "直到状态变成 done（拿到 Excel 路径）或 error（拿到中断原因）。"
            "一次检索可能几分钟：采集有节流（宁可慢，也不触发平台风控），"
            "而且**中途可能需要人在浏览器窗口里过验证码**。"
            "严禁因为「看起来没动静」就重复启动 —— 同一时刻只允许一个，重复调用会被拒绝。"
            "筛选只支持行业与每量级条数；粉丝量等其它维度的平台参数尚未破译，本项目不提供。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "first_industry": {
                    "type": "string",
                    "description": "一级行业 ID（**不是名称**，取自 pgy_list_industries）；留空=不限",
                },
                "second_industry": {
                    "type": "string",
                    "description": "二级行业 ID；留空=不限",
                },
                "per_tier": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "每个量级（超头部/头部/腰部/尾部）各取多少条，默认 20",
                },
                "mode": {
                    "type": "string",
                    "enum": ["fresh", "next"],
                    "description": (
                        "**一般不用传**。不传就是后端的默认：接着这组条件上次翻到的位置往下取，"
                        "给的是没看过的人（平台排序是写死的，每次都从头取的话，"
                        "池子里剩下的永远轮不到）。"
                        "只有用户明确说「想再看一遍第 1 页那批人」时才传 fresh。"
                    ),
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "pgy_selection_progress",
        "description": (
            "查选号进度：进行到哪一步（登录/检索/导出）、翻了第几页、各量级已取多少条；"
            "跑完时给出交付 Excel 的**完整路径**与统计，中断时给出**停在哪一步**和原因。"
            "`pgy_start_selection` 之后请轮询这个工具。"
            "中断时**如实转述原因，不要自动重试**：多半需要人处理（登录、验证码）。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _read_only(),
    },
    {
        "name": "pgy_login",
        "description": (
            "触发一次蒲公英登录。**会打开一个可见的浏览器窗口**；"
            "如果平台要求验证码或短信验证，需要**人在那台机器上手动完成** —— "
            "你要明确告诉用户「请到屏幕上的浏览器窗口里完成验证」，然后等待，"
            "**绝不要尝试识别验证码、绕过风控或反复重试**。"
            "本工具不接收也不返回任何账号密码。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": True},
    },
]

HANDLERS = {
    "pgy_status": tool_status,
    "pgy_list_industries": tool_list_industries,
    "pgy_start_selection": tool_start_selection,
    "pgy_selection_progress": tool_selection_progress,
    "pgy_login": tool_login,
}


def call_tool(client: PgyClient, name: str, arguments: dict) -> list[dict]:
    """把一次 tools/call 转给具体实现。

    只拦「工具名不存在」这类协议层问题；业务失败一律让 `PgyError` 上抛，
    由 server 统一转成 `isError` 结果——不在这里把原因盖成「工具执行失败」。
    """
    handler = HANDLERS.get(name)
    if handler is None:
        raise PgyError(f"没有名为 {name} 的工具。", code="UNKNOWN_TOOL")
    if not isinstance(arguments, dict):
        raise PgyError("工具参数必须是一个 JSON 对象。", code="BAD_ARGUMENTS")
    return handler(client, arguments)
