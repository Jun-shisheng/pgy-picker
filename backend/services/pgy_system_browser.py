"""系统浏览器接入：用电脑上已安装的 Edge 接管用户日常那个浏览器窗口。

实测结论（2026-09-23，用户机器 Edge 146）：
  把 Edge 全部关掉之后，用**默认配置目录** + `--remote-debugging-port` 启动是**可行的**
  （端口正常出现，接管的窗口里就是用户自己的 24 条小红书 Cookie、83KB 书签、10 个扩展）。
  也就是说 Chrome 136 起那条「默认配置目录不给开调试端口」的禁令，Edge 146 **没有**照搬；
  但**不能保证以后不跟进**，所以这里的做法是「先试主配置、验证端口真的起来了再用，
  起不来就如实回落到专属配置」。

因此本模块提供两种接入形态：
  1) **主配置（默认优先）**：`%LocalAppData%\\Microsoft\\Edge\\User Data`——就是用户日常
     那个 Edge：同一个配置、同一份登录态/书签/扩展，不会有第二个任务栏图标。
     代价：接管必须发生在 Edge **启动那一刻**（单例锁 + 端口只能在启动时打开），
     所以 Edge 已经在跑时需要先把它温和关掉再重新打开（可配置为提醒用户手动关）。
  2) **专属配置（回落）**：`profiles/<账号>`（ProfileManager 提供）——完全不碰用户数据，
     但窗口里没有用户的个人资料，且任务栏会多一个 Edge 图标。

边界条件：
- 只有在端口探测成功（`/json/version` 返回 Chromium 家族的 Browser 串）时才认为
  「可以接管」；探测不到一律重新启动，绝不盲连一个来路不明的端口；
- 判断「新进程是不是被已有实例顶掉」看进程是否很快退出（命令转发），而不是猜；
- 失败必须抛出可读异常，由上层转成用户能看懂的一句话，而不是笼统的 503；
- 本模块只做「发现 / 启动 / 探测 / 必要时温和关闭」，不认识页面语义。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import socket
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlparse
from typing import Any

from backend.core.pgy_core import ROOT

log = logging.getLogger(__name__)

# 模式开关：system=用电脑上的 Edge（默认）；managed=旧行为（Playwright 自带的
# 持久化上下文，内核优先仍取 Edge，但整套窗口都由 Playwright 托管）。
MODE_ENV = "MIO_BROWSER_MODE"
MANAGED_MODES = {"managed", "isolated", "profile", "own", "builtin", "playwright"}
# 主配置接管策略：auto=Edge 在运行时由程序接管（先温和关闭、必要时强制关闭，再带端口
# 重新打开；默认，客户端会在界面上提示用户"登录会关闭 Edge 窗口"）；
# prompt=不关用户的浏览器，只返回一句让他自己关的可操作提示；
# off=只用专属配置（完全不碰用户日常的浏览器）。
MAIN_PROFILE_ENV = "MIO_MAIN_PROFILE"
MAIN_PROFILE_MODES = {"auto": "auto", "prompt": "prompt", "manual": "prompt",
                      "off": "off", "0": "off", "false": "off", "no": "off"}
# 温和关闭（等于用户点右上角 ×）之后还不退出的，再等这么久就强制结束：
# Edge 的「关闭多个标签页确认」弹窗会挡住温和关闭，而界面已经明确提示过用户
# 「登录会关闭所有 Edge 窗口」，所以这里允许兜底强杀（登录态/书签/历史不受影响，
# 未保存的表单内容会丢——这正是要在界面上提示用户的原因）。
#
# 但强杀要尽量少用（用户 2026-09-23 要求）：被强杀的 Edge 会判定"上次异常退出"，
# 下次启动时把上一轮的页面**恢复**回来（用户看到的"垃圾页"）。所以：
#   * 我们自己带调试端口的实例一律先走 CDP `Browser.close`（正常退出）；
#   * 温和关闭的兜底时间从 3 秒放宽到 10 秒——那个确认框是**给人点的**（窗口全程可见，
#     用户点一下「关闭所有标签页」就是一次正常退出），只有他确实没点才强杀。
CLOSE_FORCE_AFTER_SECONDS = 10.0
# 指定调试端口（默认自动挑一个空闲端口；固定端口便于排障时手动接管）。
PORT_ENV = "MIO_EDGE_DEBUG_PORT"
# 指定 Edge 可执行文件（默认自动发现）。
EXECUTABLE_ENV = "MIO_BROWSER_EXECUTABLE"
# 可选：给自动化浏览器显式指定代理。默认走系统代理（与日常 Edge 一致）；
# 如果用户的默认浏览器装了**代理扩展**（系统代理里没有），专属配置不会带那个扩展，
# 表现就是「默认浏览器能开、程序里的浏览器打不开」。这时设
#   set PGY_BROWSER_PROXY=http://127.0.0.1:7890
# 再启动即可（具体地址看用户自己的代理软件）。
PROXY_ENV = "PGY_BROWSER_PROXY"

# 端口探测与启动等待上限。Edge 冷启动（要读几十 MB 的配置目录）实测 2–8 秒，
# 25 秒余量足够；超时必须报错，绝不无限等。
PROBE_TIMEOUT_SECONDS = 1.5
# 热路径（"点登录"前的状态查询、接管前判断上一轮端口还在不在）用更短的超时：
# 健康的调试端点在回环上是毫秒级响应；端口已经死了的时候，等满 1.5 秒只会让用户
# 觉得"点了登录半天没反应"（真机实测：探测一个已死端口正好要 1573 毫秒）。
PROBE_FAST_TIMEOUT_SECONDS = 0.5
STARTUP_TIMEOUT_SECONDS = 25.0
# 温和关闭用户的 Edge 之后，等它真正退出的上限（超过就不再硬来，改为如实报错）。
CLOSE_WAIT_SECONDS = 25.0
# 启动命令生效的判据窗口（秒）：超过它、端口还没开、**进程表里也没有任何一个 msedge
# 带着我们的调试端口**，就认定这次启动没生效，不再傻等满 STARTUP_TIMEOUT_SECONDS。
# 真机 2026-09-24 11:01：Edge 的「启动增强」后台实例把启动命令吞掉，只留下一个
# `--no-startup-window` 进程活着——"进程还活着＝没被转发"的老判据失效，工作线程干等 25 秒，
# 用户看到的是"浏览器打开了却什么都不发生"。
LAUNCH_FLAG_GRACE_SECONDS = 6.0
# 主配置（用户日常 Edge）只是「顺带用你已有登录态」的加分项：接不上时必须**秒级**
# 回落到专属配置继续登录，绝不耗掉用户一分钟。真机 2026-09-29（虚拟机，0.9.7）反馈：
# 主配置起不来时走满「25 秒等待 × 2 次尝试 + 收尾」才切换，用户盯着「打开了两个页面
# 却什么都不做」。所以主配置的启动等待用这个短预算、且只试一次（2.5 秒转发判据、
# 6 秒无端口进程判据仍会更早放弃）；专属配置是必须成功的正路，保持 25 秒 + 自愈重试。
MAIN_PROFILE_STARTUP_TIMEOUT_SECONDS = 10.0

# 端点记录：只用于「同一个配置 → 同一个窗口」的重连，内容不含任何凭据
# （只有端口、窗口号、配置目录与浏览器版本串）。存放在我们自己的数据目录，
# 不往用户的浏览器配置目录里写任何文件。
RECORD_NAME = "pgy_browser_endpoint.json"
RECORD_DIR = ROOT / "browser-endpoints"


class SystemBrowserUnavailable(RuntimeError):
    """系统 Edge 不可用（未安装 / 端口被占用 / 启动后没有开端口）。"""


class SystemBrowserBusy(RuntimeError):
    """该账号的自动化配置目录已经被另一个 Edge 实例占用。"""


@dataclass(frozen=True)
class EdgeEndpoint:
    """一个可接管的系统浏览器端点。"""

    cdp_url: str
    port: int
    user_data_dir: str
    browser: str = ""
    # True=本次由我们启动；False=接管了上一轮留下的同一个浏览器。
    launched: bool = False
    pid: int | None = None
    # main=用户日常的主配置；dedicated=自动化专属配置。
    profile_kind: str = "dedicated"
    # 自动化自己的那个窗口号（主配置模式下用它把用户自己的页签排除在回收范围外）。
    window_id: int | None = None

    @property
    def summary(self) -> str:
        label = "用户日常配置" if self.profile_kind == "main" else "专属配置"
        return f"{self.browser or 'Edge'} @127.0.0.1:{self.port}（{label}，{'新启动' if self.launched else '已接管'}）"


def browser_mode() -> str:
    """返回 `system` 或 `managed`；非法取值按默认（system）处理。"""
    value = os.environ.get(MODE_ENV, "system").strip().lower()
    return "managed" if value in MANAGED_MODES else "system"


def main_profile_mode() -> str:
    """主配置接管策略：auto（默认）/ prompt / off。"""
    value = os.environ.get(MAIN_PROFILE_ENV, "auto").strip().lower()
    return MAIN_PROFILE_MODES.get(value, "auto")


# 「这台电脑接不了日常配置」的记忆：进程内 + 落盘（数据目录里的一行记录）。
#
# 为什么需要它：接管日常配置要先把用户的 Edge 关掉再带端口启动，而有些机器上我们的
# 启动命令会被 Edge 自己的实例吞掉（真机 2026-09-24 同事机器：窗口开了、空白页，
# 但没有任何进程带上我们的调试端口）。失败**一次**就够说明这台机器不行了——再试只会
# 又关一遍用户的 Edge、再白等 30 秒。所以**落盘记住**（不然每次重启客户端都要重演一遍，
# 用户看到的就是"装完还是有别的问题"），只在超过 _MAIN_PROFILE_RETRY_AFTER 后才重试一次，
# 给 Edge 升级 / 策略变化留机会；「清理数据」会把这个记录一起清掉。
_MAIN_PROFILE_UNAVAILABLE = ""
_MAIN_PROFILE_MARK_NAME = "main-profile-unavailable.json"
_MAIN_PROFILE_RETRY_AFTER = 7 * 24 * 3600


def _main_profile_mark_path() -> Path:
    return RECORD_DIR / _MAIN_PROFILE_MARK_NAME


def mark_main_profile_unavailable(reason: str) -> None:
    global _MAIN_PROFILE_UNAVAILABLE
    _MAIN_PROFILE_UNAVAILABLE = reason or "接管日常配置失败"
    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        _main_profile_mark_path().write_text(
            json.dumps({"reason": _MAIN_PROFILE_UNAVAILABLE, "at": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        # 记不住就退回"本次进程内有效"：功能不受影响，只是重启后会再试一次。
        log.info("记录「接不了日常配置」失败（%s），本次进程内仍然按接不了处理", type(exc).__name__)


def main_profile_unavailable() -> str:
    """空串＝这台电脑还能试主配置；非空＝已经失败过，直接走专属配置（附失败原因）。"""
    global _MAIN_PROFILE_UNAVAILABLE
    if _MAIN_PROFILE_UNAVAILABLE:
        return _MAIN_PROFILE_UNAVAILABLE
    try:
        payload = json.loads(_main_profile_mark_path().read_text(encoding="utf-8"))
        at = float(payload.get("at") or 0)
    except Exception:
        return ""
    if time.time() - at > _MAIN_PROFILE_RETRY_AFTER:
        try:
            _main_profile_mark_path().unlink(missing_ok=True)
        except OSError:
            pass
        log.info("「接不了日常配置」的记录已过期（超过 %d 天）：重试一次接管日常配置",
                 _MAIN_PROFILE_RETRY_AFTER // 86400)
        return ""
    _MAIN_PROFILE_UNAVAILABLE = str(payload.get("reason") or "接管日常配置失败（记录）")
    return _MAIN_PROFILE_UNAVAILABLE


def default_user_data_dir(engine: str = "edge") -> Path | None:
    """用户日常浏览器的配置目录（Windows 标准位置）。

    这是「用户自己的浏览器数据」所在地：读它意味着能用上他现有的登录态与书签，
    也意味着任何清理动作都必须绕开它（上层据此区分 main / dedicated 两种模式）。
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    if engine == "chrome":
        return Path(base) / "Google" / "Chrome" / "User Data"
    return Path(base) / "Microsoft" / "Edge" / "User Data"


