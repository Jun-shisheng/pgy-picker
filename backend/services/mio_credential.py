"""Mio 本地凭据安全存储：Windows Credential Manager（ctypes，无第三方依赖）。

产品规则（对应 P0-1 第 2.2 / 2.4 节）：
- 账号元数据进 SQLite（mio_account），密码/敏感凭据进本模块（Windows Credential Manager）。
- 业务层只允许 AccountService → CredentialStore 这条调用链，不得直接调用本模块底层
  的 CredWriteW / CredReadW 等系统接口。
- 密码绝不以明文写入 SQLite、JSON 文件、日志或源码。

target 命名：`pgy://<platform>/account/<account_id>`，只用于在系统凭据库里定位条目，
不含密码本身。每条凭据的 blob 存 `{"username": ..., "password": ...}`。

可测试性：CredentialStore 接受一个可注入 backend（默认 WindowsBackend）。单元测试
注入内存 FakeBackend，绝不写入真实 Windows 凭据库。
"""
from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol

CRED_TYPE_GENERIC = 1
# 当前用户可见的「普通凭据」，绑定到用户会话，注销/重启后仍在（正是账号密码需要的持久性）。
CRED_PERSIST_ENTERPRISE = 3
ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


@dataclass(frozen=True)
class Credential:
    account_id: str
    username: str
    password: str


class CredentialBackend(Protocol):
    """凭据后端的结构化接口，`WindowsBackend` 与测试用 `FakeBackend` 都满足它。

    这里刻意用 Protocol 而不是把参数标注成具体的 `WindowsBackend`：本模块的设计约束
    明确允许单元测试注入内存 FakeBackend（绝不写真实 Windows 凭据库）。若按具体类标注，
    测试替身就无法注入——类型标注会与模块自身的设计约束互相矛盾，测试只能靠
    `# type: ignore` 打补丁。Protocol 让"可替换后端"这件事在类型层面也成立。
    """

    def write(self, target: str, username: str, blob: bytes) -> None:
        """写入/覆盖一条凭据；失败应抛 OSError。"""
        ...

    def read(self, target: str) -> tuple[str, bytes] | None:
        """读取凭据，返回 (username, blob)；条目不存在返回 None。"""
        ...

    def delete(self, target: str) -> bool:
        """删除凭据；原本就不存在时返回 False 而不是抛错（幂等语义）。"""
        ...


class WindowsBackend:
    """ctypes 直连 Windows Credential Manager（Advapi32.dll）。"""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows Credential Manager 仅在 Windows 上可用")
        self._advapi32 = ctypes.windll.advapi32
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    @staticmethod
    def _last_error() -> int:
        return ctypes.windll.kernel32.GetLastError()

    def write(self, target: str, username: str, blob: bytes) -> None:
        cred = _CREDENTIALW()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        buf = ctypes.create_string_buffer(blob, len(blob))
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = CRED_PERSIST_ENTERPRISE
        cred.UserName = username
        if not self._advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise ctypes.WinError(self._last_error())

    def read(self, target: str) -> tuple[str, bytes] | None:
        pcred = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = self._last_error()
            if err == ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(err)
        try:
            cred = pcred.contents
            size = cred.CredentialBlobSize
            blob = ctypes.string_at(cred.CredentialBlob, size) if size else b""
            username = cred.UserName or ""
            return username, blob
        finally:
            self._advapi32.CredFree(pcred)

    def delete(self, target: str) -> bool:
        if not self._advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            err = self._last_error()
            if err == ERROR_NOT_FOUND:
                return False
            raise ctypes.WinError(err)
        return True


class CredentialStore:
    """统一凭据门面：业务层只依赖它，不直接接触 Credential Manager。"""

    def __init__(self, backend: CredentialBackend | None = None) -> None:
        # 不传 backend 时才落到真实 Windows 凭据库；测试传 FakeBackend 实现完全隔离。
        self._backend = backend if backend is not None else WindowsBackend()

    @staticmethod
    def target_name(account_id: str, platform: str = "pugongying") -> str:
        return f"pgy://{platform}/account/{account_id}"

    def save(self, account_id: str, username: str, password: str, platform: str = "pugongying") -> None:
        blob = json.dumps({"username": username, "password": password}, ensure_ascii=False).encode("utf-8")
        self._backend.write(self.target_name(account_id, platform), username, blob)

    def load(self, account_id: str, platform: str = "pugongying") -> Credential | None:
        data = self._backend.read(self.target_name(account_id, platform))
        if data is None:
            return None
        stored_username, blob = data
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return Credential(
            account_id=account_id,
            username=payload.get("username") or stored_username,
            password=payload.get("password") or "",
        )

    def delete(self, account_id: str, platform: str = "pugongying") -> bool:
        return self._backend.delete(self.target_name(account_id, platform))

    def exists(self, account_id: str, platform: str = "pugongying") -> bool:
        return self._backend.read(self.target_name(account_id, platform)) is not None


def redact(value: str) -> str:
    """对敏感值做掩码（界面/日志用），返回如 `a***`。空值原样返回。"""
    if not value:
        return ""
    if len(value) <= 1:
        return "*"
    return value[:1] + "***"
