import asyncio
import ctypes
import io
import os
import sys
import threading
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.core.errors import AppError
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import (
    INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES,
    ProcessRunner,
)
from app.platform.services.task_execution import (
    InteractiveProcessSession,
    ManagedTaskContext,
    ManagedTaskResult,
)
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


class DelayedOpenProcessRunner(ProcessRunner):
    def __init__(self) -> None:
        self.process_created = threading.Event()
        self.allow_return = threading.Event()
        self.sessions: list[InteractiveProcessSession] = []

    async def open_interactive_process(self, command, cwd, on_output):
        session = await super().open_interactive_process(command, cwd, on_output)
        self.sessions.append(session)
        self.process_created.set()
        if not await asyncio.to_thread(self.allow_return.wait, 2):
            await session.terminate()
            raise AssertionError("interactive open was not released")
        return session


class FailingJobBindingProcessRunner(ProcessRunner):
    def __init__(self) -> None:
        self.spawned_pid: int | None = None

    def _create_process_group(self, process_id: int) -> None:
        self.spawned_pid = process_id
        return None


def _python_command(script: str, *args: str) -> list[str]:
    return [sys.executable, "-u", "-c", script, *args]


async def _wait_until(predicate, timeout: float = 3) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _service(
    tmp_path,
    process_runner: ProcessRunner | None = None,
) -> tuple[TaskService, WorkspaceService]:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    return (
        TaskService(
            workspace_service=workspace_service,
            task_store=TaskStore(tmp_path / "tasks.sqlite3"),
            log_service=TaskLogService(100, tmp_path / "logs.sqlite3"),
            process_runner=process_runner or ProcessRunner(),
            default_timeout_seconds=10,
        ),
        workspace_service,
    )


async def _create_project(workspace_service: WorkspaceService):
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("input.txt", "source")
    archive.seek(0)
    return await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )


async def _wait_for_terminal(service: TaskService, task_id: UUID):
    await _wait_until(
        lambda: service.require_task(task_id).status
        in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    )
    return service.require_task(task_id)


async def _wait_for_workspace_cleanup(task_root) -> None:
    await _wait_until(lambda: not task_root.exists())


async def _close(service: TaskService) -> None:
    await service.shutdown(grace_seconds=1)
    service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_interactive_session_streams_multiple_rounds_and_preserves_whitespace(
    tmp_path,
) -> None:
    output: list[tuple[str, str]] = []
    session = await ProcessRunner().open_interactive_process(
        command=_python_command(
            """
import sys
for line in sys.stdin:
    value = line.rstrip("\\r\\n")
    print(f"out:{value}  ", flush=True)
    print(f"err:{value}\\t ", file=sys.stderr, flush=True)
    if value == "quit":
        break
"""
        ),
        cwd=tmp_path,
        on_output=lambda message, stream: output.append((message, stream)),
    )

    await session.write("first")
    await asyncio.sleep(0.05)
    assert output == []
    await session.write("\n")
    await _wait_until(lambda: len(output) >= 2)
    await session.write("quit\n")
    exit_code = await session.wait()

    assert exit_code == 0
    assert await session.wait() == exit_code
    assert session.returncode == exit_code
    assert ("out:first  ", "stdout") in output
    assert ("err:first\t ", "stderr") in output


@pytest.mark.asyncio
async def test_interactive_session_wait_and_terminate_are_idempotent(tmp_path) -> None:
    output: list[tuple[str, str]] = []
    session = await ProcessRunner().open_interactive_process(
        command=_python_command(
            "import time; print('ready', flush=True); time.sleep(60)"
        ),
        cwd=tmp_path,
        on_output=lambda message, stream: output.append((message, stream)),
    )
    await _wait_until(lambda: ("ready", "stdout") in output)
    wait_task = asyncio.create_task(session.wait())

    await asyncio.gather(session.terminate(), session.terminate())
    exit_code = await wait_task

    await session.terminate()
    assert await session.wait() == exit_code
    assert session.returncode == exit_code


@pytest.mark.asyncio
async def test_interactive_session_accepts_large_mi_record(tmp_path) -> None:
    record_size = 128 * 1024
    output: list[tuple[str, str]] = []
    session = await ProcessRunner().open_interactive_process(
        command=_python_command(
            "import sys; print('x' * int(sys.argv[1]), flush=True)",
            str(record_size),
        ),
        cwd=tmp_path,
        on_output=lambda message, stream: output.append((message, stream)),
    )

    assert await session.wait() == 0
    assert output == [("x" * record_size, "stdout")]