def _edge_candidates() -> list[str]:
    """Edge 的候选路径（优先级从高到低）：**先绝对路径、再注册表、最后才 which**。

    顺序是有性能原因的：`shutil.which` 要把 PATH 里每个目录都 stat 一遍（实测单次
    上百毫秒），而这条解析在"点登录"前的状态查询里也要走；Windows 上 Edge 大多装在
    固定位置，先 stat 三个绝对路径就是微秒级；读注册表同样是微秒级，比 which 快得多，
    还能覆盖"装在非标准位置"的机器（真机 2026-09-24 同事机器）。
    """
    return [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        *_registered_edge_paths(),
        shutil.which("msedge") or "",
    ]


def _chrome_candidates() -> list[str]:
    """Edge 确实不存在时的备选：同一套 CDP 接管方式对 Chrome 同样成立。"""
    return [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        shutil.which("chrome") or "",
    ]


def resolve_browser_executable() -> tuple[str, str] | None:
    """自动识别要用的浏览器：返回 (可执行文件路径, 引擎名)。

    顺序（新电脑上不需要用户做任何配置）：
      1. `MIO_BROWSER_EXECUTABLE` 显式指定的路径（最高优先级，方便指定绿色版/非标准安装）；
      2. 系统 Edge（msedge.exe）——产品以 Edge 为主；
      3. 系统 Chrome（chrome.exe）——本机没有 Edge 时的等价备选；
      4. 都没有 → None，由上层回落到 Playwright 自带内核并如实提示。
    """
    configured = os.environ.get(EXECUTABLE_ENV, "").strip()
    if configured and os.path.isfile(configured):
        name = os.path.basename(configured).casefold()
        return configured, "edge" if "msedge" in name else "chrome"
    for path in _edge_candidates():
        if path and os.path.isfile(path):
            return path, "edge"
    for path in _chrome_candidates():
        if path and os.path.isfile(path):
            return path, "chrome"
    return None


