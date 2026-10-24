from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import AppError
from app.modules.co_debug.debug.session import GdbMiCommandError
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.platform.domain.enums import TaskStatus, TaskType


class FailingRunManager:
    @staticmethod
    def contains(_task_id):
        return True

    @staticmethod
    async def run(_task_id):
        command = SimpleNamespace(text="-exec-run")
        record = SimpleNamespace(payload={"msg": "During startup program exited with code 126."})
        raise GdbMiCommandError(command, record)


@pytest.mark.asyncio
async def test_run_command_error_becomes_readable_app_error():
    task_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        task_type=TaskType.DEBUG,
        status=TaskStatus.RUNNING,
    )
    service = DebugSessionService(
        task_store=SimpleNamespace(require=lambda requested: task),
        session_manager=FailingRunManager(),
    )

    with pytest.raises(AppError) as caught:
        await service.run(task_id)

    assert caught.value.status_code == 400
    assert caught.value.message == (
        "Failed to run debug target: During startup program exited with code 126."
    )
