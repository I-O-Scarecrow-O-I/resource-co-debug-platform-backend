import asyncio
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.core.time import utc_now
from app.platform.schemas.common import LogEvent


@dataclass(slots=True, frozen=True)
# 慢订阅以显式溢出信号结束，客户端可回查持久化历史。
class LogStreamOverflow:
    pass


# 订阅实例按对象身份比较，允许同一个循环为同一任务建立多条独立连接。
# 共享 task_id 不会把它们压成同一个集合成员。
@dataclass(slots=True, eq=False)
# 暂存和消费队列分开，生产线程不直接写入订阅方事件循环的队列。
class _LogSubscriber:
    # 循环与队列在订阅创建时绑定；订阅不能转交给另一个事件循环消费。
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[LogEvent | LogStreamOverflow]
    # 暂存区供线程安全的批量投递，最大积压与订阅队列同样受行数限制。
    # 溢出后只保留终止标记，避免订阅者误以为可以无缝接续。
    staging: deque[LogEvent | LogStreamOverflow] = field(default_factory=deque)
    drain_scheduled: bool = False
    overflowed: bool = False
    active: bool = True


# 历史以任务为边界持久化；订阅者仅存在于当前服务进程。
class TaskLogService:
    # 行数为每个任务的窗口，也为单个订阅的缓冲预算。
    # 它不是跨所有任务共享的全库日志配额。
    def __init__(self, max_lines: int, database_path: str | Path | None = None) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines must be greater than 0")
        self.max_lines = max_lines
        self._lock = threading.RLock()
        self._closed = False
        # 无持久化路径时使用独立内存日志；它只在当前实例有效。
        if database_path is None or str(database_path) == ":memory:":
            connection_path = ":memory:"
        else:
            connection_path = Path(database_path)
            connection_path.parent.mkdir(parents=True, exist_ok=True)
        # 同一连接供后台线程与 API 使用，外部锁承担串行访问责任。
        self._connection = sqlite3.connect(connection_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # 订阅索引用数据库一致的字符串任务键，但不持久化连接或队列。
        # 服务重启后客户端需要重新订阅，旧历史仍可从落盘记录读取。
        self._subscribers: dict[str, set[_LogSubscriber]] = {}
        # 序号与任务 ID 组成联合键，每个任务单独从首个序号开始计数。
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_logs (
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                stream TEXT NOT NULL,
                message TEXT NOT NULL,
                progress INTEGER,
                PRIMARY KEY (task_id, sequence)
            )
            """
        )
        self._connection.commit()

    def _ensure_open(self) -> None:
        # 调用方须在持锁时检查是否关闭，避免检查后到访问连接前被另一线程关闭。
        if self._closed:
            raise RuntimeError("task log service is closed")

    @staticmethod
    # 历史记录兼容 UUID 与非 UUID 的任务标识，不因解析旧 ID 丢掉一行日志。
    def _deserialize(row: sqlite3.Row) -> LogEvent:
        # 持久化键统一是字符串；可以无损还原的 UUID 在 API 层恢复其类型。
        task_id = row["task_id"]
        try:
            task_id = UUID(task_id)
        except ValueError:
            pass
        return LogEvent(
            task_id=task_id,
            sequence=row["sequence"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            stream=row["stream"],
            message=row["message"],
            progress=row["progress"],
        )

    # 历史按序号提供供重放，时间戳只用于展示。
    def _history(self, task_id: str) -> list[LogEvent]:
        rows = self._connection.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY sequence ASC", (task_id,)
        ).fetchall()
        return [self._deserialize(row) for row in rows]

    # 追加返回数据库接受的事件，便于测试和调用者引用实际序号。
    # 消息来源标签不参与决定历史保留顺序。
    def append(
        self,
        task_id: UUID | str,
        message: str,
        stream: str = "system",
        progress: int | None = None,
    ) -> LogEvent:
        # UUID 对象和对应字符串写入同一任务日志流。
        # 消息携带原先的任务标识，查找和排序使用规范化键。
        key = str(task_id)
        with self._lock:
            self._ensure_open()
            # 先建立消息快照，再在事务中分配本任务的序号。
            event = LogEvent(
                task_id=task_id,
                sequence=0,
                timestamp=utc_now(),
                stream=stream,
                message=message,
                progress=progress,
            )
            # 序号分配、插入和裁剪同一事务完成，避免两个生产者拿到相同序号。
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                # 同任务新增序号取现有最大值后继。
                # 旧记录按同一序号裁剪，因此序号不因历史窗口移动而重置。
                sequence = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_logs WHERE task_id = ?",
                    (key,),
                ).fetchone()[0]
                # 事件的序号以数据库分配为准，不把构造时的占位序号暴露给订阅者。
                # 失败回滚不会为下一次提交保留这个未发布序号。
                event = event.model_copy(update={"sequence": sequence})
                self._connection.execute(
                    """
                    INSERT INTO task_logs (task_id, sequence, timestamp, stream, message, progress)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        event.sequence,
                        event.timestamp.isoformat(),
                        event.stream,
                        event.message,
                        event.progress,
                    ),
                )
                # 裁剪不重编号，实时订阅和历史切换可继续使用原序号。
                self._connection.execute(
                    "DELETE FROM task_logs WHERE task_id = ? AND sequence <= ?",
                    (key, event.sequence - self.max_lines),
                )
                # 订阅只消费已提交的数据；事务失败时回滚且不触发实时分发。
                self._connection.commit()
            # 数据库错误在这里退出，调用者不会收到只存在于内存队列的事件。
            except Exception:
                self._connection.rollback()
                raise

            # 只有数据库已提交的事件才投递给实时订阅者。
            stale_subscribers: list[_LogSubscriber] = []
            subscribers = list(self._subscribers.get(key, set()))
            # 订阅循环可能在分发时关闭，批量摘除失效订阅而不中断其他接收者。
            for subscriber in subscribers:
                # 暂存拒绝只说明这个订阅无法再接收，不会反向撤销已经写好的日志。
                # 其他仍活动的订阅可以收到同一条已持久化事件。
                if not self._stage_event(subscriber, event):
                    stale_subscribers.append(subscriber)
            if stale_subscribers:
                self._subscribers[key].difference_update(stale_subscribers)
        return event

    # 本方法在日志锁内暂存事件，通过线程安全回调安排订阅循环排空。
    def _stage_event(self, subscriber: _LogSubscriber, event: LogEvent) -> bool:
        # 溢出属于订阅者终止状态，后续事件不会继续积压在它的暂存区。
        if subscriber.overflowed:
            return True
        # 暂存达到上限时用溢出标记替换积压，避免后台线程为慢客户端无限分配内存。
        if len(subscriber.staging) >= self.max_lines:
            subscriber.staging.clear()
            subscriber.staging.append(LogStreamOverflow())
            subscriber.overflowed = True
        # 尚有容量时保持写入顺序追加。
        # 这条路径只处理缓冲状态，生产方不会等待客户端实际发送。
        else:
            subscriber.staging.append(event)
        # 同一订阅只安排一次待执行的排空，后续事件进入相同暂存区。
        if subscriber.drain_scheduled:
            return True
        # 持锁设置排空标记，再把操作交给订阅者循环。
        # 其他线程附加事件时不需要为每条记录再调度一次。
        subscriber.drain_scheduled = True
        try:
            subscriber.loop.call_soon_threadsafe(self._drain_subscriber, subscriber)
        # 订阅循环已退出时标记无效，生产端将其从索引移除。
        except RuntimeError:
            # 已无可执行回调的循环通过返回值通知分发方移除。
            # 关闭的是订阅，不是写入它所观察的任务。
            subscriber.active = False
            return False
        return True

    # 此方法在订阅方循环中操作 asyncio 队列，锁保证与追加及撤销协调。
    def _drain_subscriber(self, subscriber: _LogSubscriber) -> None:
        with self._lock:
            # 清除已排程标志后，本次回调负责当前整批暂存。
            # 生产者与排空器都持同一把锁，因此不会抢走同一批待投递内容。
            subscriber.drain_scheduled = False
            # 撤销订阅可能比待处理回调先到；失效回调只清暂存。
            if not subscriber.active:
                subscriber.staging.clear()
                return
            # 队列操作使用非等待形式，不能在持锁区等 WebSocket 消费。
            # 缓冲无法容纳当前批次时走显式中止路径。
            while subscriber.staging:
                item = subscriber.staging.popleft()
                # 队列溢出后优先确保关闭信号可见，现有排队日志让位给该信号。
                if isinstance(item, LogStreamOverflow) or subscriber.queue.full():
                    while not subscriber.queue.empty():
                        try:
                            subscriber.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    # 结束标记使用队列正常读路径交付，让 WebSocket 无需额外监听控制通道。
                    subscriber.queue.put_nowait(LogStreamOverflow())
                    # 标记溢出后不再恢复这个订阅；后来产生的日志仍持续落库。
                    # 消费者需要重新获取保留窗口才能继续。
                    subscriber.staging.clear()
                    subscriber.overflowed = True
                    return
                try:
                    subscriber.queue.put_nowait(item)
                except asyncio.QueueFull:
                    subscriber.staging.clear()
                    subscriber.queue.put_nowait(LogStreamOverflow())
                    subscriber.overflowed = True
                    return

    # 历史为当前保留窗口，不保证包含任务从开始以来的全部日志。
    def history(self, task_id: UUID | str) -> list[LogEvent]:
        with self._lock:
            self._ensure_open()
            return self._history(str(task_id))

    # 此接口只订阅未来事件；需要连续历史的使用带快照的入口。
    def subscribe(self, task_id: UUID | str) -> asyncio.Queue[LogEvent | LogStreamOverflow]:
        with self._lock:
            self._ensure_open()
            queue: asyncio.Queue[LogEvent | LogStreamOverflow] = asyncio.Queue(
                maxsize=self.max_lines
            )
            # 必须由消费方所在循环注册，因为后续排空直接操作该循环的队列。
            # 后台写入线程只负责安排回调，不拥有这个队列。
            subscriber = _LogSubscriber(loop=asyncio.get_running_loop(), queue=queue)
            self._subscribers.setdefault(str(task_id), set()).add(subscriber)
        return queue

    # 读取历史和注册实时订阅持有同一把锁，封住两阶段之间漏日志的窗口。
    def subscribe_with_history(
        self, task_id: UUID | str
    ) -> tuple[list[LogEvent], asyncio.Queue[LogEvent | LogStreamOverflow], int]:
        # 历史与切换位置一起生成，序号为空时用零表示尚未写入日志。
        key = str(task_id)
        with self._lock:
            self._ensure_open()
            queue: asyncio.Queue[LogEvent | LogStreamOverflow] = asyncio.Queue(
                maxsize=self.max_lines
            )
            subscriber = _LogSubscriber(loop=asyncio.get_running_loop(), queue=queue)
            # 先取历史再注册，但两步都和 append 共用同一锁。
            # 这样前一条已提交事件必在历史窗口内，后一条走订阅投递。
            history = self._history(key)
            self._subscribers.setdefault(key, set()).add(subscriber)
            # 切换序号是已交付历史的尾界，调用方据此过滤重叠。
            cutover_sequence = history[-1].sequence if history else 0
        return history, queue, cutover_sequence

    # 以队列身份撤销一个订阅，不影响同一任务的其他 WebSocket。
    def unsubscribe(
        self,
        task_id: UUID | str,
        queue: asyncio.Queue[LogEvent | LogStreamOverflow],
    ) -> None:
        key = str(task_id)
        with self._lock:
            self._ensure_open()
            subscribers = self._subscribers.get(key, set())
            # 同任务可能有多名订阅者，注销只选择传入的确切队列。
            # 未命中时不使其他订阅失活。
            for subscriber in subscribers:
                # 先置 inactive 并清暂存，已经投递到事件循环的回调也不会继续填充。
                if subscriber.queue is queue:
                    subscriber.active = False
                    subscriber.staging.clear()
            self._subscribers[key] = {
                subscriber for subscriber in subscribers if subscriber.queue is not queue
            }

    # 关闭连接后所有读写需经过已关闭检查，避免意外恢复已销毁的日志服务。
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # 关闭后所有写入口必须经过状态检查，不允许旧 worker 在已关闭连接上继续工作。
            self._connection.close()
            self._closed = True

