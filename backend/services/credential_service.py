"""蒲公英账号凭据：**只进 Windows 凭据管理器**（`CLAUDE.md` 第四条）。

开发期允许从 `资料和说明/账号密码.txt` **一次性导入**，导入后即由凭据管理器保管；
此后任何代码路径都不再读那个文件。凭据**不打印、不落库、不进日志**。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.mio_core import ROOT
from backend.services.mio_credential import CredentialStore, redact

log = logging.getLogger("pgy.credential")

PLATFORM = "pugongying"
DEFAULT_ACCOUNT_ID = "default"
SEED_FILE = ROOT / "资料和说明" / "账号密码.txt"

_store = CredentialStore()


def _parse_seed_file(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    account = password = ""
    for line in re.split(r"[\r\n]+", raw):
        line = line.strip()
        if not line:
            continue
        for sep in ("：", ":", "="):
            if sep in line:
                key, value = line.split(sep, 1)
                key, value = key.strip(), value.strip()
                if key in ("账号", "帐号", "用户名", "account"):
                    account = value
                elif key in ("密码", "password"):
                    password = value
                break
    return account, password


def seed_from_file(account_id: str = DEFAULT_ACCOUNT_ID) -> bool:
    """把种子文件里的凭据导入凭据管理器。已有则跳过。"""
    if _store.exists(account_id, PLATFORM):
        return False
    if not SEED_FILE.exists():
        log.info("没有种子文件 %s，跳过导入", SEED_FILE.name)
        return False
    account, password = _parse_seed_file(SEED_FILE)
    if not account or not password:
        log.warning("种子文件解析不出账号或密码，跳过导入")
        return False
    _store.save(account_id, account, password, PLATFORM)
    log.info("已把蒲公英凭据导入系统凭据管理器（account_id=%s）", account_id)
    return True


def load(account_id: str = DEFAULT_ACCOUNT_ID) -> tuple[str, str] | None:
    """取凭据；没有就尝试一次从种子文件导入。返回值只在内存里传递。"""
    credential = _store.load(account_id, PLATFORM)
    if credential is not None:
        return credential.username, credential.password
    if seed_from_file(account_id):
        credential = _store.load(account_id, PLATFORM)
        if credential is not None:
            return credential.username, credential.password
    return None


def has_credentials(account_id: str = DEFAULT_ACCOUNT_ID) -> bool:
    return _store.exists(account_id, PLATFORM)


def save(username: str, password: str, account_id: str = DEFAULT_ACCOUNT_ID) -> None:
    """写入/覆盖凭据。**只记 account_id，绝不记用户名或密码。**"""
    _store.save(account_id, username, password, PLATFORM)
    log.info("已更新凭据（account_id=%s）", account_id)


def delete(account_id: str = DEFAULT_ACCOUNT_ID) -> bool:
    """删除凭据；本来就没有时返回 False（幂等，不报错）。"""
    removed = _store.delete(account_id, PLATFORM)
    log.info("删除凭据 account_id=%s 结果=%s", account_id, removed)
    return removed


def describe(account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
    """给界面看的账号信息 —— **构造白名单 dict，绝不透传 Credential 对象**。

    直接 `asdict()` / `__dict__` 会把密码一起带出去，这是本模块最容易犯的错。
    """
    credential = _store.load(account_id, PLATFORM)
    if credential is None:
        return {"has_credentials": False, "username_masked": ""}
    return {"has_credentials": True,
            "username_masked": redact(credential.username)}