def system_browser_available() -> bool:
    """当前平台是否可以用系统浏览器跑自动化（Windows + 装好 Edge/Chrome + 未被显式关闭）。"""
    if browser_mode() != "system":
        return False
    if os.name != "nt":
        # 只在 Windows 上按系统浏览器接入；其它平台回落受管内核。
        return False
    return resolve_browser_executable() is not None


def browser_report() -> dict[str, Any]:
    """给「设置 → 系统状态」用的浏览器自述：模式、引擎、路径、是否可用。"""
    mode = browser_mode()
    resolved = resolve_browser_executable()
    if mode != "system":
        return {"mode": mode, "engine": "", "path": "", "available": False,
                "detail": "当前使用受管浏览器内核（MIO_BROWSER_MODE=managed）"}
    if os.name != "nt":
        return {"mode": mode, "engine": "", "path": "", "available": False,
                "detail": "非 Windows 平台，回落受管浏览器内核"}
    if not resolved:
        return {"mode": mode, "engine": "", "path": "", "available": False,
                "detail": "没有找到系统 Edge/Chrome；可用 MIO_BROWSER_EXECUTABLE 指定路径"}
    path, engine = resolved
    label = "Microsoft Edge" if engine == "edge" else "Google Chrome"
    return {"mode": mode, "engine": engine, "path": path, "available": True,
            "detail": f"使用电脑上已安装的 {label}（自动识别，可用 MIO_BROWSER_EXECUTABLE 覆盖）"}


