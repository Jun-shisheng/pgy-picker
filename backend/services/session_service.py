"""Stable identity and short-lock snapshots, separate from browser serialization."""
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock, RLock
from copy import deepcopy
from time import monotonic
from uuid import uuid4


class SessionConflict(Exception):
    def __init__(self, message, code="OPERATION_CONFLICT", operation_id=None):
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


# 冲突提示里说清"到底是什么在跑"：只说「已有账户操作正在进行」，用户会以为是误报
# （真机 2026-09-22 反复反馈「明明没在操作却提示冲突」）。
_ACTION_LABELS = {
    "login": "登录", "restore": "登录", "send_code": "发送验证码", "verify_code": "验证码登录",
    "logout": "退出登录", "subaccounts": "子账户查询", "jump": "联登聚光", "plans": "计划查询",
    "select_plans": "勾选计划", "prepare": "打开创建页", "fill": "填写笔记 ID",
    "create": "创建", "create_and_publish": "一键填写并发布", "publish": "发布",
    "preview": "页面截图", "status": "状态检查", "resume": "恢复", "show": "打开窗口", "hide": "收回窗口",
    "prewarm": "登录页预热", "keepalive": "会话保活", "abandon": "终止任务",
}


class SessionService:
    # 用户操作撞上程序自己的短操作（状态检查/读列表）时先排队等一会儿；只有真的
    # 被长任务占用才回 409。真机反馈的「冲突」几乎都是撞上了几秒的状态轮询。
    OPERATION_WAIT_SECONDS = 8.0

    def __init__(self):
        self._operation_lock = Lock()
        self._state_lock = RLock()
        self.account_id: str | None = None
        self.generation = 0
        self.status = "disconnected"
        self.checked_at: str | None = None
        self._verified_monotonic: float | None = None
        self._identity_verified = False
        self._account = None
        self.executor_status = "idle"
        self.control_mode = "automation"
        self._operation: dict | None = None
        self._sequence = 0

    @contextmanager
    def operation(self, account_id, action, account=None):
        if not self._operation_lock.acquire(timeout=self.OPERATION_WAIT_SECONDS):
            snapshot = self.snapshot()
            running = snapshot["operation"] or {}
            label = _ACTION_LABELS.get(str(running.get("action")), "另一项操作")
            raise SessionConflict(f"{label}正在进行中，请等它结束后再试。", operation_id=running.get("operation_id"))
        operation = None
        try:
            with self._state_lock:
                if self.executor_status == "recovery_required" and action != "logout":
                    # 用户的退出永远优先：超时未确认停止时也允许退出账户（浏览器侧会退化成
                    # 强制清理），其它操作仍然禁止，避免和可能还在跑的浏览器操作交叠。
                    raise SessionConflict("操作超时且尚未确认停止；已禁止新操作，请等待受控恢复。", "RECOVERY_REQUIRED", (self._operation or {}).get("operation_id"))
                # 退出别的账户只是清理它自己的持久化 Profile，不是"切换当前会话"；
                # 否则 A 在线时 B 永远删不掉、也改不了登录方式。
                if self.account_id and self.account_id != account_id and action != "logout":
                    raise SessionConflict("请先退出当前账户，再登录或操作其他账户。", "ACCOUNT_CONFLICT")
                if action == "abandon" and self.control_mode != "manual":
                    raise SessionConflict("当前没有需要终止的人工接管任务。", "NOT_MANUAL")
                if action in {"show", "hide", "resume", "abandon"} and self.account_id is None:
                    raise SessionConflict("请先登录合作平台，再操作这一步。", "NO_ACTIVE_SESSION")
                # login 是用户明确的「重来一次」意图：人工接管不能把它顶回 409。
                # 点击登录即视为放弃当前接管（浏览器侧随之关闭受管窗口后重新登录）。
                if self.control_mode == "manual" and action not in {"show", "resume", "logout", "abandon", "login"}:
                    raise SessionConflict("现在是你手动操作中。请停止操作页面，回到客户端点「已处理，重新检查」。", "MANUAL_CONTROL")
                if action == "resume" and self.control_mode != "manual":
                    raise SessionConflict("当前没有待恢复的人工接管。", "NOT_MANUAL")
                if action == "login" or (action == "send_code" and self.account_id is None):
                    self.account_id = account_id
                    self.generation += 1
                    self._identity_verified = False
                    self.status = "checking"
                    self.checked_at = None
                    self._verified_monotonic = None
                if account and account_id == self.account_id:
                    self._account = {"id": account.id, "display_name": account.display_name}
                self._sequence += 1
                operation = {"operation_id": uuid4().hex, "sequence": self._sequence, "account_id": account_id,
                             "generation": self.generation, "action": action,
                             "status": "running", "started_at": self._now(), "finished_at": None}
                self._operation = operation
                self.executor_status = "running"
                if action in {"show"}:
                    # 「打开页面查看」不再进入人工接管：只把窗口露出来给用户看，
                    # 自动执行权保留（下一次操作开始时窗口会自动收回后台）。
                    pass
                elif action == "login" and self.control_mode == "manual":
                    # 强制重新登录：接管状态在操作开始时即失效，即使登录失败，
                    # 界面也不会再显示「人工接管中」——受管窗口此刻已被关闭。
                    self.control_mode = "automation"
            try:
                yield deepcopy(operation)
            except Exception:
                with self._state_lock:
                    if self._matches(operation) and operation["status"] == "running":
                        operation["status"] = "failed"
                raise
            else:
                with self._state_lock:
                    if self._matches(operation) and operation["status"] == "running":
                        operation["status"] = "succeeded"
        finally:
            with self._state_lock:
                if operation and self._matches(operation):
                    if operation["status"] != "recovery_required":
                        operation["finished_at"] = self._now()
                    if self.executor_status == "running":
                        self.executor_status = "idle"
            self._operation_lock.release()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _matches(self, operation):
        return bool(self._operation and operation and self._operation["operation_id"] == operation["operation_id"]
                    and self.generation == operation["generation"])

    def observe(self, account_id, result, operation):
        with self._state_lock:
            if self.account_id != account_id or not self._matches(operation) or self.executor_status == "recovery_required":
                return
            self.status = ("challenge" if result.get("stage") == "challenge" else
                           result.get("connection_status") or ("connected" if result.get("connected") else "expired"))
            if self.status == "connected":
                self._identity_verified = True
            if result.get("control_mode") == "manual":
                self.control_mode = "manual"
            if operation["action"] == "resume" and result.get("control_mode") == "automation" and self.status == "connected":
                self.control_mode = "automation"
            if self.status != "unknown":
                self.checked_at = self._now()
                self._verified_monotonic = monotonic()

    def recover_if_idle(self, has_active_work: bool) -> bool:
        """受控恢复：超时未确认停止的操作一旦真的结束，就自动解锁。

        超时时会把会话锁成 recovery_required（除退出登录外一切操作被拒——真机
        2026-09-22 用户因此连「查看当前网页」都点不动）。浏览器侧已经没有任何在跑
        的操作时，这把锁没有存在意义：自动解除，并把连接状态标回 unknown，等下一次
        真实检查重新确认（登录态本身不受影响，绝不是掉线）。
        """
        with self._state_lock:
            if self.executor_status != "recovery_required" or has_active_work:
                return False
            self.executor_status = "idle"
            self.status = "unknown"
            self._verified_monotonic = None
            return True

    def mark_unknown(self, operation, *, recovery_required=False):
        with self._state_lock:
            if not self._matches(operation):
                return
            self.status = "unknown"
            # Preserve the last actual check time, not the time of an exception.
            if recovery_required:
                self.executor_status = "recovery_required"
                # _matches() proves that both operations exist and refer to the same
                # generation, but type checkers cannot narrow an instance attribute
                # through that helper call.
                current_operation = self._operation
                if current_operation is not None:
                    current_operation["status"] = "recovery_required"

    def terminate_task(self, account_id, operation):
        """End the current manual task without logging out its platform account."""
        with self._state_lock:
            if self.account_id != account_id or not self._matches(operation):
                return
            self.status = "unknown"
            self.control_mode = "automation"
            # A closed page is no longer connection evidence. Keep the
            # verified identity while requiring a fresh hidden Profile check.
            self._verified_monotonic = None

    def rename(self, account):
        with self._state_lock:
            if self.account_id == account.id:
                self._account = {"id": account.id, "display_name": account.display_name}

    def clear(self):
        with self._state_lock:
            self.account_id = None
            self._account = None
            self._identity_verified = False
            self.status = "disconnected"
            self.generation += 1
            self.checked_at = None
            self._verified_monotonic = None
            self.executor_status = "idle"
            self._operation = None
            self.control_mode = "automation"

    def snapshot(self):
        with self._state_lock:
            # 登录状态不按时间过期；只有实际检查返回非 connected 才失效。
            # 平台操作会在发现会话失效时更新 status，避免无操作时反复要求重新连接。
            stale = self.status != "connected"
            return {"status": self.status, "connection_status": self.status,
                    "account_id": self.account_id,
                    "account": deepcopy(self._account) if self._identity_verified else None,
                    "generation": self.generation, "checked_at": self.checked_at,
                    "verification_stale": stale,
                    "executor_status": self.executor_status, "operation": deepcopy(self._operation),
                    "control_mode": self.control_mode,
                    "can_operate": self.status == "connected" and not stale and self.executor_status == "idle" and self.control_mode == "automation"}
