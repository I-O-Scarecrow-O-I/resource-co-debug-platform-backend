import asyncio
from collections import defaultdict, deque
from uuid import UUID

from app.core.time import utc_now
from app.schemas.common import LogEvent


class TaskLogService:
    def __init__(self, max_lines: int) -> None:
        self.max_lines = max_lines
        self._logs: dict[str, deque[LogEvent]] = defaultdict(lambda: deque(maxlen=max_lines))
        self._subscribers: dict[str, set[asyncio.Queue[LogEvent]]] = defaultdict(set)

    def append(
        self,
        task_id: UUID | str,
        message: str,
        stream: str = "system",
        progress: int | None = None,
    ) -> LogEvent:
        key = str(task_id)
        event = LogEvent(
            task_id=task_id,
            timestamp=utc_now(),
            stream=stream,
            message=message,
            progress=progress,
        )
        self._logs[key].append(event)
        for queue in list(self._subscribers[key]):
            queue.put_nowait(event)
        return event

    def history(self, task_id: UUID | str) -> list[LogEvent]:
        return list(self._logs[str(task_id)])

    def subscribe(self, task_id: UUID | str) -> asyncio.Queue[LogEvent]:
        queue: asyncio.Queue[LogEvent] = asyncio.Queue()
        self._subscribers[str(task_id)].add(queue)
        return queue

    def unsubscribe(self, task_id: UUID | str, queue: asyncio.Queue[LogEvent]) -> None:
        self._subscribers[str(task_id)].discard(queue)
