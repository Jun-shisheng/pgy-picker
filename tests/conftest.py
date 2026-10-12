"""测试夹具。

**三条铁律**（写在这里，免得日后有人图省事绕过去）：

1. **绝不碰真实的 Windows 凭据管理器** —— 密码是用户的真实资产，
   测试跑一遍就把它改掉是不可接受的。靠 `mio_credential.CredentialBackend`
   这个 Protocol 注入内存替身来隔离（该 Protocol 的文档里写明就是为此存在的）。
2. **绝不打开浏览器、绝不访问蒲公英** —— 这是产品红线（`CLAUDE.md` 第一、二条），
   跑测试不是访问平台的理由。`_forbid_browser` 直接让 `run_with_page` 抛断言，
   任何不小心走到浏览器的测试会**当场失败**而不是悄悄开一个窗口。
3. **绝不把缓存写进仓库根** —— 缓存文件默认落在 `ROOT`，不隔离的话
   每跑一次测试就在工作区里留一个 `industries-cache.json`。

⚠️ 第 1 条还不够：`credential_service.load()` 在没有凭据时会去读
`资料和说明/账号密码.txt` **自动导入**。不把种子文件也屏蔽掉，
"无凭据" 的分支根本测不到，而且会带着真凭据一路走到登录
（第一次写这套测试就踩了：日志里出现「已建立蒲公英浏览器会话」——
测试真的开了浏览器）。所以种子文件一并指向不存在的路径。

跑法：在**仓库根**执行 `python -m pytest tests`
（用 `python -m` 才会把当前目录放进 `sys.path`，`import backend` 才成立）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services import (
    credential_service,
    industry_cache,
    progress,
    selection_cursor,
    selection_service,
)
from backend.services.mio_credential import CredentialStore


class FakeBackend:
    """内存版凭据后端，满足 `CredentialBackend` Protocol。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, bytes]] = {}

    def write(self, target: str, username: str, blob: bytes) -> None:
        self._items[target] = (username, blob)

    def read(self, target: str) -> tuple[str, bytes] | None:
        return self._items.get(target)

    def delete(self, target: str) -> bool:
        # 幂等语义与 WindowsBackend 保持一致（不存在返回 False，不抛错）
        return self._items.pop(target, None) is not None


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """把凭据库、种子文件、行业缓存都换成本次测试专用的临时实现。"""
    fake = FakeBackend()
    monkeypatch.setattr(credential_service, "_store", CredentialStore(fake))
    monkeypatch.setattr(credential_service, "SEED_FILE", tmp_path / "没有这个种子文件.txt")
    monkeypatch.setattr(industry_cache, "CACHE_FILE", tmp_path / "industries-cache.json")
    monkeypatch.setattr(selection_cursor, "CURSOR_FILE", tmp_path / "selection-cursor.json")
    # 进度是**进程内全局**状态：不重置的话，上一个用例留下的 STAGE 会污染下一个。
    progress.reset()
    return {"backend": fake, "tmp": tmp_path}


@pytest.fixture(autouse=True)
def _forbid_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试一旦走到浏览器就当场炸，绝不允许真的开窗口访问平台。"""

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "测试里不应触碰真实浏览器（pgy_session.run_with_page）——"
            "请把该路径替换成替身，不要在测试里访问蒲公英。")

    monkeypatch.setattr(selection_service.pgy_session, "run_with_page", forbidden)


@pytest.fixture()
def client():
    """FastAPI 测试客户端。请求不带 Origin 头，因此不会被来源校验拦下。"""
    from fastapi.testclient import TestClient

    from backend.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
