"""Packaged Pgy local service entry point (PyInstaller sidecar).

打包后这个进程没有控制台（--noconsole），因此这里的每条兜底路径都必须把证据写进
文件，否则用户只会看到「客户端打不开、也没有任何提示」：

- 启动期任何异常 → ``<数据目录>/pgy_logs/pgy-startup-error.log`` + 正常日志文件；
- 端口被占用 / 端口落在 Windows 保留区间 → 明确写出原因与排查命令；
- 父进程（桌面客户端）退出后自动退出，避免留下占着 18081 端口的僵尸进程
  （表现为「重启客户端也没用」）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from pathlib import Path

DEFAULT_PORT = 18081


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) / "Pgy" if base else Path.home() / ".pgy"
    root.mkdir(parents=True, exist_ok=True)
    return root


os.environ.setdefault("PGY_DATA_DIR", str(_data_dir()))

from backend.services import mio_logging  # noqa: E402  必须在数据目录确定之后导入

log = logging.getLogger("pgy.sidecar")


def _start_parent_watchdog() -> None:
    """父进程退出后自杀：Windows 上不会自动回收孤儿进程。

    为什么必须做：PyInstaller onefile 的启动器进程被客户端结束（或客户端崩溃）后，
    真正跑 uvicorn 的子进程会继续活着并占住端口。下一次启动时新进程绑定失败，
    用户看到的是「程序启动不了」，而实际上是一个看不见的旧进程在挡路。

    **退出只做这一件事**：释放端口。浏览器、页签、登录态一个都不动——用户口径
    （2026-09-24）：「关闭客户端仅仅是结束了自动化流程的接管，不要带着把浏览器关掉」。
    退出前收平台页签那一步已经删掉：它属于"动浏览器"，而且它的"最后一页别关"保护
    是按整台浏览器算的，别的窗口开着任何网页时，我们这个窗口的最后一个页签也会被关
    （关掉最后一个标签页＝关掉窗口，真机反馈过的"关客户端把浏览器也关了"）。残留的
    平台页签由**下一次接管时**的开工前清理负责收掉。
    """
    if os.name != "nt":
        return
    parent = os.getppid()
    if parent <= 0:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, parent)
    if not handle:
        log.warning("无法监听父进程（pid=%s），跳过孤儿进程保护", parent)
        return

    def watch() -> None:
        # 父进程句柄变为有信号 = 进程已退出；最多 5 秒延迟即可释放端口。
        while True:
            if kernel32.WaitForSingleObject(handle, 5000) == 0:
                # 用 os._exit 而不是正常退出：正常退出流程会去断连/收尾，可能拖住端口
                # 释放，正是要避免的情形。浏览器不在退出流程的责任范围内。
                os._exit(0)

    threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()
    log.info("已启用父进程守护（父进程 pid=%s）", parent)


def _fatal(title: str, detail: str) -> int:
    """记录致命启动错误并返回退出码。"""
    path = mio_logging.write_startup_error(title, detail)
    log.critical("%s\n%s", title, detail)
    try:
        # 打包后没有控制台，stderr 可能是 None；写日志失败不能盖住真正的错误。
        if sys.stderr is not None:
            sys.stderr.write(f"[pgy] {title}；详情见 {path}\n")
    except Exception:
        pass
    return 1


def main() -> int:
    mio_logging.setup_logging()
    mio_logging.install_excepthooks()
    log.info("本地服务启动中 pid=%s 数据目录=%s", os.getpid(), os.environ.get("PGY_DATA_DIR"))
    browser = mio_logging.browser_environment()
    log.info("浏览器环境：%s", browser)
    if not browser["available"]:
        # 不阻塞启动：登录时会给出可操作的错误，界面也提供「安装浏览器内核」入口。
        log.warning("未检测到可用的浏览器内核：%s", browser["detail"])
    _start_parent_watchdog()

    try:
        import uvicorn

        from backend.app import app
    except Exception:
        return _fatal("本地服务导入失败", traceback.format_exc())

    port = int(os.environ.get("PGY_PORT") or DEFAULT_PORT)
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            log_config=None,
        )
    except SystemExit as exc:
        # uvicorn 绑定失败时自己 sys.exit(1)；这里也要留下可读的启动错误文件，
        # 因为此时「设置 → 运行日志」还打不开。
        if exc.code in (0, None):
            raise
        return _fatal(
            f"本地服务无法监听 127.0.0.1:{port}（退出码 {exc.code}）",
            "通常是端口被占用（上一次未退出的本地服务）或端口落在 Windows 保留区间。\n"
            f"排查：netsh int ipv4 show excludedportrange protocol=tcp\n"
            f"或结束占用 {port} 端口的进程后重试。",
        )
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        hint = ""
        if winerror == 10013:
            hint = (f"\n端口 {port} 落在 Windows 保留区间或没有绑定权限。"
                    "排查：netsh int ipv4 show excludedportrange protocol=tcp")
        elif winerror in (10048, 10049):
            hint = f"\n端口 {port} 已被其他进程占用（可能是上一次未退出的本地服务）。"
        return _fatal(f"本地服务无法监听 127.0.0.1:{port}", f"{traceback.format_exc()}{hint}")
    except Exception:
        return _fatal("本地服务异常退出", traceback.format_exc())
    log.info("本地服务已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
