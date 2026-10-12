"""交付物在接口响应里的形状。

**这个文件的由来**：`app.py` 原来用 `path.parent.relative_to(ROOT)` 算相对目录。
装机后 `ROOT` 是 `%LOCALAPPDATA%\\Pgy`，而默认导出目录在「文档」下 —— 不在 ROOT 底下，
`relative_to` 抛 ValueError。要命的是它发生在**检索成功、Excel 也写完**之后，
用户看到的却是「选号失败」。开发态因为导出目录恰好落在 ROOT 里，一直没暴露。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.mio_core import ROOT
from backend.services import settings_service
from backend.services.selection_service import export_payload

# 「导出目录不在 ROOT 底下」这种情形仍然会发生 —— 用户可以在设置里把导出目录改到任何
# 地方（比如 D:\达人提报）。这条用例防的是**路径相对化**那类写法，与默认值在哪无关。
INSTALLED_PATH = Path("D:/达人提报/达人提报-20261012-153000.xlsx")


def test_payload_works_when_export_dir_is_outside_root():
    """这条在修复前是红的 —— 而它在装机后是**每次检索**都会走到。"""
    payload = export_payload(INSTALLED_PATH)

    assert payload["name"] == "达人提报-20261012-153000.xlsx"
    assert payload["path"] == str(INSTALLED_PATH)
    assert payload["dir"] == str(INSTALLED_PATH.parent)
    # dir 必须是绝对路径：界面拿它去调 Tauri 的 open_path
    assert Path(payload["dir"]).is_absolute()


def test_payload_also_works_inside_root():
    """开发态（导出目录落在项目根里）同样要正常。"""
    inside = Path(ROOT) / "exports" / "达人提报-20261012-153000.xlsx"
    payload = export_payload(inside)
    assert payload["dir"] == str(inside.parent)


def test_relative_to_would_have_raised():
    """把"为什么不能用 relative_to"钉成一条可执行的证据。"""
    with pytest.raises(ValueError):
        INSTALLED_PATH.parent.relative_to(ROOT)


# ── 默认导出目录：安装目录下的 exports/ ─────────────────────────────────────

def test_default_export_dir_lives_under_the_data_dir():
    """用户 2026-10-12 定：Excel 跟程序放在一起。

    装机后数据目录 == 安装目录（`%LOCALAPPDATA%\\Pgy`），所以默认就是安装目录下的
    `exports/`；开发态数据目录是项目根，落点同理。
    """
    assert settings_service.default_export_dir() == Path(ROOT) / "exports"
    assert settings_service.EXPORT_DIR_NAME == "exports"


def test_installer_hook_protects_the_same_folder():
    """安装脚本必须保留**同一个**文件夹名。

    这是本次唯一一处"改了 A 忘了改 B 就会静默删掉客户交付物"的地方：
    名字对不上 → 挪开/挪回作用于另一个目录 → 用户的 Excel 随着
    `RMDir /r "$LOCALAPPDATA\\Pgy"` 一起没了，而且**没有任何提示**。
    """
    hooks = (Path(__file__).resolve().parents[1]
             / "frontend" / "src-tauri" / "installer-hooks.nsh")
    text = hooks.read_text(encoding="utf-8")

    assert f"Pgy\\{settings_service.EXPORT_DIR_NAME}" in text, (
        f"安装脚本里没有 `Pgy\\{settings_service.EXPORT_DIR_NAME}`："
        "交付物会在装/升级/卸载时被删掉")
    assert "PGY_STASH_EXPORTS" in text and "PGY_RESTORE_EXPORTS" in text
    # 保留动作必须同时出现在安装与卸载两条路径上
    assert text.count("!insertmacro PGY_RESTORE_EXPORTS") == 2


def test_nsis_hook_file_stays_ascii_for_paths():
    """无 BOM 的 .nsh 会被 NSIS 按 ANSI 解码 —— 非 ASCII 路径会被解错。

    这里只允许中文出现在注释里（`;` 开头），不允许出现在 nsExec 的命令行里。
    """
    hooks = (Path(__file__).resolve().parents[1]
             / "frontend" / "src-tauri" / "installer-hooks.nsh")
    for line in hooks.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(";") or not stripped:
            continue
        assert stripped.isascii(), (
            f"安装脚本的非注释行里出现了非 ASCII 字符，NSIS 会解错：{stripped}")