@pytest.mark.asyncio
async def test_oversized_interactive_record_fails_wait_and_terminates_process(
    tmp_path,
) -> None:
    session = await ProcessRunner().open_interactive_process(
        command=_python_command(
            """
import sys
import time
sys.stdout.write("x" * int(sys.argv[1]) + "\\n")
sys.stdout.flush()
time.sleep(60)
""",
            str(INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES + 1),
        ),
        cwd=tmp_path,
        on_output=lambda _message, _stream: None,
    )

    with pytest.raises(
        AppError,
        match=(
            "interactive process output record exceeds "
            f"{INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES} bytes"
        ),
    ):
        await session.wait()
    assert session.returncode is not None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
async def test_windows_job_binding_failure_never_resumes_interactive_process(
    tmp_path,
) -> None:
    side_effect = tmp_path / "must-not-exist.txt"
    runner = FailingJobBindingProcessRunner()

    with pytest.raises(
        AppError,
        match="failed to assign interactive process to Windows job object",
    ):
        await runner.open_interactive_process(
            command=_python_command(
                """
import pathlib
import sys
import time
pathlib.Path(sys.argv[1]).write_text("ran", encoding="utf-8")
time.sleep(60)
""",
                str(side_effect),
            ),
            cwd=tmp_path,
            on_output=lambda _message, _stream: None,
        )

    assert side_effect.exists() is False
    assert runner.spawned_pid is not None
    assert _windows_process_is_running(runner.spawned_pid) is False


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
async def test_windows_suspended_launch_resumes_after_job_assignment(tmp_path) -> None:
    output: list[tuple[str, str]] = []
    session = await ProcessRunner().open_interactive_process(
        command=_python_command(
            """
import sys
for line in sys.stdin:
    print(f"reply:{line.rstrip()}", flush=True)
    break
"""
        ),
        cwd=tmp_path,
        on_output=lambda message, stream: output.append((message, stream)),
    )

    await session.write("hello\n")
    assert await session.wait() == 0
    assert output == [("reply:hello", "stdout")]


@pytest.mark.asyncio
async def test_multiple_interactive_sessions_are_isolated(tmp_path) -> None:
    outputs: list[list[tuple[str, str]]] = [[], []]
    script = """
import sys
prefix = sys.argv[1]
for line in sys.stdin:
    value = line.rstrip("\\r\\n")
    print(f"{prefix}:{value}", flush=True)
    if value == "quit":
        break
"""
    sessions = [
        await ProcessRunner().open_interactive_process(
            command=_python_command(script, prefix),
            cwd=tmp_path,
            on_output=lambda message, stream, index=index: outputs[index].append(
                (message, stream)
            ),
        )
        for index, prefix in enumerate(("one", "two"))
    ]

    await asyncio.gather(
        sessions[0].write("alpha\n"),
        sessions[0].write("beta\n"),
        sessions[1].write("gamma\n"),
    )
    await asyncio.gather(*(session.write("quit\n") for session in sessions))
    assert await asyncio.gather(*(session.wait() for session in sessions)) == [0, 0]

    assert {message for message, _ in outputs[0]} == {
        "one:alpha",
        "one:beta",
        "one:quit",
    }
    assert {message for message, _ in outputs[1]} == {"two:gamma", "two:quit"}


