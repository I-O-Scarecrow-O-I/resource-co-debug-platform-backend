import os
from uuid import uuid4

import pytest

from app.platform.domain.enums import SchedulerStrategy
from app.platform.services.log_service import TaskLogService
from app.platform.services.scheduler_service import SchedulerService


def _service() -> SchedulerService:
    return SchedulerService(log_service=TaskLogService(max_lines=100))


def test_scheduler_uses_process_affinity_for_default_cores(monkeypatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda process_id: {4, 2}, raising=False)

    plan = _service().create_plan(
        task_id=uuid4(),
        strategy=SchedulerStrategy.FIFO_BASELINE,
        tasks=[],
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
            is_cancelled=lambda: False,
        )
