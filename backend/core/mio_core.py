"""Mio 核心领域层：任务状态机、本地任务存储与结构化审计日志。

这是「小红书聚光投放」批处理工具的最底层：本模块不依赖 mio_queue / mio_executor /
mio_client，而是被它们依赖。因此它可以被单独导入、单独测试，不会顺带拉起 GUI、
浏览器或网络连接。

职责边界（为什么这样切分）：
- 状态机：用 ALLOWED_TRANSITIONS 白名单写死「任务此刻允许变成什么」，非法迁移直接抛
  IllegalTransition。调度侧和 UI 侧的任意代码都无法把任务从 SUCCEEDED 改成 RUNNING
  这类会破坏断点语义的中间态，规则只存在于一处。
- 持久化：Job 自带序列化（to_dict / from_dict），JobStore 只管读写文件。领域对象不关心
  文件在哪，存储层不关心业务规则，两者之间只通过普通 dict 交互。
- 审计：AuditLog 只做 JSONL 追加写，用于事后复盘「什么时候、对哪个任务、做成了什么」。
  它是旁路（side channel），不参与任务状态推导。

设计约束：
- 纯标准库，不联网、不读取第三方账号密码（只用到 json/os/threading/uuid/dataclasses/
  datetime/enum/pathlib/typing）。
- 任务状态只能沿允许路径迁移，重试生成新的 attempt，不覆盖历史结果：add_results 按
  (note_id, attempt) 覆盖写入，上一轮 attempt 的记录依然保留，便于对比第 1 次与第 2 次
  失败原因的差异。
- 所有写盘使用“临时文件 + 原子替换”，避免执行中被中断导致任务文件损坏。
- 并发安全只覆盖「单进程内多线程」：靠 threading.RLock 串行化同一 JobStore 实例的写
  操作。多进程同时写同一个任务文件不在支持范围内（详见 _atomic_write 与 JobStore.save）。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

# 数据根目录固定为包所在目录，而不是当前工作目录：桌面客户端可能由快捷方式、计划任务
# 或不同 shell 启动，cwd 会变；用 __file__ 定位才能保证任务文件始终落在同一处，
# 否则会出现「换了启动方式就看不到昨天的任务」的问题。
ROOT = Path(os.environ.get("PGY_DATA_DIR", Path(__file__).resolve().parents[2]))
JOBS_DIR = ROOT / "mio_jobs"  # 每个任务一个 <job_id>.json，是任务状态的唯一真相来源
LOG_DIR = ROOT / "pgy_logs"  # 审计日志目录：与任务数据分开，便于整体清理而不动任务
QUEUE_LOG = LOG_DIR / "queue.jsonl"  # 追加式 JSONL：一行一条事件，异常时最多丢最后一行
BATCH_SIZE = 100  # 默认批次大小：一次浏览器会话提交的笔记数，用来控制单次交互时长
EXECUTOR_VERSION = "0.2"  # 执行器协议版本，写入任务快照，便于识别旧任务由哪代执行器产生


def now_iso() -> str:
    """生成本地时间的秒级 ISO 时间戳，作为任务审计和 UI 展示时间。

    为什么所有时间都统一从这里取：created_at / updated_at / started_at / finished_at /
    events[].at 全部用同一函数生成，格式一致（YYYY-MM-DDTHH:MM:SS），字符串可以直接
    按字典序排序——JobStore.list() 才能用 created_at 做倒序排列。若改用「文件修改时间」
    排序，刚打开过的老任务会跳到列表最前面，与用户的心理预期相反。

    边界条件：
    - 秒级精度意味着同一秒内产生的多个事件时间戳相同，不能用它区分同秒事件的先后，
      真正的先后顺序由 events 列表的追加顺序决定（append 顺序即因果顺序）。
    - 用本地时间而非 UTC：UI 展示要与用户感知一致；代价是跨时区/夏令时切换时，历史
      时间戳不能直接相互比较，这里只做展示与排序，不做时长计算，因此可以接受。
    """
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """通过临时文件再替换目标文件，降低进程中断造成 JSON 半文件的概率。

    为什么必须这样做：任务 JSON 是断点续跑的唯一起点。若原地覆盖写、写到一半进程被
    Ctrl+C 或任务管理器结束，文件就会被截断（截断后的 JSON 既不能被 json.loads 解析，
    也看不出哪些字段是旧值），任务既无法继续也无法重试，只能人工修文件。

    原子性从何而来：
    1) 先把完整内容写进同一目录下的 <名字>.tmp。同一目录必然在同一个卷上，后面的 rename
       才是原子的；若 tmp 放到临时目录（可能跨卷），rename 会退化成「复制 + 删除」，又变
       成可中断的长时间操作。
    2) os.replace 在 POSIX 上对应 rename(2)，在 Windows 上对应 MoveFileEx 的替换式移动：
       两者都由文件系统在「目录项」这一元数据层面完成切换，并且都会覆盖已存在的目标文件
       （这也是不能直接用 os.rename 的原因：Windows 上对已存在文件会失败）。
    3) 于是任何时刻读到的 <名字>.json 只可能是「替换前的完整旧文件」或「替换后的完整新
       文件」，不存在中间态。半写内容只短暂存在于 .tmp 中，而没有任何读取路径会去解析 .tmp。

    边界与取舍（为什么这里不需要更强保证）：
    - 没有 fsync：本函数防的是「进程被杀死」，不防「整机掉电」——后者需要 fsync 文件并
      fsync 目录才能保证元数据落盘。单机工具的代价收益比不划算，且掉电时回退到上一次保存
      的任务状态仍是可用状态（不会损坏）。
    - tmp 名字固定为 "<名字>.tmp" 而非随机名：同一任务的并发写会争用同一个 tmp。之所以安全，
      是因为调用方 JobStore.save 全程持锁，同一时刻只有一个写者。若将来出现多进程写同一任务，
      这里必须改成唯一 tmp 名或引入文件锁。
    - 只保证「文件内容完整」，不保证「业务字段之间自洽」：字段一致性由 Job 与 JobStore 负责。
    """
    # parents=True：首次运行时 mio_jobs/ 可能还不存在，自动创建，避免每个调用方各写一遍
    path.parent.mkdir(parents=True, exist_ok=True)
    # 与目标同目录同卷：这是 os.replace 具备原子性的前提
    tmp = path.with_name(path.name + ".tmp")
    # 显式 utf-8：任务名与失败原因都含中文，依赖平台默认编码会在 Windows(GBK) 下写坏
    tmp.write_text(text, encoding="utf-8")
    # 一步完成原子切换：成功即新内容生效，失败则旧文件仍完好无损
    os.replace(tmp, path)


class JobStatus(str, Enum):
    """任务级状态机的全部取值。

    继承 str 让成员可以和字符串直接比较、也能被 json.dumps 处理，但落盘时仍然显式写
    .value：序列化方式只有一种，将来即使枚举成员改名也不会产生两种历史文件格式。
    每个状态「谁能写、意味着什么」见下方注释；状态之间的合法连线见 ALLOWED_TRANSITIONS。
    """

    DRAFT = "draft"  # 草稿：刚创建、尚未校验，只存在于本地
    VALIDATING = "validating"  # 校验中：检查 ID 格式/去重/账号是否可用等前置条件
    PENDING_CONFIRM = "pending_confirm"  # 待确认：校验通过，等用户点开始才入队
    QUEUED = "queued"  # 排队中：已入队，等待执行器领取
    RUNNING = "running"  # 执行中：执行器正在逐批处理
    PAUSED = "paused"  # 已暂停：用户主动暂停，cursor 不动，恢复时从断点继续
    NEEDS_MANUAL = "needs_manual"  # 需要人工处理：登录失效/验证码/页面异常，必须人工介入
    AWAITING_PUBLISH = "awaiting_publish"  # 待人工发布：页面已就绪，发布按钮由人工点击
    SUCCEEDED = "succeeded"  # 成功：全部笔记 ID 均已提交成功（或已处于待发布）
    PARTIAL = "partial"  # 部分成功：有成功也有失败，可只补跑失败部分
    FAILED = "failed"  # 失败：整批判定为失败，或前置条件不满足
    CANCELED = "canceled"  # 已取消：绝对终态，没有任何出口


# UI 展示用的中文名。用显式字典而不是枚举文档或自动翻译，是为了让文案可以独立调整而
# 不必触碰状态机本身；键必须覆盖全部任务级状态，Job.label() 会直接按状态取值。
STATUS_LABEL = {
    JobStatus.DRAFT: "草稿",
    JobStatus.VALIDATING: "校验中",
    JobStatus.PENDING_CONFIRM: "待确认",
    JobStatus.QUEUED: "排队中",
    JobStatus.RUNNING: "执行中",
    JobStatus.PAUSED: "已暂停",
    JobStatus.NEEDS_MANUAL: "需要人工处理",
    JobStatus.AWAITING_PUBLISH: "待人工发布",
    JobStatus.SUCCEEDED: "成功",
    JobStatus.PARTIAL: "部分成功",
    JobStatus.FAILED: "失败",
    JobStatus.CANCELED: "已取消",
}

# 任务级终态：执行器不会自动继续推进的状态。
# 注意 PARTIAL 不在其中——部分成功仍允许补跑失败部分（PARTIAL → QUEUED），属于「可继续」
# 的中间态；调用方据此决定是否还显示「继续 / 补跑失败项」按钮。
TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED}

# 允许的状态迁移。RL = 需要人工处理; AP = 待人工发布。
# 结构为 {当前状态: 允许到达的状态集合}，Job.transition 只认这张表；表中没有的组合一律抛
# IllegalTransition（见 Job.transition），因此这里是整个工具唯一的「合法性定义处」。
# 三条贯穿全表的设计原则：
# 1) 只向前推进，不回退到更早的流程状态（例如不允许 RUNNING → VALIDATING、不允许回到
#    DRAFT），否则断点信息（cursor / batches / results）会与状态自相矛盾。
# 2) 除 CANCELED 外，每个状态都能取消；CANCELED 是唯一没有出口的终态，取消即终止。
# 3) 所有「卡住」的状态（PAUSED / NEEDS_MANUAL / AWAITING_PUBLISH / PARTIAL / FAILED）
#    都保留了回到 QUEUED 的边：人工处理完只需从断点续跑，而不是把整批重新跑一遍。
ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    # 草稿只能去校验，或直接丢弃。特意不提供 DRAFT → QUEUED：未经校验的任务绝不能进入
    # 投放流程，否则会拿着格式错误/重复的 ID 去执行。
    JobStatus.DRAFT: {JobStatus.VALIDATING, JobStatus.CANCELED},
    # 校验是同步判定：要么进入待确认，要么直接失败。没有「校验中 → 草稿」的回退口，
    # 需要改内容就新建任务，从而保证任务快照（note_ids 等）在整个生命周期内不可变。
    JobStatus.VALIDATING: {JobStatus.PENDING_CONFIRM, JobStatus.FAILED, JobStatus.CANCELED},
    # 只有用户点了确认才入队。“投放要花钱”这一操作必须由人工点头，这是最后一道闸门。
    JobStatus.PENDING_CONFIRM: {JobStatus.QUEUED, JobStatus.CANCELED},
    # 排队中：可能被执行器领取（RUNNING）、被用户暂停（PAUSED）或取消。
    # 不允许 QUEUED → 各种终态：还没跑过的任务不存在「成功/失败」，结论只能由执行产生。
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.CANCELED},
    # 执行中：可被暂停；遇到必须人工介入的情况转到 RL/AP；跑完落到三种结果终态。
    # 特意不提供 RUNNING → QUEUED：要回队列必须先经过 PAUSED，避免「一边还在跑、一边又被
    # 别的执行器领走」造成同一批次双跑。
    JobStatus.RUNNING: {
        JobStatus.PAUSED,
        JobStatus.NEEDS_MANUAL,
        JobStatus.AWAITING_PUBLISH,
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    },
    # 暂停后恢复 = 回到 QUEUED 重新排队（cursor 不变，所以从断点继续）。
    # 允许直接 FAILED，用于处理「恢复前发现账号已不可用」这类不能再跑的情况。
    JobStatus.PAUSED: {JobStatus.QUEUED, JobStatus.CANCELED, JobStatus.FAILED},
    # 人工接管完成（登录/验证/页面确认）后从断点回到队列，而不是整批重跑。
    # 之所以不设 NEEDS_MANUAL → RUNNING：人工介入后必须重新走一次入队，让调度侧重新
    # 分配执行顺序，也避免执行器进程已经退出却把状态直接改成执行中。
    JobStatus.NEEDS_MANUAL: {JobStatus.QUEUED, JobStatus.CANCELED, JobStatus.FAILED},
    # 页面上 ID 已填好，发布按钮由人工点击；人工确认后从断点继续剩余批次。
    # 允许直接进 SUCCEEDED/PARTIAL：若这一批就是最后一批，人工发布完成即等于整个任务结束，
    # 再绕一次 QUEUED 反而会让 UI 显示成「排队中」，与事实不符。
    JobStatus.AWAITING_PUBLISH: {
        JobStatus.QUEUED,
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    },
    # 部分成功 / 失败都允许回 QUEUED 做「补跑」：补跑时用 Job.failed_ids() 只挑没有成功的
    # ID，已成功条目不会因为补跑而重复投放；不允许直接回 RUNNING，是为了强制走一次入队流程。
    JobStatus.PARTIAL: {JobStatus.QUEUED, JobStatus.CANCELED},
    JobStatus.FAILED: {JobStatus.QUEUED, JobStatus.CANCELED},
    # SUCCEEDED 仍保留回 QUEUED 的边，用于「同一任务再投一次」的重复投放场景；
    # 此时执行器要靠 idempotency_keys 判断哪些批次已经做过，避免重复创建计划。
    JobStatus.SUCCEEDED: {JobStatus.QUEUED, JobStatus.CANCELED},
    # 已取消是绝对终态：空集合表示没有任何出口，想继续投放只能新建任务。
    JobStatus.CANCELED: set(),
}


class IllegalTransition(RuntimeError):
    """任务状态机拒绝的迁移。

    继承 RuntimeError 而不是 ValueError：非法迁移通常意味着「调用时序错了」（执行器还在
    跑就想把任务放回队列、UI 重复点击、人工处理完又走了一遍旧流程等），属于运行时状态问题，
    不是参数格式问题。调用方一般不应吞掉它，让它冒泡到日志才能暴露真正的逻辑缺陷。

    为什么选择抛异常而不是返回 False 或静默修正：状态机是唯一的正确性守门人。静默忽略会让
    任务停在非法状态上，导致批次记录、cursor 与 summary 全部失真，而且很难事后定位，因此
    必须在这里显式中断本次操作。
    """


class BatchStatus(str, Enum):
    """批次级状态：每个 BatchRecord（一批笔记 ID）在自身小状态机里的位置。

    任务级状态描述「这个任务整体在做什么」，批次级状态描述「第 N 批做到哪一步了」，
    两者必须分开保存：任务可能是 RUNNING，而其中第 1 批已 SUCCEEDED、第 2 批 FAILED。
    断点续跑正是靠批次级状态 + next_pending_batch 推导出「从哪一批接着跑」。
    """

    PENDING = "pending"  # 未开始（含被 reset_batches_after 重置回待执行的批次）
    RUNNING = "running"  # 执行器正在处理这一批
    SUCCEEDED = "succeeded"  # 该批全部成功：视为已翻页，不会重跑
    PARTIAL = "partial"  # 该批内部部分成功：多用于统计，续跑时仍会被重跑补齐
    FAILED = "failed"  # 该批判定失败：可在下一次 attempt 里被重跑
    SKIPPED = "skipped"  # 有意跳过（例如重复 ID 被剔除后批次为空）
    AWAITING_PUBLISH = "awaiting_publish"  # 该批已填好等待人工点击发布，视为已翻页


@dataclass
class NoteResult:
    """单条笔记 ID 的执行结果。

    只记录「一条 ID 在某一次尝试里的结论」，不保存页面细节——页面细节属于审计日志。
    粒度选在「ID」而不是「批次」，因为重试时失败的是个别 ID，按 ID 记录才能让补跑只挑
    没成功的条目，而不是整批重来。
    """

    note_id: str
    status: str  # success / failed / skipped / awaiting_publish
    reason: str = ""  # 失败或跳过的原因（给人看，不参与逻辑判断）
    batch: int = 0  # 产生这条结果的批次下标，便于把结果回溯到批次时间线
    attempt: int = 1  # 第几次尝试产生的结果；(note_id, attempt) 是结果的唯一身份

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 json.dumps 的普通字典，作为任务文件格式的一部分。

        手写而不是用 dataclasses.asdict，是为了让落盘字段名与顺序显式可控：
        历史文件已经按这个形状写出来了，字段顺序与命名一旦变化就会影响人工排查。
        """
        return {"note_id": self.note_id, "status": self.status, "reason": self.reason, "batch": self.batch, "attempt": self.attempt}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NoteResult":
        """从落盘字典重建对象，字段缺失或类型不符时退回安全默认值。

        为什么每个字段都做 str()/int() 转换并带默认值：任务文件可能由更早版本写出、也可能
        被人工编辑过。这里保证「坏数据也能读出来」——真正的格式错误由 JobStore.list 外层
        兜底跳过，而不是在这一层抛异常导致整个任务不可读。
        """
        return cls(
            note_id=str(data.get("note_id", "")),
            status=str(data.get("status", "failed")),
            reason=str(data.get("reason", "")),
            batch=int(data.get("batch", 0)),
            attempt=int(data.get("attempt", 1)),
        )


