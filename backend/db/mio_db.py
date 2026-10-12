"""本地 SQLite 存储层：连接工厂、建库与迁移。

承载「账号元数据」表 `local_accounts`。选号任务/快照仍走 `mio_core` 的 JSON 存储
（渐进迁移，不推倒重写）。**密码等敏感凭据不进本库**，统一由
`mio_credential.CredentialStore` 交给 Windows Credential Manager。

本模块不依赖 GUI / 队列 / 浏览器，可独立测试。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.core.mio_core import ROOT

DB_PATH = ROOT / "pgy.db"

_ACCOUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS local_accounts (
    id             TEXT PRIMARY KEY,
    platform       TEXT NOT NULL DEFAULT 'pugongying',
    display_name   TEXT NOT NULL,
    username       TEXT NOT NULL,
    credential_key TEXT NOT NULL,
    remark         TEXT NOT NULL DEFAULT '',
    is_default     INTEGER NOT NULL DEFAULT 0,
    last_used_at   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""

def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """打开连接并应用统一 pragma。调用方应使用 open_db 以获得自动提交/回滚/关闭。"""
    # Keep the low-level factory usable on a fresh/custom data directory too.
    # ``sqlite3.connect`` does not create parent directories itself.
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_db(db_path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    """打开连接：成功提交、异常回滚、最后关闭。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """老库补列：SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查表结构再改。"""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def migrate(db_path: Path | str = DB_PATH) -> None:
    """建表 + 迁移到最新 schema。幂等，可重复调用。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn:
        conn.executescript(_ACCOUNTS_TABLE)
