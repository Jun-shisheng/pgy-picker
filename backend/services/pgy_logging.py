"""本地运行日志：轮转文件 + 内存环形缓冲 + 全局异常钩子。

为什么必须独立成模块：打包后的 EXE 以 ``--noconsole`` 运行，stdout/stderr 没有
任何窗口可看。一旦在同事机器上启动失败、卡在某个浏览器操作或抛出异常，如果日志
只写到标准输出，就等于「程序没反应且没有任何线索」。本模块把全部运行信息落到
本机文件（``<数据目录>/pgy_logs/pgy.log``），同时保留内存环形缓冲，供
「设置 → 运行日志」在界面里直接查看（不需要用户去翻磁盘）。

职责边界：
- 只负责「日志往哪里写、异常怎么落盘、缓冲怎么读」，不含任何业务判断；
- 不写密码/验证码/Cookie：调用方本就禁止记录这些值，这里再加一层正则脱敏作为兜底；
- 写盘失败（磁盘满、目录只读）不得影响业务：文件处理器交给 logging 自身的
  ``handleError`` 兜底，环形缓冲与 stderr 仍然保留。

线程模型：logging 内部已保证 Handler 级线程安全，本模块额外用锁保护环形缓冲的
读写（环形缓冲不只是被 Handler 写，还会被 HTTP 线程读）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.pgy_core import LOG_DIR, ROOT

LOG_FILE_NAME = "pgy.log"
RING_CAPACITY = 5000
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 行首格式与 LOG_FORMAT 一一对应，供日志接口把文件内容解析成结构化条目。
LINE_PATTERN = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<level>[A-Z]+) (?P<logger>\S+) (?P<message>.*)$"
)

# 兜底脱敏：即使调用方不小心把整条请求/异常文本塞进日志，也不让口令类值落盘。
_SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|authorization|cookie|credential)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]+)"
)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_setup_lock = threading.RLock()
_ring_lock = threading.Lock()
_ring: deque[dict[str, Any]] = deque(maxlen=RING_CAPACITY)
_configured = False
_started_at = time.time()


class _RedactingFilter(logging.Filter):
    """把日志正文里的口令类字段替换成 ``***``。

    这是第二道防线：主防线是「调用方不记录敏感值」的代码约束。之所以仍然保留，
    是因为 Playwright 的异常消息可能包含已填入表单的值，而这类异常最容易在排障时
    被整体打进日志。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _SECRET_PATTERN.sub(r"\1\2***", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _BenignNoiseFilter(logging.Filter):
    """丢掉已知无害、但会以 ERROR 出现的解释器/驱动噪音。

    目前只有一条：Playwright 的 `Response.finished` 回调在页签被关闭之后仍会跑一次，
    抛 `Error('Target closed')`，asyncio 把它打成「Task exception was never retrieved」。
    页签关闭正是我们自己的正常动作（例如平台判定"0 条可添加"后收掉空表单页），
    它不影响任何结果，但会以 ERROR 出现在界面上，让用户以为流程出错
    （真机 2026-09-23 用户就是拿着这条来问"是超时吗"），因此在日志层统一滤掉。

    注意：只滤这一种组合，其它 asyncio/驱动异常照常记录。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "Task exception was never retrieved" not in message:
            return True
        return "Target closed" not in message


class _RingHandler(logging.Handler):
    """把最近若干条日志留在内存里，供界面按等级/关键词即时检索。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "at": datetime.fromtimestamp(record.created).strftime(DATE_FORMAT),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        except Exception:
            return
        with _ring_lock:
            _ring.append(entry)


def log_dir() -> Path:
    return Path(LOG_DIR)


def log_file() -> Path:
    return log_dir() / LOG_FILE_NAME


def startup_error_file() -> Path:
    """启动期致命错误（导入失败、端口占用）的落盘位置。

    与主日志分开的原因：主日志由 logging 初始化后才会创建，而「初始化之前的崩溃」
    恰恰是最需要证据的场景（例如 PyInstaller 缺模块、Playwright 驱动缺失）。
    """
    return log_dir() / "pgy-startup-error.log"


def logger(name: str = "pgy") -> logging.Logger:
    return logging.getLogger(name)


def uptime_seconds() -> float:
    return round(time.time() - _started_at, 1)


def setup_logging(level: int = logging.INFO) -> Path:
    """初始化根日志：文件（轮转）+ 内存缓冲 +（非冻结时）控制台。返回日志文件路径。

    幂等：重复调用只调整级别，不重复挂 Handler——否则每次调用都会多一份文件句柄，
    日志行数会成倍增长，排查时反而更乱。
    """
    global _configured
    path = log_file()
    with _setup_lock:
        root = logging.getLogger()
        if _configured:
            root.setLevel(level)
            return path
        log_dir().mkdir(parents=True, exist_ok=True)

        root.setLevel(level)
        formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
        redact = _RedactingFilter()
        benign = _BenignNoiseFilter()

        try:
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redact)
            file_handler.addFilter(benign)
            root.addHandler(file_handler)
        except OSError as exc:  # 只读目录 / 磁盘满：保留内存缓冲与控制台
            sys.stderr.write(f"[pgy] 日志文件不可写，仅保留内存日志：{exc}\n")

        ring_handler = _RingHandler()
        ring_handler.addFilter(redact)
        ring_handler.addFilter(benign)
        root.addHandler(ring_handler)

        # 冻结（PyInstaller）后没有可用控制台，stderr 写入可能抛错，因此只判断
        # 「是否冻结」而不是「是否有 stderr 对象」。
        if not getattr(sys, "frozen", False):
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            stream_handler.addFilter(redact)
            stream_handler.addFilter(benign)
            root.addHandler(stream_handler)

        # uvicorn 自带 logger 默认不配置 handler，这里统一交给根 logger 处理，
        # 服务器启动/关闭/异常都会进入同一个文件。
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers = []
            uvicorn_logger.propagate = True
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        # 测试/依赖库的逐请求日志与业务无关，降到 WARNING 免得淹没真正的故障。
        for name in ("httpx", "httpcore", "asyncio", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)
        _configured = True

    logger().info("日志系统就绪 file=%s level=%s", path, logging.getLevelName(level))
    return path


