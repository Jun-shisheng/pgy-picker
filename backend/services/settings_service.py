"""应用设置（目前主要是**导出目录**）。

存放位置：`<数据目录>/settings.json`。
- 开发态：项目根（`PGY_DATA_DIR`）
- 装机后：`%LOCALAPPDATA%\\Pgy\\settings.json`

**默认导出目录**（用户 2026-10-12 定）：**安装目录下的 `exports/`**，
开发态则是项目根的 `exports/` —— 两种形态都是「跟程序放在一起、固定可预期」。

⚠️ 这个选择带来一个必须同时处理的副作用：`%LOCALAPPDATA%\\Pgy` **既是数据目录
也是安装目录**（NSIS 是 `currentUser` 模式），而安装脚本原本会在**装/升级/卸载时
`RMDir /r` 整个目录** —— 那一并把用户已交付的表格删了。所以
`frontend/src-tauri/installer-hooks.nsh` 现在会**先把 `exports/` 挪开、清理完再挪回来**。
改这里的路径就必须同时改那个脚本，否则交付物会在下次重装时消失。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from backend.core.mio_core import ROOT

log = logging.getLogger("pgy.settings")

SETTINGS_FILE = ROOT / "settings.json"
#: 安装目录下的交付物文件夹。与 `installer-hooks.nsh` 里的保留逻辑**必须一致**。
#:
#: ⚠️ **刻意用 ASCII 名，别改成中文**：`installer-hooks.nsh` 要在 NSIS 里写同一个
#: 路径，而无 BOM 的 .nsh 会被 NSIS 按 ANSI(GBK) 解码 —— 中文路径字面量会被解错，
#: 于是"挪开再挪回来"挪的是另一个文件夹，用户的交付物**照样被删且毫无提示**。
#: 要改成中文，得先把那个文件转成 UTF-8 with BOM，并**真格跑一次升级安装**验证。
EXPORT_DIR_NAME = "exports"


def default_export_dir() -> Path:
    """默认导出目录 = **数据目录下的 `exports/`**。

    开发态数据目录就是项目根，于是落在 `项目根/exports/`；装机后是
    `%LOCALAPPDATA%\\Pgy\\exports\\` —— 也就是**安装目录下的 `exports/`**。
    两种形态同一套规则，不用再分叉。
    """
    return ROOT / EXPORT_DIR_NAME


def _read_raw() -> dict:
    if not SETTINGS_FILE.is_file():
        return {}
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        log.warning("设置文件解析失败，按默认处理：%s", exc)
        return {}


def load() -> dict:
    """返回当前设置（已填好默认值）。"""
    raw = _read_raw()
    export_dir = str(raw.get("export_dir") or "").strip()
    return {
        "export_dir": export_dir or str(default_export_dir()),
        "export_dir_is_default": not bool(export_dir),
        "settings_file": str(SETTINGS_FILE),
    }


def save(patch: dict) -> dict:
    """更新设置（只合并传入的键），返回更新后的完整设置。"""
    raw = _read_raw()
    for key in ("export_dir",):
        if key in patch:
            value = str(patch.get(key) or "").strip()
            if value:
                raw[key] = value
            else:
                raw.pop(key, None)          # 传空 = 恢复默认
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("设置已保存：%s", SETTINGS_FILE)
    return load()


def export_dir(*, create: bool = True) -> Path:
    """解析出的导出目录（必要时创建）。解析不出来就回落到默认值。"""
    configured = Path(load()["export_dir"]).expanduser()
    try:
        if create:
            configured.mkdir(parents=True, exist_ok=True)
        return configured
    except Exception as exc:
        fallback = default_export_dir()
        log.warning("导出目录 %s 不可用（%s），回落到 %s", configured, exc, fallback)
        if create:
            fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def validate(path_str: str) -> tuple[bool, str]:
    """校验一个候选导出目录是否可用。返回 `(是否可用, 说明)`。"""
    text = (path_str or "").strip()
    if not text:
        return False, "路径不能为空"
    candidate = Path(text).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"无法创建该目录：{exc}"
    if not candidate.is_dir():
        return False, "该路径不是一个目录"
    # 真正写一个文件试试 —— `mkdir` 成功不代表写得进去（权限、只读盘、网络盘）。
    probe = candidate / ".pgy-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return False, f"目录不可写：{exc}"
    return True, str(candidate)