@dataclass
class BatchRecord:
    """一个批次的执行记录：批次级进度、尝试次数与起止时间。

    批次的划分由 Job.batch_size 决定（见 Job.batch_ids），BatchRecord 只保存结果，不保存
    成员 ID 列表——成员 ID 永远可以从 Job.note_ids + index 推导出来。这样做的好处是重试
    或调整批量时不需要同步修改批次记录，也避免了「同一份 ID 存两遍、两边不一致」的问题。
    """

    index: int  # 批次下标，从 0 开始；与 Job.note_ids 的分片一一对应
    status: BatchStatus = BatchStatus.PENDING
    count: int = 0  # 该批 ID 数量（冗余字段，便于离线排查，不参与断点判断）
    attempt: int = 1  # 该批结果属于第几次尝试，用于区分「上一轮的失败」与「本轮的重跑」
    message: str = ""  # 失败原因或人工处理说明，给人看
    started_at: str = ""  # 进入 RUNNING 的时刻；为空表示从未开始
    finished_at: str = ""  # 进入终态（成功/部分/失败/跳过/待发布）的时刻

    def to_dict(self) -> dict[str, Any]:
        """序列化。显式写 status.value 而不是枚举对象，保证 JSON 里是可读字符串。"""
        return {
            "index": self.index,
            "status": self.status.value,
            "count": self.count,
            "attempt": self.attempt,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchRecord":
        """反序列化，并对旧文件/手工编辑的字段做容错。

        status 用 BatchStatus(...) 严格构造（非法值会抛 ValueError，由 JobStore.list 统一
        跳过该文件），其余字段缺省即用默认值，避免历史任务因新增字段而读不出来。
        """
        return cls(
            index=int(data.get("index", 0)),
            status=BatchStatus(data.get("status", BatchStatus.PENDING.value)),
            count=int(data.get("count", 0)),
            attempt=int(data.get("attempt", 1)),
            message=str(data.get("message", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
        )


@dataclass
class Job:
    """一次投放任务及其完整执行快照。

    `cursor` 指向当前要处理的批次；`batches` 保存批次级状态；
    `results` 保存单条 ID 的历史结果；三者一起构成断点续跑所需的信息。

    断点续跑的整体语义（为什么这样设计）：
    投放是「慢且不可逆」的外部操作，中途必然会遇到登录过期、页面弹窗、需要人工点发布等
    情况。因此本对象被设计成可随时序列化、随时恢复的快照：
    - note_ids 一旦创建就不再修改，保证批次切分（batch_ids）在所有 attempt 中都稳定；
    - batches 记录每批走到哪里，next_pending_batch() 由此得到「断点」；
    - results 记录每条 ID 的结论，failed_ids() 由此得到「还需补跑的 ID」；
    - attempt 单调递增，新一轮尝试只是换了个 attempt 编号，旧结果不删除；
    - idempotency_keys 记录「这个任务的这批这次已经做过了」，恢复或重复入队时据此去重。

    因此「续跑」= 从 batches 里的断点继续，而不是把已成功的内容再做一遍——这正是外部
    平台最怕的重复投放。
    """

    job_id: str  # 形如 job_20240101_120000_ab12，同时是 JSON 文件名，创建后不再变化
    name: str  # 用户可见的任务名，仅展示用
    account: str  # 广告子账号（平台内的广告账户，用于页面流程里「选择子账号」这一步）
    profile: str = ""  # 登录账号（浏览器 Profile 隔离键）；空则回退「默认账号」
    advertiser: str = ""  # 广告主/账户标识，仅在平台上做选择时使用
    template: str = ""  # 使用的投放模板，决定执行器怎么填页面
    note_ids: list[str] = field(default_factory=list)  # 待投放的笔记 ID 全集，创建后不可变
    batch_size: int = BATCH_SIZE  # 每批多少条；改动它会改变批次划分，进而影响断点位置
    status: JobStatus = JobStatus.DRAFT  # 任务级状态，只能经 transition() 修改
    attempt: int = 1  # 当前尝试轮次，从 1 开始；重试时 start_attempt() 递增
    cursor: int = 0  # 下一个待执行批次下标：断点续跑的起点
    manual_reason: str = ""  # 转入 NEEDS_MANUAL/AWAITING_PUBLISH 的原因（给人工看）
    manual_kind: str = ""  # 人工事项的分类（如 login/captcha/publish），供 UI 决定提示方式
    manual_at: str = ""  # 转入人工状态的时间，便于判断「挂太久了」
    executor: str = ""  # 创建时写入的执行器版本，用于隔离不同代执行器的行为差异
    flow_name: str = ""  # 执行的流程包名（如 xiaohongshu_juguang）；空则用执行器默认流程
    created_at: str = field(default_factory=now_iso)  # 列表排序依据（倒序）
    updated_at: str = field(default_factory=now_iso)  # 每次状态/批次/结果变化都会刷新
    batches: list[BatchRecord] = field(default_factory=list)  # 批次级进度，断点判断的依据
    results: list[NoteResult] = field(default_factory=list)  # 单条 ID 的历史结果（跨 attempt 累积）
    # 幂等键：job:batch:attempt。命中即跳过，避免重试造成重复创建计划。
    idempotency_keys: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)  # 任务内事件时间线，append 顺序即因果顺序

    # ---------- 派生信息 ----------
    @property
    def batch_count(self) -> int:
        """批次总数（派生值，不落盘，避免与 note_ids / batch_size 不一致）。

        计算口径：向上取整 (len(note_ids) + batch_size - 1) // batch_size。
        - 空任务返回 0：没有 ID 就没有批次，UI 不应显示「1/1 批」，否则会误以为有活可干。
        - 非空任务至少为 1：不足 batch_size 的尾巴也算一整批，不会被丢掉。
        注意 batch_size 取自当前快照，若中途被改小/改大，批次划分会整体变化，所以本字段
        只在「同一任务同一 batch_size」的前提下与历史 batches 记录可比。
        """
        # 空任务返回 0；非空任务至少有一个批次，最后一批可以少于 batch_size。
        return max(1, (len(self.note_ids) + self.batch_size - 1) // self.batch_size) if self.note_ids else 0

    def batch_ids(self, index: int) -> list[str]:
        """取出第 index 批的笔记 ID 列表（按固定切分，因此可在任意时刻重算）。

        为什么按 index 现算而不是把 ID 存进 BatchRecord：批次内容唯一来源是 note_ids，
        存两份迟早会出现不一致；重试、恢复、重新分页时都能用同一个公式得到相同结果。
        越界时返回空列表（切片语义），调用方据此可以安全地对「已不存在的批次」做判断。
        """
        start = index * self.batch_size
        return self.note_ids[start : start + self.batch_size]

    def idempotency_key(self, index: int, attempt: int | None = None) -> str:
        """生成幂等键：job_id:批次下标:attempt。

        为什么需要它：投放计划一旦在平台上创建，重跑同一批就会重复创建。恢复执行、
        重复入队、用户手动重试都可能触发同一批再次执行，所以每批每次尝试都对应一个唯一的
        键；执行器开工前先查 keys，命中就跳过，从而把「至少执行一次」变成「最多执行一次」。
        为什么把 attempt 也编进键里：重试必须能真正重跑（否则失败批次永远被自己挡住），
        键里带上 attempt 就让新一轮尝试获得全新的键位。
        attempt 传 None 表示「用任务当前的 attempt」，这是常规调用方式；显式传入是为了
        让调用方能查询历史轮次或预告下一轮的键。
        """
        # 同一个任务、批次、尝试次数组成唯一键，避免恢复时重复执行。
        return f"{self.job_id}:{index}:{attempt if attempt is not None else self.attempt}"

    def results_for_attempt(self, attempt: int | None = None) -> list[NoteResult]:
        """筛出指定轮次的结果，默认取当前 attempt。

        用途：UI 只需要展示「本轮」成败；历史轮次的结果仍然保留在 results 里用于对比。
        传 None 表示当前轮，避免调用方到处重复写 self.attempt 这个默认值。
        """
        attempt = attempt if attempt is not None else self.attempt
        return [r for r in self.results if r.attempt == attempt]

    def failed_ids(self) -> list[str]:
        """返回还需要处理的 ID（按 note_ids 原始顺序，天然去重）。

        口径：success 与 awaiting_publish 都算「已达成」，其余（failed / skipped / 本轮
        没有任何结果）都算待处理。awaiting_publish 之所以算完成，是因为 ID 已经在页面上
        填好、等待人工点击发布，若再把它算成待补跑就会造成重复提交。
        返回值按 note_ids 顺序而不是失败时间顺序，保证补跑时的批次划分与首轮一致，断点语义不变。
        """
        done = {r.note_id for r in self.results if r.status in {"success", "awaiting_publish"}}
        return [x for x in self.note_ids if x not in done]

    def next_pending_batch(self) -> int | None:
        """返回第一个未成功完成的批次下标（断点）。

        判定为「已完成、可以跳过」的批次状态只有 SUCCEEDED、SKIPPED、AWAITING_PUBLISH：
        - SKIPPED：批次内容为空或有意的跳过，重跑没有意义；
        - AWAITING_PUBLISH：内容已提交到页面，等人工点发布，重跑会产生重复提交。
        PENDING / RUNNING / PARTIAL / FAILED 以及「记录缺失」都会被视为未完成并返回其下标。
        记录缺失（record is None）也算未完成，是为了兼容旧版本写出的、还没有批次记录的任务：
        宁可重跑一次该批，也不能因为缺少记录而误判成做完。

        返回值 None 表示所有批次都已完成（整个任务可判成功）；首个未完成批次既是最小下标，
        因此返回它即可用于续跑，无需额外排序。
        """
        for index in range(self.batch_count):
            record = self.batch_at(index)
            if record is None or record.status not in {BatchStatus.SUCCEEDED, BatchStatus.SKIPPED, BatchStatus.AWAITING_PUBLISH}:
                return index
        return None

    def batch_at(self, index: int) -> BatchRecord | None:
        """按下标查批次记录；不存在返回 None。

        batches 是列表而不是字典，是因为它要按 index 稳定序列化到 JSON。列表小（数量级为
        笔记数 / batch_size），线性查找足够；返回 None 让调用方显式处理「记录缺失」的情况
        （见 next_pending_batch 与 mark_batch 的兼容逻辑）。
        """
        for record in self.batches:
            if record.index == index:
                return record
        return None

    def summary(self) -> dict[str, int]:
        """Summarize the current attempt; publication must be confirmed to count.

        Retrying narrows note_ids to the retry scope. Earlier results remain in
        results for auditing, but must not inflate this scope's progress.
        """
        scope = set(self.note_ids)
        current = {r.note_id: r for r in self.results_for_attempt() if r.note_id in scope}
        success = sum(r.status == "success" for r in current.values())
        failed = sum(r.status == "failed" for r in current.values())
        return {"total": len(scope), "success": success, "failed": failed,
                "remaining": len(scope) - success}

    def label(self) -> str:
        """返回状态的中文展示名。

        直接按状态查 STATUS_LABEL；若将来新增状态而忘记补文案，这里会抛 KeyError 而不是
        显示空白，属于「早点暴露」的选择（新增状态时必然要顺带决定它怎么展示）。
        """
        return STATUS_LABEL[self.status]

    # ---------- 变更操作 ----------
    def transition(self, status: JobStatus, detail: str = "") -> None:
        """执行一次受约束的状态迁移，并把迁移写入事件历史。

        为什么所有状态改动都必须走这里：状态是断点续跑、UI 按钮可用性、执行器能否领取任务的
        唯一依据。把校验集中在一个入口，就能保证任何调用方都绕不过 ALLOWED_TRANSITIONS。

        行为约定：
        - 目标状态与当前状态相同：直接返回，不报错也不写事件。这样重复点击「暂停」或调度侧
          轮询时重复设置状态都是幂等的，不会把 events 刷满。
        - 目标状态不在白名单：抛 IllegalTransition，并带上前后状态名与 detail，便于定位是
          谁在什么流程里做了非法迁移。表里查不到当前状态（.get 返回空集合）同样会被拒绝。
        - detail 是给人看的补充说明，会随事件一起落盘；为空时错误信息里显示「无说明」。

        副作用只有三处：更新 status、刷新 updated_at、追加一条 kind="status" 的事件。
        不做任何持久化——落盘时机由调用方决定，便于把「状态+批次+结果」的多次修改合并成
        一次原子写，减少中间态被读到的窗口。
        """
        if status is self.status:
            return
        allowed = ALLOWED_TRANSITIONS.get(self.status, set())
        if status not in allowed:
            raise IllegalTransition(f"{self.status.value} → {status.value} 不是允许的状态迁移（{detail or '无说明'}）")
        previous, self.status = self.status, status
        self.updated_at = now_iso()
        self.add_event("status", f"{previous.value} → {status.value}", detail)

    def add_event(self, kind: str, message: str, detail: str = "") -> None:
        """向任务内事件时间线追加一条记录。

        与 AuditLog 的区别：事件是任务文件的一部分（随任务一起被读写、随任务一起删除），
        用来在 UI 上还原「这个任务经历了什么」；AuditLog 是跨任务的应用级日志。
        只追加、不覆盖也不裁剪：列表顺序就是真实发生顺序，所以展示时不要重排。
        """
        self.events.append({"at": now_iso(), "kind": kind, "message": message, "detail": detail})

    def ensure_batches(self) -> None:
        """为所有笔记 ID 建立批次记录，同时兼容旧任务缺少记录的情况。

        幂等：已存在的下标不会重建，因此可以安全地在每次开始执行前调用。
        为什么需要它：批次记录是断点判断的唯一依据（见 next_pending_batch），而旧版本写出的
        任务文件里可能没有 batches 字段。补齐时用当前 batch_ids 计算数量，保证 count 与
        实际切分一致；最后按 index 排序，让 JSON 里的批次顺序稳定、便于人工阅读与 diff。
        """
        known = {b.index for b in self.batches}
        for index in range(self.batch_count):
            if index not in known:
                self.batches.append(BatchRecord(index=index, count=len(self.batch_ids(index))))
        self.batches.sort(key=lambda b: b.index)

    def mark_batch(self, index: int, status: BatchStatus, message: str = "", attempt: int | None = None) -> None:
        """更新批次状态和时间戳；若记录不存在则先创建。

        时间戳的写法（started_at / finished_at）是有意区分「过程性」与「终局性」状态的：
        - 进入 RUNNING 时记 started_at：这是这一批真正开工的时刻，用于算单批耗时、判断是否
          卡死（UI 可以拿它和当前时间比，提示「已执行很久」）。
        - 进入终态（SUCCEEDED / PARTIAL / FAILED / SKIPPED / AWAITING_PUBLISH）时记
          finished_at：这是结论产生的时刻，用于计算总耗时、判断「重试是否比上次更快」。
        - 刻意不给 PENDING 记时间：待执行是初始态，也是 reset_batches_after 的重置结果，
          给它写时间戳会让「从未开始」和「重跑过」变得无法区分。
        由于进入 RUNNING 不会清空旧的 finished_at、进入终态也不会改 started_at，一次重跑
        只会覆盖对应的那一个时间戳，恰好反映「本轮的开始/结束」，无需额外字段。

        attempt 传 None 时取任务当前 attempt，使记录与结果落在同一轮次上，避免出现
        「批次说第 2 次、结果说第 1 次」这种自相矛盾的数据。count 每次重算，兼容 batch_size
        变动或 note_ids 被人工修正过的历史文件。
        """
        record = self.batch_at(index)
        if record is None:
            # 记录缺失（旧文件或新批次）时补建，保证重试/续跑后状态始终可查
            record = BatchRecord(index=index, count=len(self.batch_ids(index)))
            self.batches.append(record)
        record.status = status
        record.message = message
        record.attempt = attempt if attempt is not None else self.attempt
        record.count = len(self.batch_ids(index))
        if status is BatchStatus.RUNNING:
            record.started_at = now_iso()
        if status in {BatchStatus.SUCCEEDED, BatchStatus.PARTIAL, BatchStatus.FAILED, BatchStatus.SKIPPED, BatchStatus.AWAITING_PUBLISH}:
            record.finished_at = now_iso()
        self.updated_at = now_iso()

    def add_results(self, results: Iterable[NoteResult]) -> None:
        """按 (note_id, attempt) 覆盖写入，不会因为重试产生重复条目。

        为什么用「(note_id, attempt) 覆盖」而不是直接 append：
        - 同一条 ID 在同一轮尝试里可能被执行器先后报告两次（先失败后补报成功、或页面重试
          成功）。若只 append，就会同时存在 failed 与 success 两条同轮记录，summary 会把
          它既算失败又算成功，用户看到的数字与事实都不符。覆盖语义保证「同一 ID 在同一轮
          只有一个最终结论，后写者胜」。
        - attempt 参与键的一部分：第 2 轮的结果作为新条目追加，第 1 轮的记录原样保留，
          这样才能对比两轮失败原因，也才符合「重试不覆盖历史结果」的约束。

        实现上先建一次索引 {键: 位置}，因此对一批 N 条结果整体是 O(N)（而不是每条都去
        results 里线性查找，那是 O(N*M)）；索引在追加新条目时同步维护，保证同一输入里
        出现重复键时也能正确覆盖（后面的覆盖前面的，且不会追加两次）。
        输入是 Iterable，允许调用方直接传生成器，内存里不会为了这个方法多存一份列表。
        """
        index = {(r.note_id, r.attempt): position for position, r in enumerate(self.results)}
        for result in results:
            key = (result.note_id, result.attempt)
            if key in index:
                self.results[index[key]] = result
            else:
                index[key] = len(self.results)
                self.results.append(result)
        self.updated_at = now_iso()

    def start_attempt(self) -> None:
        """开启新一轮尝试：attempt 递增并记录事件。

        为什么用「递增 + 事件」而不是清空 results 重来：历史结果需要保留以供复盘，attempt
        只承担「给本轮结果打标签」的职责；幂等键里也带 attempt，所以递增后本轮可以重新执行
        上一轮已经做过的批次。
        注意本方法只改变轮次编号，不重置批次状态与 cursor——批次是否需要重跑由
        reset_batches_after 显式决定，两者分开才能表达「只补跑失败批次」这种精细操作。
        """
        self.attempt += 1
        self.updated_at = now_iso()
        self.add_event("attempt", f"开始第 {self.attempt} 次尝试")

    def reset_batches_after(self, index: int) -> None:
        """把 index 及其之后的批次重置为待执行，用于重试/续跑。

        为什么从 index 开始往后的都要重置（而不是只重置失败的那一批）：批次是顺序执行的，
        第 3 批没跑完就说明第 4 批之后根本没有可信的结论；留着它们「已完成」的旧状态会让
        next_pending_batch 提前返回 None，从而误判任务完成。
        同时清空 message 与两个时间戳，避免旧轮的失败原因和耗时留在新一轮记录上造成误读。
        index 小于 0 或超出范围时不报错（循环自然不匹配或全量重置），调用方无需先做边界检查；
        本方法也不改 cursor：cursor 属于任务级断点，由调度侧在执行前重新校准。
        """
        for record in self.batches:
            if record.index >= index:
                record.status = BatchStatus.PENDING
                record.message = ""
                record.started_at = ""
                record.finished_at = ""

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        """把手写字段逐个展开成落盘用的字典（任务 JSON 格式的唯一出口）。

        为什么手写而不是 dataclasses.asdict：asdict 会深拷贝整个对象图（大批任务时开销明显），
        并且会把枚举原样带出（无法直接 JSON 序列化）。手写能精确控制「哪些字段落盘、以什么
        形态落盘」，也便于以后只加字段而不动旧字段顺序，保证历史文件可继续被解析。
        status 写成 .value、batches/results 各自调用自己的 to_dict，都是为了产出纯 JSON
        基本类型；这里不做任何持久化，落盘由 JobStore.save 负责。
        """
        return {
            "job_id": self.job_id,
            "name": self.name,
            "account": self.account,
            "profile": self.profile,
            "advertiser": self.advertiser,
            "template": self.template,
            "note_ids": self.note_ids,
            "batch_size": self.batch_size,
            "status": self.status.value,
            "attempt": self.attempt,
            "cursor": self.cursor,
            "manual_reason": self.manual_reason,
            "manual_kind": self.manual_kind,
            "manual_at": self.manual_at,
            "executor": self.executor,
            "flow_name": self.flow_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "batches": [b.to_dict() for b in self.batches],
            "results": [r.to_dict() for r in self.results],
            "idempotency_keys": self.idempotency_keys,
            "events": self.events,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        """从落盘字典重建 Job，对本任务未认识的字段与新老格式差异做容错。

        逐字段用 .get(默认值) 的理由：任务文件可能由更早版本写出（缺少后加的字段），也可能
        缺少字段名拼写差异。这里给出与 dataclass 一致的默认值，使「老文件也能打开」。
        几个特意保留的宽松点：
        - created_at / updated_at 缺失时用 now_iso() 兜底，宁可时间不准也不让整个任务读不出来；
        - note_ids、idempotency_keys 强制转成 str 列表，防止 JSON 里混入数字型 ID 导致
          后续拼接幂等键或比对 ID 时出现类型不一致；
        - events 直接 list() 浅拷贝，事件是纯数据字典，不需要逐条转换。
        注意 status 用 JobStatus(...) 严格构造：无法识别的状态会抛 ValueError，由
        JobStore.list 决定是跳过该文件还是向调用方曝露，而不是在这里静默变成 DRAFT。
        """
        return cls(
            job_id=str(data.get("job_id", "")),
            name=str(data.get("name", "")),
            account=str(data.get("account", "")),
            profile=str(data.get("profile", "")),
            advertiser=str(data.get("advertiser", "")),
            template=str(data.get("template", "")),
            note_ids=[str(x) for x in data.get("note_ids", [])],
            batch_size=int(data.get("batch_size", BATCH_SIZE)),
            status=JobStatus(data.get("status", JobStatus.DRAFT.value)),
            attempt=int(data.get("attempt", 1)),
            cursor=int(data.get("cursor", 0)),
            manual_reason=str(data.get("manual_reason", "")),
            manual_kind=str(data.get("manual_kind", "")),
            manual_at=str(data.get("manual_at", "")),
            executor=str(data.get("executor", "")),
            flow_name=str(data.get("flow_name", "")),
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
            batches=[BatchRecord.from_dict(b) for b in data.get("batches", [])],
            results=[NoteResult.from_dict(r) for r in data.get("results", [])],
            idempotency_keys=[str(x) for x in data.get("idempotency_keys", [])],
            events=list(data.get("events", [])),
        )


class AuditLog:
    """JSONL 追加日志。写入失败不影响业务，只记录到内存列表。

    定位：这是旁路（side channel）——它只解释「发生过什么」，不参与任何状态推导。任务状态
    的唯一真相来源始终是 JobStore 里的任务 JSON，因此日志丢了不会导致任务跑不动。

    格式选择 JSONL（每行一个独立 JSON）的原因：可以边写边读、可以用普通文本工具 tail，
    并且单行损坏不会影响其他行；相比把事件写进一个 JSON 数组，无需为了追加而重写整个文件。
    """

    def __init__(self, path: Path = QUEUE_LOG) -> None:
        """绑定日志文件位置并创建写锁。

        默认为模块级 QUEUE_LOG，允许注入其他路径以便测试或让不同组件分开记录。
        锁在构造时创建（而不是每次 write 现取），保证同一实例的所有写操作共用同一把锁；
        构造本身不创建文件/目录，因此创建 AuditLog 对象不会有任何磁盘副作用——
        真正的 mkdir 推迟到第一次 write，避免「只是构造了日志对象就在磁盘上留下空目录」。
        """
        self.path = path
        # 只保护本实例的写操作：多线程同时 append 时若不串行化，可能出现两行内容交错
        # （尤其是短写与缓冲刷新叠加时），JSONL 的「一行一事件」前提就被破坏，日志无法解析。
        # 用普通 Lock 而不是 RLock：write 不会递归调用自身，没有可重入需求。
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        """追加一条审计事件：时间 + 事件名 + 任意业务字段。

        为什么写失败只忽略不抛错（except OSError: pass）：
        - 审计是旁路。磁盘满、文件被别的程序占用、目录无权限等都属于环境问题，如果在这里抛错，
          会把一次本来可以成功的投放操作整体打断——用户会看到「任务失败」，而真实原因只是
          日志写不进去，这是最糟的故障归因。
        - 本方法的调用点都在业务主流程（创建任务、入队、状态流转）之后，抛错会让调用方处于
          「业务已生效但异常已抛」的半完成状态，反而更需要人工介入。
        代价是日志可能静默缺失，因此排查问题时不能把日志当作完备的审计来源；真正关键的状态
        变化另有 Job.events 记录在任务文件里（它随任务一起原子落盘，不会被静默丢弃）。

        fields 用 **kwargs 展开，调用方写 audit.write("job_created", job_id=...) 即可；
        ensure_ascii=False 保证中文事件内容在文件里可读，不会被转成 unicode 转义序列。
        追加模式 "a" 与 mkdir(parents=True) 覆盖「首次运行日志目录还不存在」的情况。
        """
        record = {"at": now_iso(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # 见 docstring：审计失败不得影响任务本身，这里有意吞掉异常
                pass


class JobStore:
    """本地任务存储：每个任务一个 JSON 文件，线程安全 + 原子写入。

    文件损坏或单个任务读取失败时，list() 会跳过该文件，避免一个坏任务阻塞
    整个客户端启动；save() 则使用原子替换保护正在运行的任务。

    为什么是「每个任务一个文件」而不是一个总表：单任务文件让写入的原子粒度与业务粒度一致
    ——某次操作只会影响一个任务，不需要为了更新一个任务而重写整张表（那会让所有任务一起
    暴露在半写风险中）；任务之间也不会互相锁竞争，纯标准库下无需任何数据库依赖。

    线程模型：单进程内多线程安全（内部 RLock + _atomic_write）。多进程同时写同一个任务文件
    不受支持，因为临时文件名固定，两个进程会争用同一个 .tmp。
    """

    def __init__(self, root: Path = JOBS_DIR, audit: AuditLog | None = None) -> None:
        """绑定任务目录与审计对象，并建立线程锁。

        构造时不创建 root 目录：创建目录的副作用留给实际写盘（_atomic_write 会 mkdir），
        这样只读场景（list/load）不会在磁盘上凭空生成空目录，测试里也可以直接传一个不存在的
        路径来验证「空存储」行为。
        audit 缺省时才新建实例，允许调用方传入共享的 AuditLog（多个 JobStore 写同一份日志）
        或测试用的替身对象。
        """
        self.root = Path(root)  # 允许传 str，统一成 Path，避免调用方混淆两种写法
        # 允许注入审计对象（测试时可传哑实现），缺省共用默认日志文件
        self.audit = audit or AuditLog()
        # 用可重入锁（RLock）而不是普通 Lock，原因有两点：
        # 1) save/load/delete 内部都会调用 self.path()，而公共方法之间也可能互相组合
        #    （例如「读取-修改-保存」的调用方在持锁期间又调 save），普通 Lock 会自我死锁；
        # 2) 同进程内可能有 UI 线程与执行器线程同时操作同一个 job，锁保证一次写入不会被
        #    另一次写入打断，配合 _atomic_write 才能真正做到「要么旧内容、要么新内容」。
        # 注意：RLock 只覆盖单进程多线程；跨进程写同一文件不在支持范围内。
        self._lock = threading.RLock()

    def path(self, job_id: str) -> Path:
        """由 job_id 推出任务文件路径（<root>/<job_id>.json）。

        文件名的规则集中在这里，read/write/delete 都复用，避免某处写成别的后缀导致「保存成功
        但列表里看不到」。
        """
        return self.root / f"{job_id}.json"

    def save(self, job: Job) -> Path:
        """把任务快照原子地写入磁盘，返回实际写入的路径。

        为什么整个过程要持锁：读写都经过同一个 RLock，保证「序列化 + 写临时文件 + 替换」这一
        串动作不会被另一个线程的 save 穿插。若不加锁，两个线程会争用同一个 <job_id>.json.tmp
        （见 _atomic_write），可能出现 A 写的临时文件被 B 覆盖，最终落盘的是半合并内容。
        序列化在锁内完成，是为了保证落盘内容对应调用时的完整对象状态：如果在锁外 dumps，
        另一个线程可能在中间修改了 job，写出的就是不一致的混合快照。
        indent=2 只影响可读性；ensure_ascii=False 让中文任务名在文件里直接可读。
        返回路径而不是 None，方便调用方记录或展示「存到哪了」。
        """
        with self._lock:
            path = self.path(job.job_id)
            _atomic_write(path, json.dumps(job.to_dict(), ensure_ascii=False, indent=2))
        return path

    def load(self, job_id: str) -> Job:
        """按 job_id 读取任务并还原为 Job 对象。

        读也持锁：若恰好与 save 的替换动作并发，加锁能让读取串行化，避免在替换窗口内读到
        .tmp 残留或半状态（_atomic_write 已经保证文件完整，这里主要是防「读旧文件的同时
        另一个线程正要替换」带来的语义歧义）。
        与 list() 不同，这里让异常直接抛出：调用方明确要这一个任务，静默返回空对象会掩盖问题，
        由上层决定是提示用户还是忽略。
        """
        with self._lock:
            data = json.loads(self.path(job_id).read_text(encoding="utf-8"))
        return Job.from_dict(data)

    def exists(self, job_id: str) -> bool:
        """判断任务文件是否存在。

        只做轻量判断、不解析内容，用于「新建前查重」「按钮是否可用」等场景。不加锁：文件系统
        的存在性检查本身是原子的，且结果只作参考，不参与状态推导。
        """
        return self.path(job_id).exists()

    def list(self) -> list[Job]:
        """列出全部任务，按 created_at 倒序（最新的在最前）。

        为什么跳过单个坏文件而不是整体失败：这是客户端启动时加载任务列表的入口，只要有一个
        文件损坏（写到一半、被手工编辑坏、磁盘错误、枚举值来自更新版本），若整体抛错就会导致
        「打开软件看不到任何任务」，用户连删掉那个坏文件都做不到。因此这里逐文件 try/except，
        坏文件跳过，其余任务照常可用——可用性优先于完备性。
        捕获的异常类型对应实际可能出现的失败：
        - OSError：文件被占用、权限不足、读取途中被删除；
        - ValueError（含 json.JSONDecodeError 与枚举构造失败）：内容不是合法 JSON 或状态值不认识；
        - KeyError：结构化数据缺字段导致的解析问题。
        root 目录不存在时直接返回空列表，而不是先创建目录：list 是只读操作，不应产生副作用，
        目录会在真正 save 时才创建（_atomic_write 里的 mkdir）。
        排序放在最后统一做：读文件顺序不影响结果，倒序能让 UI 直接按返回顺序渲染。
        """
        jobs: list[Job] = []
        if not self.root.exists():
            return jobs
        for file in sorted(self.root.glob("*.json")):
            try:
                jobs.append(Job.from_dict(json.loads(file.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError):
                # 单个坏文件不应拖垮整个任务列表，跳过继续（见 docstring）
                continue
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def delete(self, job_id: str) -> None:
        """删除任务文件。

        missing_ok=True 让「删除一个已经被删掉的任务」成为幂等操作，UI 重复点击删除不会报错。
        与 save 共用同一把锁，避免删除与写入交错（例如删除后另一个线程又把文件写回来）。
        """
        with self._lock:
            self.path(job_id).unlink(missing_ok=True)

    def create(
        self,
        name: str,
        account: str,
        note_ids: list[str],
        template: str = "",
        advertiser: str = "",
        executor: str = "",
        flow_name: str = "",
        profile: str = "",
        batch_size: int = BATCH_SIZE,
    ) -> Job:
        """创建并立即持久化一个新任务，返回可继续进入队列生命周期的对象。

        关键设计：创建时就立刻 save，而不是等用户点「确认」才写盘。这样任务从诞生起就在磁盘
        上有一份可恢复的副本，UI 崩溃或程序被杀也不会让用户输入的 ID 列表白填。
        初始状态是 DRAFT（dataclass 默认值），且这里刻意不做状态迁移：创建 ≠ 校验通过，
        状态推进由调用方按状态机一步步做。

        ensure_batches 在保存前调用：让第一次写出的文件就带有完整的批次记录，后续断点判断
        不需要再区分「这批是没跑过还是记录缺失」。
        job_id 取「本地时间到秒 + 4 位随机十六进制」：时间部分便于人工排序与识别，随机后缀
        避免同一秒内创建多个任务时撞名（撞名会互相覆盖文件，也就是丢任务）。
        add_event 记录创建动作，让 UI 的时间线从任务的第一刻起就是完整的。
        最后写一条审计日志；因为 AuditLog 自身吞掉写失败，它不会影响创建结果。
        """
        job_id = f"job_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
        job = Job(
            job_id=job_id,
            name=name,
            account=account,
            advertiser=advertiser,
            template=template,
            note_ids=list(note_ids),
            batch_size=batch_size,
            executor=executor,
            flow_name=flow_name,
            profile=profile,
        )
        # 先补齐批次记录再落盘：保证磁盘上的任务从一开始就自带断点信息
        job.ensure_batches()
        job.add_event("created", f"创建任务，共 {len(note_ids)} 条 / {job.batch_count} 批")
        self.save(job)
        self.audit.write("job_created", job_id=job_id, account=account, count=len(note_ids), executor=executor)
        return job