def set_level(level: str) -> str:
    """运行时切换日志级别（界面「详细日志」开关用）。返回生效后的级别名。"""
    name = (level or "").upper()
    if name not in _LEVELS:
        raise ValueError("不支持的日志级别")
    with _setup_lock:
        logging.getLogger().setLevel(_LEVELS[name])
    logger().info("日志级别已切换为 %s", name)
    return name


def current_level() -> str:
    return logging.getLevelName(logging.getLogger().level)


def write_startup_error(title: str, detail: str) -> Path:
    """把启动期致命错误追加写入独立文件；返回文件路径。

    绝不让写日志本身再抛异常：这个函数已经处在「启动失败」的兜底路径上。
    """
    path = startup_error_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {datetime.now().strftime(DATE_FORMAT)} {title} =====\n")
            handle.write(f"{detail}\n")
    except OSError:
        pass
    return path


def ring_snapshot(limit: int = 500, level: str = "", query: str = "") -> list[dict[str, Any]]:
    """读取内存缓冲中的最近日志，可按最低等级与关键词过滤。"""
    minimum = _LEVELS.get((level or "").upper(), 0)
    keyword = (query or "").strip().lower()
    with _ring_lock:
        items = list(_ring)
    if minimum:
        items = [item for item in items if _LEVELS.get(item.get("level", "INFO"), 0) >= minimum]
    if keyword:
        items = [item for item in items if keyword in item.get("message", "").lower()
                 or keyword in item.get("logger", "").lower()]
    return items[-max(1, min(limit, RING_CAPACITY)):]