def extension_flags() -> list[str]:
    """系统 Edge 的启动参数（可见窗口，不做任何隐藏/离屏处理）。

    保留这组「关闭后台节流」的开关：窗口被最小化或压在别的窗口后面时，Chromium
    会把渲染进程降频，依赖定时器/rAF 的平台异步渲染链会明显变慢；RPA 需要看到
    与人手一致的速度。`--disable-popup-blocking` 是平台在新标签页打开登录/表单
    所必需的（Playwright 默认参数里有，自己启动时必须显式给）。
    """
    flags = [
        "--no-first-run",
        "--no-default-browser-check",
        # 不显示"上次异常退出，是否恢复页面"的气泡：我们接管的是用户日常的浏览器，
        # 被强杀过的实例下次启动会把这个气泡和一堆旧标签页一起带回来（用户看到的"垃圾页"）。
        "--hide-crash-restore-bubble",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    proxy = (os.environ.get(PROXY_ENV) or "").strip()
    if proxy:
        flags.append(f"--proxy-server={proxy}")
        # 本机调试端口/回环地址永远不走代理，否则可能连自己的 CDP 都连不上。
        flags.append("--proxy-bypass-list=127.0.0.1;localhost;<-loopback>")
        log.info("自动化浏览器使用显式代理：%s", proxy)
    return flags


def probe_endpoint(port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict | None:
    """探测端口上是否有一个 Chromium 家族（Edge/Chrome）的调试端点。

    只读 `/json/version`：拿不到合法 JSON、或拿不到 Browser 版本串，都算「不是
    可接管的端点」。这样即使端口被别的服务占用，也不会把自动化接错地方。
    """
    try:
        with urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    browser = str(payload.get("Browser") or payload.get("browser") or "").strip()
    if not browser or "://" in browser:
        return None
    return payload


def port_open(port: int, *, timeout: float = 0.15) -> bool:
    """端口上有没有东西在听（毫秒级，比 HTTP 探测便宜一个数量级）。

    只回答"有没有监听"：真正的端点身份仍由 `probe_endpoint`（HTTP）确认。
    存在的理由：已死端口的 HTTP 探测会等满超时（真机实测 1573 毫秒），而 TCP 连接
    在 150 毫秒内必给答案（活着＝1 毫秒内连上，死了＝RST 或立刻超时）。
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _port_is_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def _free_port() -> int:
    """让系统随便给一个空闲端口（避免与其它调试工具抢 9222）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def record_path(user_data_dir: Path | str) -> Path:
    """端点记录文件的位置：放在**我们自己的数据目录**里，按配置目录哈希命名。

    刻意不写进浏览器配置目录：主配置模式接管的是用户日常那份数据，除了浏览器自己
    的东西，我们一个文件都不该往里放（这也是"读它但不改它"的具体做法）。
    """
    key = hashlib.sha256(str(Path(user_data_dir)).casefold().encode("utf-8")).hexdigest()[:16]
    return RECORD_DIR / f"{key}.json"


def read_record(user_data_dir: Path | str) -> dict | None:
    """读回上一轮记下的端口；损坏或不存在都按「没有记录」处理。"""
    try:
        payload = json.loads(record_path(user_data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_record(user_data_dir: Path | str, endpoint: EdgeEndpoint) -> None:
    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        record_path(user_data_dir).write_text(
            json.dumps({"port": int(endpoint.port), "browser": endpoint.browser,
                        "user_data_dir": str(user_data_dir), "at": int(time.time()),
                        "profile_kind": endpoint.profile_kind,
                        "window_id": endpoint.window_id},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as exc:
        # 记录写不进去只影响「下次能不能复用同一个窗口」，不影响本次运行。
        log.info("浏览器端点记录写入失败（下次会重新启动窗口）：%s", type(exc).__name__)


def update_record_window(user_data_dir: Path | str, window_id: int | None) -> None:
    """把「自动化窗口号」补进记录：下次接管时靠它区分我方窗口与用户自己的页签。"""
    record = read_record(user_data_dir)
    if not record:
        return
    record["window_id"] = int(window_id) if window_id else None
    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        record_path(user_data_dir).write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    except OSError:
        pass


def clear_record(user_data_dir: Path | str) -> None:
    try:
        record_path(user_data_dir).unlink(missing_ok=True)
    except OSError:
        pass


def edge_main_processes() -> list[dict]:
    """列出 msedge 的「主进程」（pid / 是否有窗口 / 用的配置目录 / 可执行文件路径）。

    渲染进程带 `--type=`，不算主进程；Edge 的「启动增强」会留一个没有窗口但仍
    持有配置目录的进程，温和关闭时必须把它也算进去，否则永远关不干净。
    非 Windows 或取不到进程表时返回空列表（调用方按「没有占用」处理）。

    `exe` 是**正在跑的那个 Edge 自己的可执行文件路径**：Edge 装在非标准位置时
    （不在 Program Files / LocalAppData 那三个固定路径里），这是唯一可靠的答案。
    """
    if os.name != "nt":
        return []
    # 浏览器不一定是 Edge：产品支持回落到系统 Chrome（resolve_browser_executable
    # 的候选顺序就是 Edge → Chrome），进程枚举也必须把 Chrome 算进来，否则
    # 「只装了 Chrome」的电脑上占用判断/释放都会失准（自检 2026-09-28）。
    script = ("$ErrorActionPreference='SilentlyContinue';"
              "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | ForEach-Object {"
              "  $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue;"
              "  $h = 0; if ($p -and $p.MainWindowHandle -ne 0) { $h = 1 };"
              "  [pscustomobject]@{ pid = $_.ProcessId; window = $h; cmd = $_.CommandLine;"
              "                     exe = $_.ExecutablePath }"
              "} | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=45,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        payload = json.loads((result.stdout or "").strip() or "[]")
    except Exception as exc:
        log.info("读取 Edge 进程表失败（按未占用处理）：%s", type(exc).__name__)
        return []
    if isinstance(payload, dict):
        payload = [payload]
    items: list[dict] = []
    for entry in payload if isinstance(payload, list) else []:
        command = str(entry.get("cmd") or "")
        if "--type=" in command:
            continue
        given = ""
        if "--user-data-dir=" in command:
            given = command.split("--user-data-dir=", 1)[1].split(" --", 1)[0].strip().strip('"')
        items.append({"pid": int(entry.get("pid") or 0), "window": int(entry.get("window") or 0),
                      "user_data_dir": given, "cmd": command,
                      "exe": str(entry.get("exe") or "")})
    return items


def running_edge_executable() -> str:
    """正在运行的 Edge 用的是哪个可执行文件（没有在跑就返回空）。

    为什么需要它：接管日常配置时要**用他电脑上真正在跑的那个 Edge**去启动。装在
    非标准位置的 Edge（不在三个固定路径、也不在 PATH 里）光靠固定路径找不到，
    而正在跑的进程自己带着答案。
    """
    for item in edge_main_processes():
        path = str(item.get("exe") or "")
        if path and os.path.isfile(path):
            return path
    return ""


def _registered_edge_paths() -> list[str]:
    """注册表登记的 Edge 路径（App Paths）：装在非标准位置也能找到。

    `winreg` 是标准库、读注册表是微秒级，所以放进常规候选里没有性能负担。
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows 不会走到
        return []
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
    )
    found: list[str] = []
    for root, key in keys:
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "")
        except OSError:
            continue
        if value and os.path.isfile(value):
            found.append(str(value))
    return found


def edge_windows_native() -> list[dict]:
    """**毫秒级**列出 msedge.exe 的顶层窗口（pid / 是否可见 / 类名 / 标题）。

    为什么需要它：判断"用户的 Edge 是不是开着"以前走 PowerShell/CIM 取命令行，
    单次约 2 秒——而这条判断在登录前（界面问"要不要关闭 Edge"）和接管等待循环里都会
    调用，用户体感就是"打开浏览器很慢"。这里用原生 EnumWindows +
    QueryFullProcessImageNameW，实测 300+ 个窗口只要 **4 毫秒**。
    （取命令行仍然只有 PowerShell 那条路，只在需要精确判断"哪个进程占着某个
    专属配置目录"时才用；默认配置目录靠这个就够了。）
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        rows: list[dict] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not process:
                return True
            try:
                size = wintypes.DWORD(32768)
                path = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    if os.path.basename(path.value).casefold() in ("msedge.exe", "chrome.exe"):
                        cls = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(hwnd, cls, len(cls))
                        rows.append({"pid": int(pid.value),
                                     "window": 1 if user32.IsWindowVisible(hwnd) else 0,
                                     "cls": cls.value, "hwnd": int(hwnd)})
            finally:
                kernel32.CloseHandle(process)
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return rows
    except Exception as exc:
        log.info("原生窗口枚举失败（回落进程表查询）：%s", type(exc).__name__)
        return []


def windowed_edge_processes() -> list[dict]:
    """有可见顶层窗口的 msedge 进程（去重）。用于"用户的 Edge 是不是开着"这个快判断。"""
    seen: dict[int, dict] = {}
    for item in edge_windows_native():
        if not item["window"]:
            continue
        seen.setdefault(item["pid"], {"pid": item["pid"], "window": 1, "user_data_dir": "", "cmd": ""})
    return list(seen.values())


def focus_edge_window_for_endpoint(user_data_dir: Path | str, port: int | None = None) -> bool:
    """按「配置目录 / 调试端口」找到**自动化那个 Edge 主进程**，再把它的窗口抬到前台。

    不能直接用启动时的 Popen pid：Edge 可能把启动器变成子进程，真正持有窗口的是
    另一个 msedge 主进程（实测启动 pid=19820，窗口属于 pid=38660）。
    """
    profile = str(user_data_dir).rstrip("\\").casefold()
    try:
        rows = edge_main_processes()
    except Exception:
        rows = []
    for item in rows:
        cmd = str(item.get("cmd") or "")
        if port and f"--remote-debugging-port={int(port)}" in cmd:
            return focus_edge_window(int(item.get("pid") or 0))
        given = str(item.get("user_data_dir") or "").rstrip("\\").casefold()
        if profile and given and profile == given:
            return focus_edge_window(int(item.get("pid") or 0))
    return False


def focus_edge_window(pid: int | None = None) -> bool:
    """把 Edge 窗口切到前台（必要时从最小化恢复）。

    为什么需要：平台的验证页/登录页在自动化 Edge 里，用户经常找不到那个窗口
    （被客户端挡住或最小化了）。Playwright 的 `page.bring_to_front()` 只切页签，
    不保证把 OS 窗口抬到最前；这里补一刀 Win32 的 SetForegroundWindow。
    2026-11-23 起小红书扫码链路已移除；这个工具保留给需要人工处理的验证窗口。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        rows = [row for row in edge_windows_native() if row.get("window") and row.get("hwnd")]
        if pid:
            rows = [row for row in rows if int(row.get("pid") or 0) == int(pid)]
        if not rows:
            return False
        hwnd = int(rows[0]["hwnd"])
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE：最小化的先还原
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception as exc:
        log.info("切换浏览器窗口到前台失败：%s", type(exc).__name__)
        return False


def processes_using(user_data_dir: Path | str) -> list[dict]:
    """正在占用指定配置目录的 Edge 主进程（没写 --user-data-dir 的算默认配置）。"""
    target = str(user_data_dir).rstrip("\\").casefold()
    owners = []
    for item in edge_main_processes():
        given = (item.get("user_data_dir") or "").rstrip("\\").casefold()
        if given == target or (not given and target.endswith("edge\\user data")):
            owners.append(item)
    return owners


def _cdp_ws_send(ws_url: str, payload: dict, *, timeout: float = 5.0) -> str:
    """极简 WebSocket 客户端：给 CDP 发一帧、读一帧（不依赖 Playwright）。

    为什么不用 Playwright：本模块要在**没有会话**的场景关闭浏览器（接管前/手动释放），
    那里没有 Playwright 实例；而且带 asyncio 的同步 API 不能在已有 Playwright 的线程里
    再开一个。这里只用标准库完成握手与一帧收发，够发 `Browser.close` 用。
    """
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(handshake.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 未升级：" + head.split(b"\r\n")[0].decode("latin-1"))
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        if len(data) < 126:
            header = bytes([0x81, 0x80 | len(data)])
        elif len(data) < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", len(data))
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        try:
            return sock.recv(4096).decode("utf-8", "replace")
        except Exception:
            # 浏览器正在关闭，连接会直接断掉——这正是我们要的结果。
            return ""
    finally:
        sock.close()


def cdp_browser_close(port: int, *, timeout: float = 5.0) -> bool:
    """用 CDP `Browser.close` **正常关闭**浏览器（返回是否已把命令发出去）。

    真机实测（2026-09-23，临时配置目录对照实验 `build/probe_cdp_graceful_close.py`）：
      * `Browser.close` → 进程自己退出，profile 里 `exit_type=Normal`；
      * `taskkill /F`    → 不会留下 Normal 标记。
    而 Edge 判定"上次是异常退出"时，下次启动会**恢复上次会话**——用户看到的"垃圾页"
    就是这么来的。所以凡是能走 CDP 的场合，一律先走这条正常关闭。
    """
    if not port_open(port, timeout=0.3):
        return False
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
            ws_url = str(json.loads(response.read().decode("utf-8")).get("webSocketDebuggerUrl") or "")
    except Exception as exc:
        log.info("读不到调试端口信息（%s）：%s", port, type(exc).__name__)
        return False
    if not ws_url:
        return False
    try:
        _cdp_ws_send(ws_url, {"id": 1, "method": "Browser.close"}, timeout=timeout)
        log.info("已发出 Browser.close（正常关闭调试实例，端口 %s）", port)
        return True
    except Exception as exc:
        log.info("Browser.close 未成功（%s）：%s", port, type(exc).__name__)
        return False


def debug_port_of(user_data_dir: Path | str) -> int | None:
    """该配置目录上**正在跑的调试端口**：先看我们自己的记录，再从进程命令行里读。

    为什么要两条路：记录文件可能被清理（用户点「清理数据」/换机器），但实例还开着；
    这时从 `--remote-debugging-port=` 里读出来，仍然可以走正常关闭而不是强杀。
    """
    recorded = _recorded_port(Path(user_data_dir))
    if recorded and port_open(recorded, timeout=0.2):
        return recorded
    for item in edge_main_processes():
        if not _same_profile(item.get("user_data_dir"), user_data_dir):
            continue
        match = re.search(r"--remote-debugging-port=(\d+)", str(item.get("cmd") or ""))
        if match:
            return int(match.group(1))
    return None


def graceful_close_via_cdp(user_data_dir: Path | str, *, wait: float = 20.0) -> bool:
    """优先用正常关闭：对我们自己的调试实例发 `Browser.close`，等它自己退出。

    找不到端口／没在跑就返回 False，交给调用方走后面的温和关闭与兜底。
    """
    port = debug_port_of(user_data_dir)
    if not port or not port_open(port, timeout=0.3):
        return False
    log.info("先尝试正常关闭（CDP Browser.close，端口 %s）", port)
    if not cdp_browser_close(port):
        return False
    deadline = time.monotonic() + max(1.0, wait)
    while time.monotonic() < deadline:
        if not port_open(port, timeout=0.2) and not processes_using(user_data_dir):
            log.info("浏览器已正常退出（CDP），未使用强杀")
            return True
        time.sleep(0.3)
    return not processes_using(user_data_dir)


def _same_profile(given: str | None, wanted: Path | str) -> bool:
    if not given:
        return False
    return str(given).rstrip("\\").casefold() == str(wanted).rstrip("\\").casefold()


def graceful_close_edge(user_data_dir: Path | str, *, windowless_only: bool = False,
                        wait: float = CLOSE_WAIT_SECONDS,
                        force_after: float = CLOSE_FORCE_AFTER_SECONDS,
                        owners: list[dict] | None = None) -> bool:
    """关闭占用该配置目录的 Edge，返回是否关干净。

    顺序（用户 2026-09-23 要求：**先正常关闭，不行再强杀**，别一直强杀触发页面恢复）：
      1) **CDP `Browser.close`**：如果是我们自己带调试端口启动的实例，直接让它自己
         正常退出（真机实测会留下 `exit_type=Normal`，下次启动不会恢复上次会话）；
      2) **温和关闭**（`taskkill` 不带 `/F`，等于用户点右上角 ×）：Edge 自己保存会话，
         书签/登录态/历史一个都不会丢；
      3) 还是没退（多半停在「关闭多个标签页确认」上）才**兜底强杀**——这一段刻意留得
         比较久（`force_after`），因为那个确认框是给人点的：窗口全程可见，用户点一下
         「关闭所有标签页」就能走正常退出，比强杀干净得多。

    无窗口的「启动增强」预加载进程没有任何窗口、没有可丢的数据，直接结束掉
    （否则"接管只能在启动那一刻发生"这条约束永远满足不了）。`windowless_only=True`
    时只结束无窗口的进程，绝不碰用户正在看的窗口。
    """
    if not windowless_only and graceful_close_via_cdp(user_data_dir):
        clear_record(user_data_dir)
        return True
    owners = processes_using(user_data_dir) if owners is None else list(owners)
    if not owners:
        return True
    if windowless_only:
        owners = [item for item in owners if not item["window"]]
        if not owners:
            return False
    log.info("接管前先让出该配置目录：%s", [(item["pid"], "有窗口" if item["window"] else "无窗口")
                                          for item in owners])
    for item in owners:
        flags = ["/PID", str(item["pid"]), "/T"] + ([] if item["window"] else ["/F"])
        subprocess.run(["taskkill", *flags],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    forced = False
    force_at = time.monotonic() + max(0.0, force_after)
    deadline = time.monotonic() + wait
    targets = {int(item["pid"]) for item in owners}
    while time.monotonic() < deadline:
        if windowless_only:
            # 目标本来就是无窗口进程：原生窗口枚举帮不上忙，只能精确查（这条路很少走）
            if not processes_using(user_data_dir):
                return True
        else:
            # 有窗口的目标：用**毫秒级**原生枚举判断退没退；全退干净就直接返回，
            # 不再每次都去查进程表（PowerShell/CIM 单次约 2 秒，白等好几秒）。
            alive = {int(item["pid"]) for item in edge_windows_native()
                     if int(item["pid"]) in targets and item["window"]}
            if not alive:
                return True
            if not forced and time.monotonic() >= force_at:
                forced = True
                # 直接对原来那批目标进程兜底：不再重查进程表（PowerShell 单次约 2 秒，
                # 而这里要处理的就是"刚才没退干净"的那几个 pid）。
                alive_pids = sorted(targets & {int(item["pid"]) for item in edge_windows_native()
                                               if item["window"]})
                if alive_pids:
                    log.warning("Edge 在 %.0f 秒内没有响应温和关闭（多半停在「关闭多个标签页确认」上）；"
                                "兜底强制结束：pid=%s（提示：窗口全程可见，点掉那个确认框就能走正常退出）",
                                force_after, ", ".join(str(pid) for pid in alive_pids))
                    for pid in alive_pids:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        time.sleep(0.2)
    return not processes_using(user_data_dir)


def launch_edge(executable: str, user_data_dir: Path | str, port: int,
                *, start_url: str = "about:blank") -> subprocess.Popen:
    """以「可被接管的系统 Edge」方式启动一个窗口。

    刻意不加 `--start-minimized` / 屏幕外坐标：用户要求全程看得见（可以自己最小化，
    但程序不替他把窗口藏起来）。
    刻意不传 URL（`start_url` 为空时不追加参数）：传了会在 Edge 恢复的会话里再加一个
    标签页，用户看到的就是「打开了两个页面」。

    进程以 detached 方式启动。注意：**它仍然是本进程的子进程**——所以客户端退出时
    绝不能对自己的进程树用 `taskkill /T`（那会把浏览器一起杀掉）；客户端侧已改成
    只按 PID 结束本地服务本身（见 frontend/src-tauri/src/lib.rs 的 kill_backend）。
    真机实测（2026-09-23）：`ShellExecute`/`os.startfile` 并不会改变父进程，换不来
    "脱离进程树"，因此这里保留 Popen 以便拿到进程句柄做转发判断与收尾。
    """
    args = [
        executable,
        f"--remote-debugging-port={int(port)}",
        f"--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={user_data_dir}",
        *extension_flags(),
    ]
    if start_url:
        args.append(start_url)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log.info("启动系统 Edge：端口 %s 配置目录 %s", port, user_data_dir)
    return subprocess.Popen(
        args,
        close_fds=True,
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_endpoint(port: int, timeout: float, process: subprocess.Popen | None = None,
                       known_pids: set[int] | None = None) -> dict | None:
    """等端口出现；**启动命令被转发时立刻放弃等待**。

    真机教训：Edge 已在运行时，我们的启动命令会被转交给已有实例、不会开端口——
    但老实现仍然傻等满 25 秒才去判断"是不是被转发了"。接管一次就白花 25 秒，
    用户体感就是"打开浏览器极慢"。两种判据：
      - 有 Popen 句柄：启动 2.5 秒后进程已退出且端口没出现 → 转发；
      - ShellExecute 启动（没有句柄）：启动 2.5 秒后**没有任何新的 msedge 进程出现**
        且端口没出现 → 转发。
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    while True:
        payload = probe_endpoint(port, timeout=min(PROBE_TIMEOUT_SECONDS, max(0.3, deadline - time.monotonic())))
        if payload is not None:
            return payload
        now = time.monotonic()
        if now - started >= 2.5:
            forwarded = False
            if process is not None and process.poll() is not None:
                forwarded = True
            elif process is None and known_pids is not None:
                current = {int(item["pid"]) for item in edge_windows_native()}
                forwarded = not (current - known_pids)
            if forwarded and probe_endpoint(port, timeout=PROBE_TIMEOUT_SECONDS) is None:
                log.info("启动命令被已有浏览器实例接管，不再等待端口 %.0f 秒", timeout)
                return None
        if now - started >= LAUNCH_FLAG_GRACE_SECONDS:
            # 第三种失败：命令生出了一个**没带调试端口**的 msedge 主进程（Edge 的「启动增强」
            # 后台实例把命令吞了）。判据换成事实——端口没开、进程表里没有一个 msedge 带我们的端口。
            # 只在进程表**读到了东西**时才据此放弃（查询失败返回空列表，不能当成"没进程"）。
            running = edge_main_processes()
            flag = f"--remote-debugging-port={port}"
            if running and not any(flag in str(item.get("cmd") or "") for item in running):
                log.warning("启动命令没有生效：%.0f 秒内没有任何 msedge 进程带上调试端口 %s"
                            "（多半被 Edge 的「启动增强」后台实例吞掉了），不再等待 %.0f 秒",
                            LAUNCH_FLAG_GRACE_SECONDS, port, timeout)
                return None
        if now >= deadline:
            return None
        time.sleep(0.3)


def _recorded_port(user_data_dir: Path | str) -> int | None:
    record = read_record(user_data_dir)
    if not record:
        return None
    try:
        port = int(record.get("port"))
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _pick_port() -> int:
    configured = os.environ.get(PORT_ENV, "").strip()
    port = int(configured) if configured.isdigit() and _port_is_free(int(configured)) else _free_port()
    # 端口已被别的程序占住时不硬抢：换一个空闲端口，接管逻辑不受影响。
    return _free_port() if probe_endpoint(port) is not None else port


def _launched_endpoint(port: int, directory: Path, payload: dict,
                       process: subprocess.Popen | None, profile_kind: str) -> EdgeEndpoint:
    """把「刚启动好的实例」记进端点记录并返回（两条启动路径共用一个出口）。"""
    endpoint = EdgeEndpoint(cdp_url=f"http://127.0.0.1:{port}", port=port,
                            user_data_dir=str(directory),
                            browser=str(payload.get("Browser") or ""),
                            launched=True, pid=process.pid if process is not None else None,
                            profile_kind=profile_kind)
    write_record(directory, endpoint)
    log.info("系统 Edge 已就绪：%s", endpoint.summary)
    return endpoint


def _kill_quietly(process) -> None:
    """收掉「启动后没开端口」的半成品实例：先温和（=点 ×，Edge 自己保存会话），4 秒不退才强杀。

    绝不给用户留一个停在新建标签页上的空白 Edge 窗口。
    """
    if process is None or process.poll() is not None:
        return
    subprocess.run(["taskkill", "/PID", str(process.pid), "/T"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.2)
    if process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        log.warning("浏览器启动后未开端口且未响应温和关闭，已强制清理该实例（pid=%s）", process.pid)
    else:
        log.info("浏览器启动后未开端口，已温和关闭该实例（pid=%s）", process.pid)


def _release_profile(user_data_dir: Path | str, *, allow_windowed: bool,
                     wait: float = CLOSE_WAIT_SECONDS) -> tuple[list[dict], int]:
    """把占用该配置目录的 Edge 请走；返回（仍然活着的占用者, 出手处理过的占用者数）。

    关闭本身走既有的 `graceful_close_edge`（CDP 正常关闭 → 温和关闭 → 到期兜底强杀，
    其中"关闭多个标签页确认"那个确认框是留给人点的，不能省）。这里只补两件它不做的：
      * 无窗口的「启动增强」后台进程**也算占用**——graceful_close_edge 会 /F 掉它们，
        但它只等有窗口的目标退干净，所以补一次"全部退干净"的确认；
      * 返回仍然活着的占用者，让调用方（启动前腾空）据此判断能不能启动。
    `allow_windowed=False` 时绝不碰有窗口的实例，只清无窗口的——调用方必须先自己判
    "有窗口就不能动"。
    """
    owners = processes_using(user_data_dir)
    if not owners:
        return [], 0
    targets = owners if allow_windowed else [item for item in owners if not item["window"]]
    if not targets:
        return processes_using(user_data_dir), 0
    graceful_close_edge(user_data_dir, windowless_only=not allow_windowed, wait=wait, owners=targets)
    remaining = processes_using(user_data_dir)
    leftovers = [item for item in remaining if not item["window"]]
    for item in leftovers:
        # 无窗口的后台预加载进程没有窗口、没有可丢的数据，但照样持有配置目录、
        # 照样能把我们的启动命令吞掉（真机 2026-09-24 11:01），所以必须确认它真的没了。
        subprocess.run(["taskkill", "/PID", str(item["pid"]), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if leftovers:
        time.sleep(1.0)
        remaining = processes_using(user_data_dir)
    if allow_windowed and not remaining:
        clear_record(user_data_dir)
    return remaining, len(targets)


def ensure_edge_endpoint(user_data_dir: Path | str, *, start_url: str = "about:blank",
                         timeout: float = STARTUP_TIMEOUT_SECONDS, profile_kind: str = "dedicated",
                         allow_restart: bool = False, attempts: int = 2) -> EdgeEndpoint:
    """拿到一个可接管的系统浏览器端点：优先接管上一轮留下的那个浏览器。

    顺序（每一步都用可验证的事实判断，不做猜测）：
      1. 配置目录里记着端口、且该端口确实还是 Chromium 调试端点 → 直接接管；
      2. **先腾空配置目录**（有窗口的按策略温和关闭、无窗口的「启动增强」后台进程直接清掉），
         再启动一个带端口的浏览器，等到端口真的起来才算成功；
      3. 端口始终不出现且新进程很快退出 → 说明该配置目录已被正在运行的浏览器占用
         （命令被转发）。此时：
           - `allow_restart=True`（主配置 + auto 策略）：清干净残留实例后**自愈重试一次**
             ——用户的登录态/书签/历史都在这份配置里，不会丢；
           - 否则抛出可操作的 SystemBrowserBusy，让用户自己关掉后重试。
    """
    directory = Path(user_data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # 找调试端口有两条路（与 `debug_port_of` 一致）：我们自己的记录文件，以及**进程命令行**
    # 里的 `--remote-debugging-port=`。真机 2026-09-24 09:36 事故：记录文件被「清理数据」
    # 删掉、而带端口的实例还开着，这里就当成"浏览器没在跑"→ 打了注定被转发的启动（5 秒）→
    # 让出 + 温和关闭等满 10 秒 → 强杀 → 重新启动，整个接管花了 28 秒（用户反馈「打开浏览器
    # 半天没有反应」）。命令行里读得到端口时直接接管，秒级完成，也不再折腾用户的窗口。
    recorded = debug_port_of(directory)
    if recorded is not None and port_open(recorded):
        if _recorded_port(directory) is None:
            log.info("该配置目录上已有带调试端口的实例（端口 %s，从进程命令行读到）：直接接管，"
                     "不再重启浏览器", recorded)
        # 先做便宜的一步：端口上确实有监听才去问它是不是 Chromium 端点。
        payload = probe_endpoint(recorded, timeout=PROBE_TIMEOUT_SECONDS)
        if payload is not None:
            record = read_record(directory) or {}
            window_id = record.get("window_id")
            return EdgeEndpoint(cdp_url=f"http://127.0.0.1:{recorded}", port=recorded,
                                user_data_dir=str(directory),
                                browser=str(payload.get("Browser") or ""), launched=False,
                                profile_kind=str(record.get("profile_kind") or profile_kind),
                                window_id=int(window_id) if window_id else None)
        # 上一轮的窗口已经关掉了：记录作废，重新启动。
        clear_record(directory)

    executable = resolve_browser_executable()
    if not executable:
        raise SystemBrowserUnavailable(
            "没有找到电脑上的 Edge/Chrome。请确认已安装 Microsoft Edge，"
            "或用 MIO_BROWSER_EXECUTABLE 指定浏览器可执行文件的完整路径。")
    executable_path, _engine = executable

    if not allow_restart and profile_kind != "dedicated":
        # 不能自己关用户的浏览器：先用**毫秒级**的原生窗口枚举看它是不是正开着。
        # （无窗口的「启动增强」占位进程不算"开着"——它没有窗口可关，稍后自动清掉。）
        #
        # **这条只对「用户日常配置」成立**：自动化只能在它启动那一刻接入。
        # 专属配置是我们自己的独立目录（`profiles/<account>`），跟用户开着几个 Edge 窗口
        # 毫无关系——真机 2026-09-24 同事机器就栽在这里：主配置接不了 → 回落到专属配置，
        # 却被这条全机器检查判成"你的 Edge 正在运行"（`SystemBrowserBusy`）而彻底登不进去，
        # 用户看到的是"浏览器开了、什么都没做、报错"。专属配置照常启动即可。
        windowed = windowed_edge_processes()
        if windowed:
            raise SystemBrowserBusy(
                "检测到你的 Edge 正在运行（也可能包含程序上一轮留下的窗口）。"
                "自动化只能在浏览器启动的那一刻接入，所以需要先让它退出："
                "可以到「设置 → 浏览器」点「关闭 Edge 并继续」，或自己关闭所有 Edge 窗口后重试。"
                "你的登录状态、书签与浏览记录都会保留。")

    # 顺序不能反：**先把配置目录腾空，再启动**。
    #
    # 为什么（真机 2026-09-24 11:01 事故，用户反馈「首次登录异常之慢、打开了浏览器却什么都没发生」）：
    # 先启动的话，命令会被 Edge 的「启动增强」后台实例转交——它顺手开出一个窗口，看着就像
    # "用户的 Edge"，我们把它关掉后重新启动，又落回那个后台进程里：进程活着、端口永远不出现，
    # 干等满 25 秒。腾空（含无窗口的后台预加载进程）之后，我们的启动才会真正开出一个带调试
    # 端口的新实例。
    owners = processes_using(directory)
    windowed = [item for item in owners if item["window"]]
    if windowed and not allow_restart:
        raise SystemBrowserBusy(
            "检测到你的 Edge 正在运行（也可能包含程序上一轮留下的窗口）。"
            "自动化只能在浏览器启动的那一刻接入，所以需要先让它退出："
            "可以到「设置 → 浏览器」点「关闭 Edge 并继续」，或自己关闭所有 Edge 窗口后重试。"
            "你的登录状态、书签与浏览记录都会保留。")
    if owners:
        remaining, handled = _release_profile(directory, allow_windowed=allow_restart)
        if remaining:
            if windowed and not allow_restart:
                raise SystemBrowserBusy(
                    "检测到你的 Edge 正在运行。自动化只能在浏览器启动的那一刻接入："
                    "可以到「设置 → 浏览器」点「关闭 Edge 并继续」，或自己关闭所有 Edge 窗口后重试。"
                    "你的登录状态、书签与浏览记录都会保留。")
            raise SystemBrowserBusy(
                "你的 Edge 正在运行，程序需要先关掉它才能接管（会自动帮你重新打开，"
                "登录、书签、历史都在）。刚才没能关干净，可以到「设置 → 浏览器」点「关闭 Edge 并继续」，"
                "或手动关闭所有 Edge 窗口后重试。")
        if handled:
            log.info("已腾空该配置目录（处理 %d 个 Edge 进程），开始带端口启动", handled)

    # 启动失败（命令没生效）就自愈一次：清掉残实例、重新腾空、再启动。默认只试两次——
    # 第一次已经腾空过，第二次还失败说明是别的问题（安全软件拦截等），如实报错比无限重试好。
    # 重试时只清"无窗口"的占用者，动了用户窗口的策略不会被绕过。
    # `attempts` 由调用方定：主配置只试一次——失败立刻回落专属配置，不再多花一份预算
    # （那多出来的一枪正是用户看到的「打开了两个页面」，见 MAIN_PROFILE_STARTUP_TIMEOUT_SECONDS）。
    port = _pick_port()
    process = None
    for attempt in range(attempts):
        port = _pick_port()
        # 记住启动前的 msedge 进程集：ShellExecute 启动没有进程句柄，靠"有没有新进程出现"
        # 判断启动命令是不是被已有实例转发了。
        baseline_pids = {int(item["pid"]) for item in edge_windows_native()}
        process = launch_edge(executable_path, directory, port, start_url=start_url)
        payload = _wait_for_endpoint(port, timeout, process, known_pids=baseline_pids)
        if payload is not None:
            return _launched_endpoint(port, directory, payload, process, profile_kind)
        _kill_quietly(process)
        if attempt + 1 >= attempts:
            break
        log.warning("带端口的 Edge 没起来（端口 %s 始终没有出现）：清掉残留实例后重试一次", port)
        # 自愈前先看一眼：这台电脑上真正在跑的 Edge 是不是另一个可执行文件（装在非标准
        # 位置时，固定路径找出来的可能根本不是他平时用的那一个）→ 换成它再试。
        live = running_edge_executable()
        if live and os.path.normcase(live) != os.path.normcase(executable_path):
            log.info("改用正在运行的 Edge（%s）重试", live)
            executable_path = live
        remaining, _handled = _release_profile(directory, allow_windowed=allow_restart)
        if remaining:
            break
    raise SystemBrowserUnavailable(
        f"浏览器没能启动（{timeout:.0f} 秒内没有开启自动化端口 {port}）。"
        "可能是安全软件拦截，或仍有 Edge 后台实例占着浏览器配置目录："
        "到「设置 → 浏览器」点「关闭 Edge 并继续」，或自己关闭所有 Edge 窗口后重试。")


def force_release_browser(user_data_dir: Path | str | None = None) -> dict[str, Any]:
    """**强制释放**浏览器：结束所有占用该配置目录的 Edge 进程，让自动化能重新接管。

    为什么客户端要有这个入口（用户 2026-09-23 明确要求：「客户端软件有杀进程的功能才行，
    不能在登录卡住」）：自动化只能在浏览器**启动那一刻**接入，所以只要有一个 Edge 实例
    还占着这个配置目录（哪怕是我们上一轮留下的、窗口停在新建标签页上的那种），
    下一次登录就必须先让它退出。用户不应该被这个卡住，也不应该自己去任务管理器里找。

    安全边界：
      - 只针对**目标配置目录**的进程（用命令行精确判断），不动用户其它浏览器；
      - 能走正常关闭就走正常关闭：带调试端口的实例先发 CDP `Browser.close`，它自己正常
        退出（真机实测 profile 里 `exit_type=Normal`，下次启动**不会**恢复上次会话）；
      - 其余有窗口的先发温和关闭（等于点 ×，Edge 自己保存会话），10 秒后再强制结束；
      - 无窗口的「启动增强」占位进程直接结束（没有窗口、没有可丢的数据）；
      - 只在"接管用的专属窗口"上动手，不清 Cookie、不删任何数据。
    """
    resolved = resolve_browser_executable()
    engine = resolved[1] if resolved else "edge"
    directory = Path(user_data_dir) if user_data_dir is not None else default_user_data_dir(engine)
    if directory is None:
        return {"released": False, "killed": 0, "message": "没有找到可用的浏览器配置目录。"}
    if graceful_close_via_cdp(directory):
        clear_record(directory)
        return {"released": True, "killed": 0,
                "message": "已让浏览器**正常退出**（CDP Browser.close，未强杀进程）；"
                           "登录状态、书签与浏览记录都没有被改动。"}
    owners = processes_using(directory)
    if not owners:
        return {"released": True, "killed": 0,
                "message": "当前没有占用该配置目录的 Edge 进程，不需要释放。"}
    log.info("手动释放浏览器：%s", [(item["pid"], "有窗口" if item["window"] else "无窗口") for item in owners])
    for item in owners:
        flags = ["/PID", str(item["pid"]), "/T"] + ([] if item["window"] else ["/F"])
        subprocess.run(["taskkill", *flags],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(2.0)
    for item in owners:
        if item["window"] and any(int(entry["pid"]) == int(item["pid"]) for entry in processes_using(directory)):
            subprocess.run(["taskkill", "/PID", str(item["pid"]), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            time.sleep(1.0)
    remaining = processes_using(directory)
    clear_record(directory)
    if remaining:
        return {"released": False, "killed": len(owners) - len(remaining),
                "message": f"仍有 {len(remaining)} 个 Edge 进程占用该配置目录，请稍后重试或重启电脑。"}
    return {"released": True, "killed": len(owners),
            "message": f"已释放浏览器（结束 {len(owners)} 个 Edge 进程），可以重新登录了；"
                       "你的登录状态、书签与浏览记录都没有被改动。"}


def takeover_status() -> dict[str, Any]:
    """给客户端看的接管状态：现在点「登录」会不会关掉用户的 Edge 窗口。

    界面据此先提示用户（"登录需要先关闭所有 Edge 窗口，程序会自动重新打开"），
    只有用户确认后才带 takeover=true 发起登录。只读进程表，不碰浏览器数据。
    """
    mode = main_profile_mode()
    resolved = resolve_browser_executable()
    engine = resolved[1] if resolved else "edge"
    directory = default_user_data_dir(engine)
    status: dict[str, Any] = {"mode": mode, "engine": engine, "profile": str(directory or ""),
                              "edge_running": False, "windowed": 0, "windowless": 0,
                              "needs_close": False, "main_profile": False}
    if mode == "off" or not resolved or directory is None or not directory.is_dir():
        return status
    if main_profile_unavailable():
        # 这台电脑已经证明接不了日常配置：别再让界面提示"会关闭你的 Edge"——我们不会再碰它。
        status["mode"] = "dedicated"
        return status
    status["main_profile"] = True
    recorded = _recorded_port(directory)
    if recorded is not None and port_open(recorded):
        # 上一轮接管过的浏览器还在（端口有监听）：界面不需要提醒任何事。
        return status
    # 这个接口在"点登录"前就会被界面调用，必须快：用毫秒级的原生窗口枚举，
    # 不去查进程命令行（PowerShell/CIM 单次约 2 秒，正好会让用户觉得"点登录半天没反应"）。
    windowed = windowed_edge_processes()
    status["edge_running"] = bool(windowed)
    status["windowed"] = len(windowed)
    status["needs_close"] = bool(windowed)
    return status

def endpoint_still_alive(endpoint: EdgeEndpoint | None) -> bool:
    """端点是否仍然可用（用于判断「窗口还在不在」）。"""
    return bool(endpoint) and probe_endpoint(endpoint.port) is not None
