"""工具层对外的唯一错误类型。

单独一个模块是为了打断 client ⇄ discovery 的循环导入：两边都要能抛
PgyError，但 discovery 先于 client 被导入。放在这里，谁都不依赖谁。

约定：`str(exc)` 与 `as_text()` 都是**可以直接转述给用户**的中文。上层的
protocol 会把它原样塞进工具结果，模型读到什么，用户就看到什么——所以这里
写下的每一句都要是「能照做的话」，不能是异常类名或英文堆栈。
"""

from __future__ import annotations


class PgyError(RuntimeError):
    """本地服务返回的失败，或连不上本地服务。带后端错误码供程序判分支。"""

    def __init__(self, message: str, *, code: str = "", status: int = 0,
                 retryable: bool = False, operation_id: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.operation_id = operation_id

    def as_text(self) -> str:
        parts = [str(self)]
        if self.code:
            parts.append(f"（错误码 {self.code}" + ("，可稍后重试）" if self.retryable else "）"))
        return " ".join(parts)
