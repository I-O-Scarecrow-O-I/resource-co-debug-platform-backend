import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

from app.core.errors import AppError, CancellationRequested
from app.core.time import utc_now
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import (
    BuildTaskRequest,
    DebugTaskRequest,
    ScheduleComparisonRequest,
    ScheduleExperimentRequest,
)
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.schedule_comparison_service import ScheduleComparisonService
from app.platform.services.schedule_execution_service import ScheduleExecutionService
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
        schedule_execution_service: ScheduleExecutionService,
        schedule_comparison_service: ScheduleComparisonService,
        default_timeout_seconds: int,
    ) -> None:
        self.workspace_service = workspace_service
        self.task_store = task_store
        self.log_service = log_service
        self.process_runner = process_runner
        self.scheduler_service = scheduler_service
        self.schedule_execution_service = schedule_execution_service
        self.schedule_comparison_service = schedule_comparison_service
        self.default_timeout_seconds = default_timeout_seconds
        self._background_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="backend-task",
        )
        self._background_futures: dict[UUID, Future[None]] = {}
        self._background_lock = threading.Lock()

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
            task.id,
            lambda: self._run_process_task(
                task_id=task.id,
                project_id=request.project_id,
                command=request.command,
                work_dir=request.work_dir,
                timeout_seconds=request.timeout_seconds or self.default_timeout_seconds,
                workspace_name="workspace",
            )
        )
        return task

    async def create_debug_task(self, request: DebugTaskRequest) -> TaskRecord:
        project = self.workspace_service.require_project(request.project_id)
        source_workspace = None
        if request.build_task_id is not None:
            build_task = self.task_store.require(request.build_task_id)
            if (
                build_task.project_id != request.project_id
                or build_task.task_type != TaskType.BUILD
            ):
                raise AppError("build_task_id must reference a build task in the same project")
            if build_task.status != TaskStatus.SUCCEEDED:
                raise AppError("build task must succeed before starting debug")
            source_workspace = self.workspace_service.resolve_task_workspace(
                request.project_id, request.build_task_id
            )
            executable = self.workspace_service.resolve_path_in_workspace(
                source_workspace, request.executable_path
            )
            source_root = source_workspace.resolve()
        else:
            executable = self.workspace_service.resolve_project_path(
                request.project_id, request.executable_path
            )
            source_root = project.source_path.resolve()
        if not executable.is_file():
            raise AppError(f"debug executable does not exist: {request.executable_path}")
        executable_relative_path = executable.relative_to(source_root)
        command = ["gdb", "--interpreter=mi2", str(executable_relative_path), *request.args]
        task = self._new_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.DEBUG,
            command=command,
            metadata=request.metadata,
        )
        self._start_background(
            task.id,
            lambda: self._run_process_task(
                task_id=task.id,
                project_id=request.project_id,
                command=command,
                work_dir=request.work_dir,
                timeout_seconds=request.timeout_seconds or self.default_timeout_seconds,
                source_workspace=source_workspace,
                executable_relative_path=str(executable_relative_path),
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
        self._start_background(task.id, lambda: self._run_schedule_experiment(task.id, request))
        return task

    async def create_schedule_comparison(self, request: ScheduleComparisonRequest) -> TaskRecord:
        self.workspace_service.require_project(request.project_id)
        task = self._new_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.SCHEDULE_COMPARISON,
            command=["app.platform.services.schedule_comparison_service.compare"],
            metadata=request.metadata,
        )
        self._start_background(task.id, lambda: self._run_schedule_comparison(task.id, request))
        return task

    def list_tasks(self) -> list[TaskRecord]:
        return self.task_store.list()

    def require_task(self, task_id: UUID) -> TaskRecord:
        return self.task_store.require(task_id)

    async def cancel_task(self, task_id: UUID) -> TaskRecord:
        task = self.task_store.request_cancel(task_id)
        with self._background_lock:
            future = self._background_futures.get(task_id)
        if task.status == TaskStatus.CANCELLED and future is not None:
            future.cancel()
        if task.status == TaskStatus.RUNNING:
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

    def _start_background(
        self,
        task_id: UUID,
        task_factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        def run_factory() -> None:
            asyncio.run(task_factory())

        future = self._background_executor.submit(run_factory)
        with self._background_lock:
            self._background_futures[task_id] = future
        future.add_done_callback(lambda completed: self._forget_background(task_id, completed))

    def _forget_background(self, task_id: UUID, future: Future[None]) -> None:
        with self._background_lock:
            if self._background_futures.get(task_id) is future:
                self._background_futures.pop(task_id, None)

    async def _run_process_task(
        self,
        task_id: UUID,
        project_id: UUID,
        command: list[str],
        work_dir: str,
        timeout_seconds: int,
        source_workspace=None,
        workspace_name: str | None = None,
        executable_relative_path: str | None = None,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return
        task.progress = 5
        self.log_service.append(task_id, f"task started: {command}", progress=5)

        task_workspace = None
        try:
            task_workspace = self.workspace_service.create_task_workspace(
                project_id,
                task_id,
                source_path=source_workspace,
                workspace_name=workspace_name,
            )
            cwd = self.workspace_service.resolve_work_dir_in_workspace(task_workspace, work_dir)
            process_command = list(command)
            if executable_relative_path is not None:
                process_command[2] = str(
                    self.workspace_service.resolve_path_in_workspace(
                        task_workspace, executable_relative_path
                    )
                )
            result = await self.process_runner.run(
                command=process_command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                on_log=lambda message, stream: self.log_service.append(
                    task_id,
                    message,
                    stream=stream,
                ),
                is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
            )
            task.finished_at = utc_now()
            task.exit_code = result.exit_code
            task.elapsed_ms = result.elapsed_ms
            task.progress = 100
            if result.exit_code == 0:
                task.status = TaskStatus.SUCCEEDED
                task.result = {"success": True}
                if task.task_type == TaskType.BUILD:
                    task.result["artifact"] = {
                        "build_task_id": str(task.id),
                        "workspace": "workspace",
                    }
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
            self.task_store.save(task)
            if task.task_type != TaskType.BUILD or task.status != TaskStatus.SUCCEEDED:
                self.workspace_service.cleanup_task_workspaces(project_id, task_id)

    async def _run_schedule_experiment(
        self,
        task_id: UUID,
        request: ScheduleExperimentRequest,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return
        task.progress = 10
        self.log_service.append(task_id, "schedule experiment started", progress=10)

        try:
            plan = self.scheduler_service.create_plan(
                task_id=task_id,
                strategy=request.strategy,
                tasks=request.tasks,
                is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
                core_ids=request.core_ids,
            )
            cwd = self.workspace_service.create_task_workspace(request.project_id, task_id)
            execution = await self.schedule_execution_service.execute(
                plan=plan,
                tasks=request.tasks,
                cwd=cwd,
                timeout_seconds=request.timeout_seconds or self.default_timeout_seconds,
                on_log=lambda message, stream: self.log_service.append(
                    task_id,
                    message,
                    stream=stream,
                ),
                on_progress=lambda percent, message: self._report_progress(
                    task,
                    percent,
                    message,
                ),
                is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
            )
            task.result = {
                **plan.model_dump(mode="json"),
                "execution": execution.model_dump(mode="json"),
            }
            task.elapsed_ms = execution.actual_makespan_ms
            task.exit_code = 0 if execution.all_succeeded else 1
            task.status = TaskStatus.SUCCEEDED if execution.all_succeeded else TaskStatus.FAILED
            if not execution.all_succeeded:
                task.error = "one or more scheduled tasks failed"
            task.progress = 100
            task.finished_at = utc_now()
            self.log_service.append(task_id, "schedule experiment finished", progress=100)
        except CancellationRequested:
            self._mark_cancelled(task)
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            self.task_store.save(task)
            self.workspace_service.cleanup_task_workspaces(request.project_id, task_id)

    async def _run_schedule_comparison(
        self,
        task_id: UUID,
        request: ScheduleComparisonRequest,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return
        task.progress = 5
        self.log_service.append(task_id, "schedule comparison started", progress=5)

        try:
            cwd = self.workspace_service.resolve_work_dir(request.project_id, ".")
            summary = await self.schedule_comparison_service.compare(
                task_id=task_id,
                workloads=request.workloads,
                core_ids=request.core_ids,
                cwd=cwd,
                timeout_seconds=request.timeout_seconds or self.default_timeout_seconds,
                on_log=lambda message, stream: self.log_service.append(
                    task_id,
                    message,
                    stream=stream,
                ),
                on_progress=lambda percent, message: self._report_progress(
                    task,
                    percent,
                    message,
                ),
                is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
                workspace_factory=lambda: self.workspace_service.create_task_workspace(
                    request.project_id, task_id
                ),
            )
            task.result = summary.model_dump(mode="json")
            task.elapsed_ms = sum(
                result.fifo.execution.actual_makespan_ms
                + result.optimized.execution.actual_makespan_ms
                for result in summary.workload_results
            )
            task.exit_code = 0 if summary.all_tasks_succeeded else 1
            task.status = TaskStatus.SUCCEEDED if summary.all_tasks_succeeded else TaskStatus.FAILED
            if not summary.all_tasks_succeeded:
                task.error = "one or more comparison tasks failed"
            task.progress = 100
            task.finished_at = utc_now()
            self.log_service.append(task_id, "schedule comparison finished", progress=100)
        except CancellationRequested:
            self._mark_cancelled(task)
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            self.task_store.save(task)
            self.workspace_service.cleanup_task_workspaces(request.project_id, task_id)

    def _mark_cancelled(self, task: TaskRecord) -> None:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        task.error = "cancelled"
        self.log_service.append(task.id, "task cancelled", progress=task.progress)

    def _report_progress(self, task: TaskRecord, percent: int, message: str) -> None:
        task.progress = percent
        self.log_service.append(
            task.id,
            message,
            stream="co_debug.executor",
            progress=percent,
        )

    def _mark_failed(self, task: TaskRecord, message: str) -> None:
        task.status = TaskStatus.FAILED
        task.finished_at = utc_now()
        task.error = message
        self.log_service.append(task.id, f"task failed: {message}", progress=task.progress)