def _tail_bytes(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(max(0, length - size))
            return handle.read()
    except OSError:
        return b""


def read_log_file(limit: int = 500, level: str = "", query: str = "") -> dict[str, Any]:
    """读取日志文件尾部并按需过滤，返回给「运行日志」界面。

    只读文件尾部固定字节数：日志会持续增长，界面只需要最近的现场；
    ``truncated`` 表示本次读取被字节上限截断，提示用户还有更早的内容。
    """
    path = log_file()
    maximum = max(1, min(limit, 5000))
    payload: dict[str, Any] = {
        "file": str(path), "dir": str(log_dir()), "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "truncated": False, "lines": [],
    }
    if not path.exists():
        return payload

    window = 512 * 1024
    raw = _tail_bytes(path, window)
    try:
        size = path.stat().st_size
    except OSError:
        size = len(raw)
    payload["truncated"] = size > window
    text = raw.decode("utf-8", errors="replace")
    # 截断读取时第一行必然不完整，丢弃它以免出现半行日志。
    lines = text.splitlines()
    if payload["truncated"] and lines:
        lines = lines[1:]

    minimum = _LEVELS.get((level or "").upper(), 0)
    keyword = (query or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        match = LINE_PATTERN.match(line)
        if match is None:
            # 续行（例如 Python traceback 的后续堆栈行）挂到上一条上，
            # 这样界面里一次异常仍然是一个完整块。
            if entries:
                entries[-1]["detail"].append(line.rstrip())
            else:
                entries.append({"at": "", "level": "INFO", "logger": "", "message": line.rstrip(), "detail": []})
            continue
        entries.append({
            "at": match.group("at"), "level": match.group("level"),
            "logger": match.group("logger"), "message": match.group("message"), "detail": [],
        })
    if minimum:
        entries = [item for item in entries if _LEVELS.get(item["level"], 0) >= minimum]
    if keyword:
        entries = [item for item in entries
                   if keyword in item["message"].lower()
                   or keyword in item["logger"].lower()
                   or any(keyword in extra.lower() for extra in item["detail"])]
    payload["lines"] = entries[-maximum:]
    payload["matched"] = len(entries)
    return payload


def install_excepthooks() -> None:
    """把主线程与子线程的未捕获异常也写进日志。

    不做这件事的话，后台线程（浏览器工作线程、看门狗）里抛出的异常只会打到
    stderr——在打包后的 EXE 里等于彻底消失，表现为「功能静默失效」。
    """
    log = logger("uncaught")
    previous_hook = sys.excepthook

    def main_hook(exc_type, exc_value, exc_traceback):
        log.critical("主线程未捕获异常：%s: %s", exc_type.__name__, exc_value,
                     exc_info=(exc_type, exc_value, exc_traceback))
        previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = main_hook

    def thread_hook(args: Any) -> None:
        log.critical("线程 %s 未捕获异常：%s: %s", getattr(args.thread, "name", "?"),
                     args.exc_type.__name__, args.exc_value,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook


def environment_summary() -> dict[str, Any]:
    """启动时记录一次运行环境，供异地排障判断「这台机器和别人有什么不同」。"""
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": str(ROOT),
        "log_file": str(log_file()),
        "platform": sys.platform,
        "pid": os.getpid(),
    }


def browser_environment() -> dict[str, Any]:
    """探测浏览器可用性（在别人电脑上跑不起来的最常见原因）。

    识别顺序与真正启动浏览器时完全一致：**先用电脑上已安装的 Edge/Chrome**
    （自动识别路径，可用 MIO_BROWSER_EXECUTABLE 覆盖），都没有才回落到
    Playwright 自带内核。只报告结论，不启动浏览器：真实的启动失败原因由
    登录/操作时的异常日志给出。
    """
    from backend.services import pgy_system_browser

    info: dict[str, Any] = {"engine": "", "path": "", "available": False, "playwright": False,
                            "detail": "", "mode": ""}
    try:
        import playwright  # noqa: F401  仅探测包是否可用
        info["playwright"] = True
    except Exception as exc:
        info["detail"] = f"Playwright 未安装：{exc}"
        return info

    report = pgy_system_browser.browser_report()
    info["mode"] = report.get("mode", "")
    if report.get("available"):
        info.update(engine=os.path.basename(str(report.get("path") or "")),
                    path=str(report.get("path") or ""), available=True,
                    detail=str(report.get("detail") or ""))
        return info

    bundled = _bundled_chromium()
    if bundled:
        info.update(engine=bundled.name, path=str(bundled), available=True,
                    detail=f"{report.get('detail') or '未找到系统 Edge/Chrome'}；改用 Playwright 自带浏览器内核")
        return info
    info["detail"] = (f"{report.get('detail')}；Playwright 自带内核也未安装"
                      if report.get("detail") else
                      "未找到本机 Edge/Chrome，且 Playwright 自带内核未安装")
    return info


def _bundled_chromium() -> Path | None:
    """查找 Playwright 已下载的 Chromium 可执行文件（不触发下载）。"""
    candidates = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        candidates.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "ms-playwright")
    for root in candidates:
        if not root.is_dir():
            continue
        for pattern in ("chromium-*/chrome-win/chrome.exe", "chromium_headless_shell-*/chrome-win/headless_shell.exe"):
            found = sorted(root.glob(pattern))
            if found:
                return found[-1]
    return None
