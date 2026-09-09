import os
from uuid import uuid4

import pytest

from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.platform.domain.enums import SchedulerStrategy


def _service() -> SchedulerService:
    return SchedulerService()


def test_scheduler_uses_process_affinity_for_default_cores(monkeypatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda process_id: {4, 2}, raising=False)

    plan = _service().create_plan(
        task_id=uuid4(),
        strategy=SchedulerStrategy.FIFO_BASELINE,
        tasks=[],
        on_log=lambda message, stream: None,
        on_progress=lambda percent, message: None,
        is_cancelled=lambda: False,
    )

    assert plan.core_ids == [2, 4]


def test_scheduler_does_not_treat_explicit_empty_cores_as_default() -> None:
    with pytest.raises(ValueError, match="core_ids must contain at least one CPU core"):
        _service().create_plan(
            task_id=uuid4(),
            strategy=SchedulerStrategy.FIFO_BASELINE,
            tasks=[],
            core_ids=[],
            on_log=lambda message, stream: None,
            on_progress=lambda percent, message: None,
            is_cancelled=lambda: False,
        )


def test_scheduler_rejects_cores_outside_process_affinity(monkeypatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda process_id: {2, 4}, raising=False)

    with pytest.raises(ValueError, match=r"unavailable CPU cores: \[3\]"):
        _service().create_plan(
            task_id=uuid4(),
            strategy=SchedulerStrategy.FIFO_BASELINE,
            tasks=[],
            core_ids=[3],
            on_log=lambda message, stream: None,
            on_progress=lambda percent, message: None,
            is_cancelled=lambda: False,
        )


def test_scheduler_routes_logs_and_maps_algorithm_progress() -> None:
    logs: list[tuple[str, str]] = []
    progress: list[tuple[int, str]] = []

    _service().create_plan(
        task_id=uuid4(),
        strategy=SchedulerStrategy.FIFO_BASELINE,
        tasks=[],
        on_log=lambda message, stream: logs.append((message, stream)),
        on_progress=lambda percent, message: progress.append((percent, message)),
        is_cancelled=lambda: False,
        progress_start=10,
        progress_end=30,
    )

    assert logs == [("planning 0 tasks with strategy FIFO_BASELINE", "co_debug.scheduler")]
    assert [percent for percent, _ in progress] == [14, 24, 30]
