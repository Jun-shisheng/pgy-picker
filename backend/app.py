"""Pgy 本地服务（FastAPI）。

**这是本产品唯一的业务内核。** React 前端与 `pgy_mcp` 都是薄客户端，只通过这里的
HTTP 接口说话 —— 业务语义不允许在客户端或 MCP 层复制一份（见 `CLAUDE.md` 第五条）。

边界（`CLAUDE.md` 第一、二条）：
- 对蒲公英平台**只读**：本服务不提供任何邀约 / 建合作 / 评论 / 私信之类的写操作接口；
- 浏览器只借用不接管，人机验证由人在可见窗口里处理。

M0 阶段只暴露最小面：健康握手 + 会话桩 + 前端错误上报。
筛号、采集、导出等接口按 `docs/03-基础设计说明书.md` §6 逐步加入。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.services import mio_logging

log = logging.getLogger("pgy.app")


class OperationHttpError(HTTPException):
    """带机器码的错误信封。

    `detail` 一律是**写给人看的中文**，客户端与 MCP 直接透传、不要二次加工 ——
    重写文案会让排障时对不上日志（参考项目的教训）。
    """

    def __init__(self, status: int, code: str, message: str,
                 operation_id: str | None = None, retryable: bool = False):
        super().__init__(status_code=status, detail=message)
        self.code = code
        self.operation_id = operation_id
        self.retryable = retryable


class SettingsPatch(BaseModel):
    """设置补丁。空字符串 = 恢复默认值。"""

    export_dir: str = Field(default="", description="表格输出目录；留空恢复默认")


class SelectionRun(BaseModel):
    """一次选号请求的参数。**定义在模块级**：局部定义的模型在 FastAPI 里
    解析 body 时容易出问题（第一次就栽在这，表现为无声的 422）。"""

    first_industry: str = Field(default="", description="一级行业 ID（不是名称）；留空=不限")
    second_industry: str = ""
    per_tier: int = Field(default=20, ge=1, le=500, description="**每个量级**取多少条")
    interval: float = Field(default=1.5, ge=0.5, le=30.0, description="翻页间隔秒数；保守优先")
    mode: str = Field(default="fresh",
                      description="`fresh`=从头取（同一条件给同一批人）；"
                                  "`next`=换一批，接着该条件上次翻到的位置往下取")


def _mode_of(body: SelectionRun) -> str:
    """认不出来的 `mode` 一律当「从头取」：宁可按保守语义走，
    也不要因为拼错一个词就 422（两个入口共用这一份判断）。"""
    return "next" if body.mode == "next" else "fresh"


class AccountUpsert(BaseModel):
    """录入/更新蒲公英账号。

    ⚠️ 这个模型**只进不出**：任何接口都不得把 `password` 回传
    （`CLAUDE.md` 第四条）。改密码时才填 `password`；留空表示**沿用原密码**。
    """

    username: str = Field(default="", description="蒲公英登录账号")
    password: str = Field(default="", description="留空 = 保留原密码")


def create_app() -> FastAPI:
    mio_logging.setup_logging()
    mio_logging.install_excepthooks()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("本地服务已启动 %s", mio_logging.environment_summary())
        try:
            yield
        finally:
            # 退出时**关掉本工具自己开的自动化页签**（"查完销毁"，避免页签越堆越多），
            # 但不碰用户在别的窗口里的任何东西（CLAUDE.md 第二条）。
            try:
                from backend.providers.pugongying import session as pgy_session
                pgy_session.close_session()
                # 顺序不能反：先关会话，再停驱动。**必须放在进程退出路径上** ——
                # 停过之后就无法再建会话了（见 session._playwright 的说明）。
                pgy_session.shutdown_playwright()
            except Exception:
                log.warning("关闭浏览器会话时出错（不影响退出）", exc_info=True)
            log.info("本地服务已关闭")

    # ⚠️ 版本号必须是这里的**字面量**：build-installer.ps1 会用正则从本文件读出它，
    # 与 tauri.conf.json / Cargo.toml / frontend/package.json 做一致性校验。
    app = FastAPI(title="Pgy 本地服务", version="0.1.3", lifespan=lifespan)

    # ---------- 错误信封 ----------

    @app.exception_handler(OperationHttpError)
    async def operation_error(request: Request, exc: OperationHttpError):
        return JSONResponse(
            {"detail": exc.detail, "code": exc.code,
             "retryable": exc.retryable, "operation_id": exc.operation_id},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Pydantic 默认会把被拒的输入回显出来（可能含密码），所以**不回显**；
        # 但**必须记日志** —— 否则排障时只剩一句「格式不正确」，等于没有信息。
        log.warning("请求参数校验失败 path=%s 详情=%s", request.url.path, exc.errors())
        return JSONResponse({"detail": "请求参数格式不正确，请检查输入。"}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        """兜底异常：完整堆栈进日志，界面只给一句可操作的提示。

        打包后的 EXE 没有控制台，未处理的异常原本只留在已关闭的 stderr 里。
        """
        log.exception("未处理的服务端异常 path=%s method=%s", request.url.path, request.method)
        return JSONResponse(
            {"detail": "本地服务内部错误，已记录到运行日志；请在「设置 → 运行日志」查看详情。",
             "code": "INTERNAL_ERROR", "retryable": True},
            status_code=500,
        )

    # ---------- 来源校验 ----------
    # 本服务持有凭据（凭据管理器里的密码），不能被任意站点读到或触发。
    allowed_origins = [
        "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:1420", "http://127.0.0.1:1420",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def check_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            log.warning("拒绝不允许的客户端来源 origin=%s path=%s", origin, request.url.path)
            return JSONResponse({"detail": "不允许的客户端来源"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """请求级日志：读请求只在失败时记录，写请求与失败请求始终记录。"""
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("请求失败 %s %s（耗时 %.2fs）", request.method, request.url.path,
                          time.perf_counter() - started)
            raise
        elapsed = time.perf_counter() - started
        status = response.status_code
        if status >= 500:
            log.error("请求 %s %s → %s（耗时 %.2fs）", request.method, request.url.path, status, elapsed)
        elif status >= 400:
            log.warning("请求 %s %s → %s（耗时 %.2fs）", request.method, request.url.path, status, elapsed)
        elif request.method != "GET":
            log.info("请求 %s %s → %s（耗时 %.2fs）", request.method, request.url.path, status, elapsed)
        else:
            log.debug("请求 %s %s → %s（耗时 %.2fs）", request.method, request.url.path, status, elapsed)
        response.headers["X-Pgy-Duration-Ms"] = f"{elapsed * 1000:.0f}"
        return response

    # ---------- 接口 ----------

    @app.get("/api/health")
    def health() -> dict:
        """身份握手端点。

        **响应里必须同时出现 `service` 与 `pgy` 两个词**：Rust 壳
        （`probe_backend_identity`）与 MCP（`pgy_mcp/discovery.py` 的 IDENTITY_MARKERS）
        都按这两个词判断「端口上跑的到底是不是本程序的后端」。

        用 `pgy` 而非参考项目的 `playwright`，是为了和可能同机共存的 Ling 客户端区分开。
        """
        return {"status": "ok", "service": "ready", "browser": "playwright", "product": "pgy"}

    @app.get("/api/session")
    def session() -> dict:
        """当前会话状态桩。

        M0 只返回「未连接」。真实实现要接会话状态机与浏览器登录态（M2/M3）。
        形状刻意与 `docs/03` §6 及前端消费方对齐，后续填充而不改契约。
        """
        return {
            "account_id": "",
            "account": {},
            "connection_status": "disconnected",
            "executor_status": "idle",
            "control_mode": "automation",
            "operation": {},
        }

    # ---------- 选号 ----------
    # ⚠️ 这些是 **def**（不是 async def）：里面跑的是同步 Playwright，
    # FastAPI 会把 def 端点丢到线程池，正好配合 session 的单工作线程模型。

    @app.get("/api/selection/status")
    def selection_status() -> dict:
        """选号前置状态：有没有凭据、当前登录态、报价档位配没配。

        `pricing_configured` 是**必看的**：没配报价档位时**预算把关是关着的**
        （`pricing.load()` 返回 None，`within_budget` 一律放行），交付表会混进
        超预算的达人。这件事以前只写在 Excel 的「元信息」表里，那张表按用户要求删了，
        所以必须搬到界面上 —— 不然它会彻底隐形。
        """
        from backend.providers.pugongying import pricing
        from backend.services import credential_service, selection_service
        try:
            status = selection_service.login_status()
        except Exception as exc:
            log.warning("查询登录态失败：%s", exc)
            status = 0
        try:
            pricing_config = pricing.load()
        except Exception as exc:
            log.warning("读取报价档位失败：%s", exc)
            pricing_config = None
        return {
            "has_credentials": credential_service.has_credentials(),
            "login_status": status,
            "logged_in": status == 200,
            "role": selection_service.DEFAULT_ROLE,
            "pricing_configured": pricing_config is not None,
            "pricing_multiplier": pricing_config.multiplier if pricing_config else None,
        }

    @app.get("/api/selection/progress")
    def selection_progress() -> dict:
        """选号进度快照，供界面画步骤条与计数。

        没有它，`/api/selection/run` 那几分钟里界面只能转一个按钮 ——
        用户看不出是在干活、还是已经卡死、还是早就失败了。
        """
        from backend.services import progress
        return progress.snapshot()

    @app.get("/api/selection/industries")
    def selection_industries() -> dict:
        """一级/二级行业枚举（前端下拉用；平台要的是 ID 不是名称）。

        `source` 如实标注这份数据是**这次真去平台问的**（`live`）还是
        **上次存下来的快照**（`cache`）—— 别让用户以为是实时数据（`CLAUDE.md` 第九条）。
        没有会话又没有快照时返回 404，前端据此引导「先去账户页登录」。
        """
        from backend.services import selection_service
        try:
            items, source = selection_service.industries_cached()
            return {"industries": items, "source": source}
        except selection_service.NotReady as exc:
            raise OperationHttpError(
                404, "NOT_CACHED",
                "还没有拿到过行业清单，而且当前没有可用的登录态。"
                "请先到「账户」页登录，再回到这里加载。") from exc
        except Exception as exc:
            log.exception("读取行业枚举失败")
            raise OperationHttpError(500, "INDUSTRIES_FAILED",
                                     f"读取行业枚举失败：{exc}", retryable=True) from exc

    @app.post("/api/selection/run")
    def selection_run(body: SelectionRun) -> dict:
        """跑一次选号：登录 → 检索 → 映射 → 导出 Excel。返回行数据与产物路径。

        会用真实账号访问蒲公英（**只读**），耗时通常几十秒到几分钟，
        取决于条数与节流间隔 —— 不要在客户端设太短的超时。
        """
        from backend.providers.pugongying.search import SearchParams
        from backend.services import selection_service, throttle

        throttle.default.min_interval = body.interval
        params = SearchParams(first_industry=body.first_industry,
                              second_industry=body.second_industry)
        try:
            # 编排、进度、失败落点都在 service 里（**唯一实现**），见 run_selection。
            return selection_service.run_selection(
                params, per_tier=body.per_tier, mode=_mode_of(body))
        except selection_service.OperationBusy as exc:
            raise OperationHttpError(409, "OPERATION_CONFLICT", str(exc), retryable=True) from exc
        except selection_service.NotReady as exc:
            raise OperationHttpError(409, "NOT_READY", str(exc), retryable=True) from exc
        except Exception as exc:
            log.exception("选号失败")
            raise OperationHttpError(500, "SELECTION_FAILED",
                                     f"选号失败：{exc}", retryable=True) from exc

    @app.post("/api/selection/start")
    def selection_start(body: SelectionRun) -> dict:
        """启动一次选号并**立即返回**，稍后用 `/api/selection/progress` 查进度。

        这个入口是给 MCP 的：Agent 的一次 `tools/call` 不该阻塞几分钟等结果
        （客户端多半会先超时）。界面那条路仍走同步的 `/api/selection/run` ——
        两者共用 `selection_service.run_selection`，不是两套实现。

        跑完之后，结果（表在哪、取了多少条）出现在进度快照的 `result` 字段里。
        """
        from backend.providers.pugongying.search import SearchParams
        from backend.services import selection_service, throttle

        if selection_service.is_busy():
            raise OperationHttpError(
                409, "OPERATION_CONFLICT",
                "已经有一次检索在进行中，请等它结束后再启动。", retryable=True)

        throttle.default.min_interval = body.interval
        params = SearchParams(first_industry=body.first_industry,
                              second_industry=body.second_industry)
        mode = _mode_of(body)

        def worker():
            try:
                selection_service.run_selection(params, per_tier=body.per_tier, mode=mode)
            except Exception:
                # 失败原因已经写进进度快照了，这里只是别让后台线程的异常刷到 stderr。
                log.info("后台检索结束（失败原因见进度快照）", exc_info=True)

        # daemon：进程退出时不要卡在一个跑了一半、还在等平台响应的检索上。
        threading.Thread(target=worker, name="pgy-selection", daemon=True).start()
        return {"started": True, "mode": mode}

    @app.get("/api/selection/artifact/{name}")
    def selection_artifact(name: str):
        """下载导出文件。**只允许 exports 目录下的文件**（防目录穿越）。"""
        from fastapi.responses import FileResponse
        from backend.services.selection_service import EXPORT_DIR

        if "/" in name or "\\" in name or ".." in name:
            raise OperationHttpError(400, "BAD_NAME", "文件名不合法")
        target = EXPORT_DIR / name
        if not target.is_file():
            raise OperationHttpError(404, "NOT_FOUND", "文件不存在")
        return FileResponse(target, filename=name,
                            media_type="application/vnd.openxmlformats-officedocument"
                                       ".spreadsheetml.sheet")

    # ---------- 账户 ----------
    # 单账号。凭据只进 Windows 凭据管理器（`CLAUDE.md` 第四条）：
    # **只写不读** —— 这里没有任何接口会把密码回传，`AccountUpsert` 只进不出。
    # ⚠️ 这几个端点**有意不进 MCP 工具清单**：登录这一步不经过大模型。

    @app.get("/api/account")
    def get_account() -> dict:
        """账号概览。**只回掩码后的账号名**（`a***`），密码永不出现在响应里。"""
        from backend.services import credential_service
        return credential_service.describe()

    @app.put("/api/account")
    def put_account(body: AccountUpsert) -> dict:
        """录入/更新账号凭据。`password` 留空表示**沿用原密码**（只改账号名时用）。"""
        from backend.services import credential_service

        username = body.username.strip()
        if not username:
            raise OperationHttpError(400, "BAD_CREDENTIALS", "账号不能为空。")

        password = body.password
        if not password:
            existing = credential_service.load()
            if existing is None:
                raise OperationHttpError(400, "BAD_CREDENTIALS", "密码不能为空。")
            password = existing[1]

        credential_service.save(username, password)
        return credential_service.describe()

    @app.delete("/api/account")
    def delete_account() -> dict:
        """删除凭据（幂等：本来就没有也返回成功，只是 `deleted` 为 false）。"""
        from backend.services import credential_service
        return {"ok": True, "deleted": credential_service.delete()}

    @app.post("/api/account/login")
    def account_login() -> dict:
        """触发一次登录：在那个**屏幕上看得见的浏览器窗口**里跑。

        要人过验证码，所以可能挂着好几分钟 —— 界面必须如实说明，
        不要在客户端设太短超时。
        """
        from backend.services import selection_service
        try:
            result = selection_service.login_now()
        except selection_service.OperationBusy as exc:
            raise OperationHttpError(409, "OPERATION_CONFLICT", str(exc), retryable=True) from exc
        except selection_service.NotReady as exc:
            raise OperationHttpError(409, "NOT_READY", str(exc), retryable=True) from exc
        except Exception as exc:
            log.exception("登录失败")
            raise OperationHttpError(500, "LOGIN_FAILED",
                                     f"登录失败：{exc}", retryable=True) from exc
        return {"connected": True, "already": bool(result.get("already"))}

    # ---------- 设置 ----------

    @app.get("/api/settings")
    def get_settings() -> dict:
        from backend.services import settings_service
        data = settings_service.load()
        data["resolved_export_dir"] = str(settings_service.export_dir())
        return data

    @app.put("/api/settings")
    def put_settings(body: SettingsPatch) -> dict:
        """改设置。只接受校验通过的导出目录 —— 目录写不进去要当场说，
        而不是等检索跑完 30 秒、导出时才失败。"""
        from backend.services import settings_service
        if body.export_dir.strip():
            ok, detail = settings_service.validate(body.export_dir)
            if not ok:
                raise OperationHttpError(400, "BAD_EXPORT_DIR", f"导出目录不可用：{detail}")
        data = settings_service.save({"export_dir": body.export_dir})
        data["resolved_export_dir"] = str(settings_service.export_dir())
        return data

    @app.post("/api/settings/validate")
    def validate_settings(body: SettingsPatch) -> dict:
        from backend.services import settings_service
        ok, detail = settings_service.validate(body.export_dir)
        return {"ok": ok, "detail": detail}

    @app.get("/api/logs/tail")
    def logs_tail(limit: int = 400, level: str = "", q: str = "") -> dict:
        """读运行日志的尾部，供「设置 → 运行日志」展示。

        等级与关键词过滤**在后端做**：日志会涨到几万行，拉进界面再筛纯属浪费。

        ⚠️ 查询参数叫 `q`，而 `read_log_file()` 的形参叫 `query` —— 这里显式对齐，
        免得两边名字一样、实际没传进去。
        """
        return mio_logging.read_log_file(limit=limit, level=level, query=q)

    @app.post("/api/logs/client")
    async def client_log(request: Request) -> dict:
        """接收前端上报的未捕获错误，落到同一份运行日志里。

        打包后的前端没有控制台，这个端点是界面故障唯一的事后线索。
        """
        try:
            payload = await request.json()
        except Exception:
            return {"ok": False}
        if not isinstance(payload, dict):
            return {"ok": False}
        log.warning(
            "前端错误 kind=%s message=%s stack=%s url=%s",
            str(payload.get("kind") or "")[:64],
            str(payload.get("message") or "")[:2000],
            str(payload.get("stack") or "")[:2000],
            str(payload.get("url") or "")[:500],
        )
        return {"ok": True}

    return app


app = create_app()
