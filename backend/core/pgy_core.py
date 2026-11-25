"""Pgy 本地数据根目录与原子写盘工具。

本模块是整个后端最底层的共享设施：只回答两个问题——

1. **数据放哪**：`ROOT` 固定为包所在的目录（而不是当前工作目录）。桌面客户端可能由
   快捷方式、计划任务或不同 shell 启动，cwd 会变；用 `__file__` 定位才能保证
   设置、缓存与导出始终落在同一处。
2. **怎么安全地写文件**：`_atomic_write` 用「临时文件 + 原子替换」避免进程被中断时
   留下半截 JSON。

本模块不依赖任何业务模块，也不联网，可以被单独导入、单独测试。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

# 数据根目录固定为包所在目录，而不是当前工作目录：桌面客户端可能由快捷方式、计划任务
# 或不同 shell 启动，cwd 会变；用 __file__ 定位才能保证文件始终落在同一处，
# 否则会出现「换了启动方式就看不到昨天的设置/缓存」的问题。
ROOT = Path(os.environ.get("PGY_DATA_DIR", Path(__file__).resolve().parents[2]))
LOG_DIR = ROOT / "pgy_logs"  # 审计日志目录：与业务数据分开，便于整体清理


def now_iso() -> str:
    """生成本地时间的秒级 ISO 时间戳，用于展示与排序。"""
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """通过临时文件再替换目标文件，降低进程中断造成 JSON 半文件的概率。

    为什么必须这样做：若原地覆盖写、写到一半进程被 Ctrl+C 或任务管理器结束，
    文件就会被截断（既不能被 json.loads 解析，也看不出哪些字段是旧值）。

    原子性从何而来：

    1. 先把完整内容写进**同一目录**下的 `<名字>.tmp`。同一目录必然在同一个卷上，
       后面的 rename 才是原子的；若 tmp 放到临时目录（可能跨卷），rename 会退化成
       「复制 + 删除」，又变成可中断的长时间操作。
    2. `os.replace` 在 POSIX 上对应 `rename(2)`，在 Windows 上对应 `MoveFileEx` 的
       替换式移动：两者都由文件系统在「目录项」这一元数据层面完成切换，并且都会覆盖
       已存在的目标文件（这也是不能直接用 `os.rename` 的原因：Windows 上对已存在
       文件会失败）。
    3. 于是任何时刻读到的 `<名字>.json` 只可能是「替换前的完整旧文件」或「替换后的
       完整新文件」，不存在中间态。

    边界与取舍：

    - 没有 fsync：本函数防的是「进程被杀死」，不防「整机掉电」——后者需要 fsync 文件
      并 fsync 目录才能保证元数据落盘。单机工具的代价收益比不划算，且掉电时回退到上
      一次保存的状态仍是可用状态（不会损坏）。
    - tmp 名字固定为 `<名字>.tmp` 而非随机名：调用方需保证同一文件的写是串行的；
      若将来出现多进程写同一文件，这里必须改成唯一 tmp 名或引入文件锁。
    - 只保证「文件内容完整」，不保证「业务字段之间自洽」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 与目标同目录同卷：这是 os.replace 具备原子性的前提
    tmp = path.with_name(path.name + ".tmp")
    # 显式 utf-8：中文内容依赖平台默认编码会在 Windows(GBK) 下写坏
    tmp.write_text(text, encoding="utf-8")
    # 一步完成原子切换：成功即新内容生效，失败则旧文件仍完好无损
    os.replace(tmp, path)
