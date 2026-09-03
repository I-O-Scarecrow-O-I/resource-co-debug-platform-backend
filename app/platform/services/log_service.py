import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.core.time import utc_now
from app.platform.schemas.common import LogEvent


@dataclass(slots=True, frozen=True)
class _LogSubscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[LogEvent]


class TaskLogService:
    def __init__(self, max_lines: int, database_path: str | Path | None = None) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines must be greater than 0")
        self.max_lines = max_lines
        self._lock = threading.RLock()
        self._closed = False
        if database_path is None or str(database_path) == ":memory:":
            connection_path = ":memory:"
        else:
            connection_path = Path(database_path)
            connection_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(connection_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._subscribers: dict[str, set[_LogSubscriber]] = {}
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
        if self._closed:
            raise RuntimeError("task log service is closed")

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> LogEvent:
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

    def _history(self, task_id: str) -> list[LogEvent]:
        rows = self._connection.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY sequence ASC", (task_id,)
        ).fetchall()
        return [self._deserialize(row) for row in rows]

    def append(
        self,
        task_id: UUID | str,
        message: str,
        stream: str = "system",
        progress: int | None = None,
    ) -> LogEvent:
        key = str(task_id)
        with self._lock:
            self._ensure_open()
            event = LogEvent(
                task_id=task_id,
                sequence=0,
                timestamp=utc_now(),
                stream=stream,
                message=message,
                progress=progress,
            )
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                sequence = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_logs WHERE task_id = ?",
                    (key,),
                ).fetchone()[0]
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
                self._connection.execute(
                    "DELETE FROM task_logs WHERE task_id = ? AND sequence <= ?",
                    (key, event.sequence - self.max_lines),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

            stale_subscribers: list[_LogSubscriber] = []
            subscribers = list(self._subscribers.get(key, set()))
            for subscriber in subscribers:
                try:
                    subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, event)
                except RuntimeError:
                    stale_subscribers.append(subscriber)
            if stale_subscribers:
                self._subscribers[key].difference_update(stale_subscribers)
        return event

    def history(self, task_id: UUID | str) -> list[LogEvent]:
        with self._lock:
            self._ensure_open()
            return self._history(str(task_id))

    def subscribe(self, task_id: UUID | str) -> asyncio.Queue[LogEvent]:
        with self._lock:
            self._ensure_open()
            queue: asyncio.Queue[LogEvent] = asyncio.Queue()
            subscriber = _LogSubscriber(loop=asyncio.get_running_loop(), queue=queue)
            self._subscribers.setdefault(str(task_id), set()).add(subscriber)
        return queue

    def subscribe_with_history(
        self, task_id: UUID | str
    ) -> tuple[list[LogEvent], asyncio.Queue[LogEvent], int]:
        key = str(task_id)
        with self._lock:
            self._ensure_open()
            queue: asyncio.Queue[LogEvent] = asyncio.Queue()
            subscriber = _LogSubscriber(loop=asyncio.get_running_loop(), queue=queue)
            history = self._history(key)
            self._subscribers.setdefault(key, set()).add(subscriber)
            cutover_sequence = history[-1].sequence if history else 0
        return history, queue, cutover_sequence

    def unsubscribe(self, task_id: UUID | str, queue: asyncio.Queue[LogEvent]) -> None:
        key = str(task_id)
        with self._lock:
            self._ensure_open()
            self._subscribers[key] = {
                subscriber
                for subscriber in self._subscribers.get(key, set())
                if subscriber.queue is not queue
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

