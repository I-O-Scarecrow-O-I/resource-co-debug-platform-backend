import asyncio
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import UUID

from app.core.time import utc_now
from app.platform.schemas.common import LogEvent


@dataclass(slots=True, frozen=True)
class _LogSubscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[LogEvent]


class TaskLogService:
    def __init__(self, max_lines: int) -> None:
        self.max_lines = max_lines
        self._logs: dict[str, deque[LogEvent]] = defaultdict(lambda: deque(maxlen=max_lines))
        self._sequences: dict[str, int] = defaultdict(int)
        self._subscribers: dict[str, set[_LogSubscriber]] = defaultdict(set)
        self._lock = threading.Lock()

    def append(
        self,
        task_id: UUID | str,
        message: str,
        stream: str = "system",
        progress: int | None = None,
    ) -> LogEvent:
        key = str(task_id)
        stale_subscribers: list[_LogSubscriber] = []
        with self._lock:
            self._sequences[key] += 1
            event = LogEvent(
                task_id=task_id,
                sequence=self._sequences[key],
                timestamp=utc_now(),
                stream=stream,
                message=message,
                progress=progress,
            )
            self._logs[key].append(event)
            subscribers = list(self._subscribers[key])
            for subscriber in subscribers:
                try:
                    subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, event)
                except RuntimeError:
                    stale_subscribers.append(subscriber)
        for subscriber in stale_subscribers:
            self.unsubscribe(task_id, subscriber.queue)
        return event

    def history(self, task_id: UUID | str) -> list[LogEvent]:
        with self._lock:
            return list(self._logs[str(task_id)])

    def subscribe(self, task_id: UUID | str) -> asyncio.Queue[LogEvent]:
        queue: asyncio.Queue[LogEvent] = asyncio.Queue()
        subscriber = _LogSubscriber(loop=asyncio.get_running_loop(), queue=queue)
        with self._lock:
            self._subscribers[str(task_id)].add(subscriber)
        return queue

    def unsubscribe(self, task_id: UUID | str, queue: asyncio.Queue[LogEvent]) -> None:
        key = str(task_id)
        with self._lock:
            self._subscribers[key] = {
                subscriber for subscriber in self._subscribers[key] if subscriber.queue is not queue
            }

