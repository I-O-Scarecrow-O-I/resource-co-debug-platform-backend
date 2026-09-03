import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.platform.services.log_service import TaskLogService


@pytest.mark.parametrize("max_lines", [0, -1])
def test_max_lines_must_be_positive(max_lines: int) -> None:
    with pytest.raises(ValueError, match="max_lines must be greater than 0"):
        TaskLogService(max_lines=max_lines)


def test_log_history_round_trips_all_fields_and_timezone(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    timestamp = datetime(2026, 9, 3, 10, 20, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    monkeypatch.setattr("app.platform.services.log_service.utc_now", lambda: timestamp)
    task_id = uuid4()
    first = TaskLogService(max_lines=10, database_path=database_path)

    appended = first.append(task_id, "compiler output", stream="stderr", progress=42)
    first.close()

    second = TaskLogService(max_lines=10, database_path=database_path)
    assert second.history(task_id) == [appended]
    assert second.history(task_id)[0].timestamp.utcoffset() == timedelta(hours=5, minutes=30)
    second.close()


def test_log_history_is_trimmed_to_max_lines(tmp_path) -> None:
    service = TaskLogService(max_lines=2, database_path=tmp_path / "tasks.sqlite3")
    task_id = uuid4()

    for index in range(5):
        service.append(task_id, f"event-{index}")

    history = service.history(task_id)
    assert [event.sequence for event in history] == [4, 5]
    assert [event.message for event in history] == ["event-3", "event-4"]
    service.close()


def test_two_instances_append_unique_ordered_sequences(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    first = TaskLogService(max_lines=100, database_path=database_path)
    second = TaskLogService(max_lines=100, database_path=database_path)
    task_id = uuid4()

    with ThreadPoolExecutor(max_workers=8) as executor:
        events = list(
            executor.map(
                lambda index: (first if index % 2 else second).append(task_id, f"event-{index}"),
                range(40),
            )
        )

    assert sorted(event.sequence for event in events) == list(range(1, 41))
    assert [event.sequence for event in first.history(task_id)] == list(range(1, 41))
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_all_other_methods_fail() -> None:
    service = TaskLogService(max_lines=10)
    task_id = uuid4()
    service.close()
    service.close()

    with pytest.raises(RuntimeError, match="task log service is closed"):
        service.append(task_id, "event")
    with pytest.raises(RuntimeError, match="task log service is closed"):
        service.history(task_id)
    with pytest.raises(RuntimeError, match="task log service is closed"):
        service.subscribe(task_id)
    with pytest.raises(RuntimeError, match="task log service is closed"):
        service.subscribe_with_history(task_id)
    with pytest.raises(RuntimeError, match="task log service is closed"):
        service.unsubscribe(task_id, asyncio.Queue())
