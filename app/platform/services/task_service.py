import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

from app.core.errors import CancellationRequested
from app.core.time import utc_now
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import BuildTaskRequest, DebugTaskRequest, ScheduleExperimentRequest
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.scheduler_service import SchedulerService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


class TaskService:
    def __init__(
        self,
        workspace_service: WorkspaceService,
        task_store: TaskStore,
        log_service: TaskLogService,
        process_runner: ProcessRunner,
        scheduler_service: SchedulerService,
        default_timeout_seconds: int,
    ) -> None:
        self.workspace_service = workspace_service
        self.task_store = task_store
        self.log_service = log_service
        self.process_runner = process_runner
        self.scheduler_service = scheduler_service
        self.default_timeout_seconds = default_timeout_seconds
        self._processes: dict[UUID, asyncio.subprocess.Process] = {}

    async def create_build_task(self, request: BuildTaskRequest) -> TaskRecord:
        self.workspace_service.require_project(request.project_id)
        task = self._new_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.BUILD,
            command=request.command,
            metadata=request.metadata,
        )
        self._start_background(
            lambda: self._run_process_task(
                task_id=task.id,
                project_id=request.project_id,
                command=request.command,
                work_dir=request.work_dir,
                timeout_seconds=request.timeout_seconds or self.default_timeout_seconds,
            )
        )
        return task

    async def create_debug_task(self, request: DebugTaskRequest) -> TaskRecord:
        self.workspace_service.require_project(request.project_id)
        command = ["gdb", "--interpreter=mi2", request.executable_path, *request.args]
        task = self._new_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.DEBUG,
            command=command,
            metadata=request.metadata,
        )
        self._start_background(
            lambda: self._run_process_task(
                task_id=task.id,
                project_id=request.project_id,
                command=command,
                work_dir=request.work_dir,
                timeout_seconds=request.timeout_seconds or self.default_timeout_seconds,
            )
        )
        return task

    async def create_schedule_experiment(self, request: ScheduleExperimentRequest) -> TaskRecord:
        self.workspace_service.require_project(request.project_id)
        task = self._new_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["app.modules.co_debug.scheduler.scheduler.plan_tasks"],
            metadata=request.metadata,
        )
        self._start_background(lambda: self._run_schedule_experiment(task.id, request))
        return task

    def list_tasks(self) -> list[TaskRecord]:
        return self.task_store.list()

    def require_task(self, task_id: UUID) -> TaskRecord:
        return self.task_store.require(task_id)

    async def cancel_task(self, task_id: UUID) -> TaskRecord:
        task = self.task_store.request_cancel(task_id)
        process = self._processes.get(task_id)
        if process is not None and process.returncode is None:
            process.kill()
            self.log_service.append(task_id, "process cancellation requested")
        return task

    def _new_task(
        self,
        module: BackendModuleName,
        project_id: UUID,
        task_type: TaskType,
        command: list[str],
        metadata: dict,
    ) -> TaskRecord:
        task = TaskRecord(
            id=uuid4(),
            module=module,
            project_id=project_id,
            task_type=task_type,
            status=TaskStatus.PENDING,
            command=command,
            created_at=utc_now(),
            metadata=metadata,
        )
        self.task_store.save(task)
        return task

    def _start_background(self, task_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        thread = threading.Thread(target=lambda: asyncio.run(task_factory()), daemon=True)
        thread.start()

    async def _run_process_task(
        self,
        task_id: UUID,
        project_id: UUID,
        command: list[str],
        work_dir: str,
        timeout_seconds: int,
    ) -> None:
        task = self.task_store.require(task_id)
        task.status = TaskStatus.RUNNING
        task.started_at = utc_now()
        task.progress = 5
        self.log_service.append(task_id, f"task started: {command}", progress=5)

        try:
            cwd = self.workspace_service.resolve_work_dir(project_id, work_dir)
            result = await self.process_runner.run(
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                on_log=lambda message, stream: self.log_service.append(
                    task_id,
                    message,
                    stream=stream,
                ),
                is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
                on_process_started=lambda process: self._processes.__setitem__(task_id, process),
            )
            task.finished_at = utc_now()
            task.exit_code = result.exit_code
            task.elapsed_ms = result.elapsed_ms
            task.progress = 100
            if result.exit_code == 0:
                task.status = TaskStatus.SUCCEEDED
                task.result = {"success": True}
            else:
                task.status = TaskStatus.FAILED
                task.error = f"command exited with code {result.exit_code}"
            self.log_service.append(
                task_id,
                f"task finished with exit code {result.exit_code}",
                progress=100,
            )
        except CancellationRequested:
            self._mark_cancelled(task)
        except TimeoutError as exc:
            self._mark_failed(task, str(exc))
        except FileNotFoundError as exc:
            self._mark_failed(task, f"executable not found: {exc.filename}")
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            self._processes.pop(task_id, None)
            self.task_store.save(task)

    async def _run_schedule_experiment(
        self,
        task_id: UUID,
        request: ScheduleExperimentRequest,
    ) -> None:
        task = self.task_store.require(task_id)
        task.status = TaskStatus.RUNNING
        task.started_at = utc_now()
        task.progress = 10
        self.log_service.append(task_id, "schedule experiment started", progress=10)

        try:
            plan = self.scheduler_service.create_plan(
                task_id=task_id,
                strategy=request.strategy,
                tasks=request.tasks,
                is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
            )
            task.result = plan.model_dump(mode="json")
            task.status = TaskStatus.SUCCEEDED
            task.progress = 100
            task.finished_at = utc_now()
            task.elapsed_ms = 0
            self.log_service.append(task_id, "schedule experiment finished", progress=100)
        except CancellationRequested:
            self._mark_cancelled(task)
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            self.task_store.save(task)

    def _mark_cancelled(self, task: TaskRecord) -> None:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        task.error = "cancelled"
        self.log_service.append(task.id, "task cancelled", progress=task.progress)

    def _mark_failed(self, task: TaskRecord, message: str) -> None:
        task.status = TaskStatus.FAILED
        task.finished_at = utc_now()
        task.error = message
        self.log_service.append(task.id, f"task failed: {message}", progress=task.progress)