@pytest.mark.asyncio
async def test_managed_task_automatically_terminates_interactive_session(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    sessions: list[InteractiveProcessSession] = []
    ready = threading.Event()

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        session = await context.open_interactive_process(
            _python_command("import time; print('ready', flush=True); time.sleep(60)"),
            workspace,
            on_output=lambda message, _stream: ready.set()
            if message == "ready"
            else None,
        )
        sessions.append(session)
        assert await asyncio.to_thread(ready.wait, 2)
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "automatic-cleanup"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert completed.status == TaskStatus.SUCCEEDED
        assert sessions[0].returncode is not None
        assert any(
            event.message == "ready" and event.stream == "stdout"
            for event in service.log_service.history(task.id)
        )
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_output_callback_failure_fails_wait_and_task(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    sessions: list[InteractiveProcessSession] = []

    def fail_output(_message: str, _stream: str) -> None:
        raise RuntimeError("callback failed")

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        session = await context.open_interactive_process(
            _python_command("import time; print('ready', flush=True); time.sleep(60)"),
            workspace,
            on_output=fail_output,
        )
        sessions.append(session)
        await session.wait()
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "callback-failure"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert completed.status == TaskStatus.FAILED
        assert completed.error == (
            "interactive process output handling failed: callback failed"
        )
        assert sessions[0].returncode is not None
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_task_terminates_all_interactive_sessions_in_finally(
    tmp_path,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    sessions: list[InteractiveProcessSession] = []

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        sessions.extend(
            [
                await context.open_interactive_process(
                    _python_command("import time; time.sleep(60)"),
                    workspace,
                ),
                await context.open_interactive_process(
                    _python_command("import time; time.sleep(60)"),
                    workspace,
                ),
            ]
        )
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "multiple-cleanup"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert completed.status == TaskStatus.SUCCEEDED
        assert len(sessions) == 2
        assert all(session.returncode is not None for session in sessions)
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_cancel_while_interactive_process_is_opening_terminates_session(
    tmp_path,
) -> None:
    runner = DelayedOpenProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        await context.open_interactive_process(
            _python_command("import time; time.sleep(60)"),
            workspace,
        )
        raise AssertionError("cancelled open must not return to the executor")

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "open-cancel-race"],
            execute=execute,
        )
        assert await asyncio.to_thread(runner.process_created.wait, 2)

        cancellation = await service.cancel_task(task.id)
        runner.allow_return.set()
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert cancellation.cancel_requested is True
        assert completed.status == TaskStatus.CANCELLED
        assert len(runner.sessions) == 1
        assert runner.sessions[0].returncode is not None
    finally:
        runner.allow_return.set()
        await _close(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "shutdown"])
async def test_cancel_or_shutdown_terminates_managed_interactive_session(
    tmp_path,
    action: str,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    sessions: list[InteractiveProcessSession] = []
    ready = threading.Event()

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        session = await context.open_interactive_process(
            _python_command("import time; print('ready', flush=True); time.sleep(60)"),
            workspace,
            on_output=lambda message, _stream: ready.set()
            if message == "ready"
            else None,
        )
        sessions.append(session)
        await session.wait()
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    closed = False
    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", action],
            execute=execute,
        )
        assert await asyncio.to_thread(ready.wait, 2)
        if action == "cancel":
            await service.cancel_task(task.id)
        else:
            await service.shutdown(grace_seconds=1)
            closed = True
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert completed.status == TaskStatus.CANCELLED
        assert sessions[0].returncode is not None
    finally:
        if closed:
            service.close_resources_when_idle()
        else:
            await _close(service)


@pytest.mark.asyncio
async def test_managed_total_timeout_terminates_interactive_session(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    sessions: list[InteractiveProcessSession] = []

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        session = await context.open_interactive_process(
            _python_command("import time; time.sleep(60)"),
            workspace,
        )
        sessions.append(session)
        await session.wait()
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "timeout"],
            execute=execute,
            total_timeout_seconds=0.1,
        )
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert completed.status == TaskStatus.FAILED
        assert completed.error == "managed task timed out after 0.1 seconds"
        assert sessions[0].returncode is not None
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_interactive_process_rejects_workspace_escape(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        await context.open_interactive_process(
            _python_command("print('must not run')"),
            workspace,
            work_dir="..",
        )
        raise AssertionError("workspace escape must fail")

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "escape"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, task.id)
        await _wait_for_workspace_cleanup(project.root_path / "tasks" / str(task.id))

        assert completed.status == TaskStatus.FAILED
        assert completed.error == "work_dir must stay inside the project source directory"
    finally:
        await _close(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "work_dir", "expected_error"),
    [
        ([], ".", "interactive command must contain non-empty strings"),
        (
            _python_command("print('must not run')"),
            "missing",
            "work_dir does not exist: missing",
        ),
    ],
)
async def test_managed_interactive_process_validates_launch_inputs(
    tmp_path,
    command: list[str],
    work_dir: str,
    expected_error: str,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("interactive")
        await context.open_interactive_process(command, workspace, work_dir=work_dir)
        raise AssertionError("invalid launch must fail")

    try:
        task = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["interactive", "invalid"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == expected_error
    finally:
        await _close(service)


def _windows_process_is_running(process_id: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_type = ctypes.c_void_p
    kernel32.OpenProcess.restype = handle_type
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [handle_type, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.CloseHandle.argtypes = [handle_type]
    process_handle = kernel32.OpenProcess(0x1000, False, process_id)
    if not process_handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
            raise OSError(ctypes.get_last_error(), "failed to query process state")
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(process_handle)
