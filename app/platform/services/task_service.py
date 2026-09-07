import asyncio
import inspect
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.core.errors import AppError, CancellationRequested
from app.core.time import utc_now
from app.modules.co_debug.services.schedule_comparison_service import ScheduleComparisonService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.modules.code_generation.client import NaturalCCClientError, NaturalCCCreateError
from app.modules.code_generation.schemas import CodeGenerationTaskRequest, NaturalCCRunRequest
from app.modules.code_generation.service import NaturalCCService
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
from app.platform.services.task_execution import (
    PreparedProcess,
    TaskPreparationContext,
    TaskPreparer,
)
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)

_NATURALCC_EVENT_PROGRESS = {
    "run.started": 10,
    "model.requested": 20,
    "model.responded": 35,
    "tool.started": 55,
    "tool.finished": 70,
    "verification.finished": 85,
    "run.completed": 95,
}
_NATURALCC_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "budget_exhausted",
    "cancelled",
    "unsupported",
}
_NATURALCC_CANCEL_TIMEOUT_SECONDS = 2.0
_NATURALCC_CANCEL_ATTEMPTS = 3
_NATURALCC_CLEANUP_RETRY_ATTEMPTS = 3
_NATURALCC_CLEANUP_RETRY_SECONDS = 0.1
_NATURALCC_OPERATION_GOALS = {
    "completion": "Complete the requested code change.",
    "repair": "Repair the reported code issue.",
    "refactor": "Refactor the requested code while preserving behavior.",
}


class _ProcessTaskControl:
    """Coordinate cancellation with one task's subprocess launch boundary."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        worker_task: asyncio.Task[Any],
    ) -> None:
        self._lock = threading.Lock()
        self._loop = loop
        self._worker_task = worker_task
        self._cancel_requested = False
        self._launching = False
        self._process: object | None = None

    def request_cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            should_cancel_worker = not self._launching or self._process is not None
        if should_cancel_worker:
            self._cancel_worker()

    def begin_launch(self, is_cancelled: Callable[[], bool]) -> None:
        with self._lock:
            if self._cancel_requested or is_cancelled():
                raise CancellationRequested()
            self._launching = True

    def process_started(self, process: asyncio.subprocess.Process) -> None:
        with self._lock:
            self._process = process
            should_cancel_worker = self._cancel_requested
        if should_cancel_worker:
            self._cancel_worker()

    def finish_launch(self) -> None:
        with self._lock:
            self._launching = False
            self._process = None

    def _cancel_worker(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._worker_task.cancel)
        except RuntimeError:
            pass


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
        naturalcc_service: NaturalCCService | None = None,
        naturalcc_approve_execute: bool = False,
    ) -> None:
        self.workspace_service = workspace_service
        self.task_store = task_store
        self.log_service = log_service
        self.process_runner = process_runner
        self.scheduler_service = scheduler_service
        self.schedule_execution_service = schedule_execution_service
        self.schedule_comparison_service = schedule_comparison_service
        self.default_timeout_seconds = default_timeout_seconds
        self.naturalcc_service = naturalcc_service
        self.naturalcc_approve_execute = naturalcc_approve_execute
        self._background_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="backend-task",
        )
        self._background_futures: dict[UUID, Future[None]] = {}
        self._background_lock = threading.Lock()
        self._process_controls: dict[UUID, _ProcessTaskControl] = {}
        self._process_controls_lock = threading.Lock()
        self._naturalcc_runs: dict[UUID, str] = {}
        self._naturalcc_runs_lock = threading.Lock()
        self._naturalcc_loop: asyncio.AbstractEventLoop | None = None
        self._naturalcc_cleanup_retries: dict[UUID, asyncio.Task[None]] = {}
        self._naturalcc_cleanup_retries_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = "RUNNING"

    async def create_build_task(self, request: BuildTaskRequest) -> TaskRecord:
        self._ensure_accepting_tasks()
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
        self._ensure_accepting_tasks()
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
        self._ensure_accepting_tasks()
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
        self._ensure_accepting_tasks()
        self.workspace_service.require_project(request.project_id)
        task = self._new_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.SCHEDULE_COMPARISON,
            command=["app.modules.co_debug.services.schedule_comparison_service.compare"],
            metadata=request.metadata,
        )
        self._start_background(task.id, lambda: self._run_schedule_comparison(task.id, request))
        return task

    async def create_prepared_process_task(
        self,
        *,
        module: BackendModuleName,
        project_id: UUID,
        task_type: TaskType,
        prepare: TaskPreparer,
        metadata: dict | None = None,
        timeout_seconds: int | None = None,
    ) -> TaskRecord:
        """Create a task whose module prepares a command inside its task workspace."""
        self._ensure_accepting_tasks()
        self.workspace_service.require_project(project_id)
        if not self._is_async_preparer(prepare):
            raise AppError("prepare must be async")
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=[],
            metadata=metadata or {},
        )
        self._start_background(
            task.id,
            lambda: self._run_process_task(
                task_id=task.id,
                project_id=project_id,
                command=None,
                work_dir=None,
                timeout_seconds=timeout_seconds or self.default_timeout_seconds,
                workspace_name="workspace",
                prepare=prepare,
            ),
        )
        return task

    async def create_code_generation_task(
        self,
        request: CodeGenerationTaskRequest,
    ) -> TaskRecord:
        self._ensure_accepting_tasks()
        self.workspace_service.require_project(request.project_id)
        task = self._new_task(
            module=BackendModuleName.CODE_GENERATION,
            project_id=request.project_id,
            task_type={
                "completion": TaskType.CODE_GENERATION,
                "repair": TaskType.CODE_REPAIR,
                "refactor": TaskType.CODE_REFACTOR,
            }[request.operation.value],
            command=["naturalcc", request.operation.value],
            metadata={},
        )
        self._start_background(
            task.id,
            lambda: self._run_code_generation_task(task.id, request),
        )
        return task

    def list_tasks(self) -> list[TaskRecord]:
        return self.task_store.list()

    def require_task(self, task_id: UUID) -> TaskRecord:
        return self.task_store.require(task_id)

    def list_task_artifacts(self, task_id: UUID) -> list[tuple[str, int]]:
        task = self._require_succeeded_artifact_task(task_id)
        return self.workspace_service.list_task_artifacts(task.project_id, task.id)

    def resolve_task_artifact(self, task_id: UUID, artifact_path: str) -> Path:
        task = self._require_succeeded_artifact_task(task_id)
        return self.workspace_service.resolve_task_artifact(
            task.project_id,
            task.id,
            artifact_path,
        )

    async def startup(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "RUNNING":
                raise AppError("task service is not accepting tasks")
            self._naturalcc_loop = asyncio.get_running_loop()
            recovered = self.task_store.recover_interrupted_tasks()
        pending_cleanup_tasks = [
            task
            for task in self.task_store.list()
            if self._naturalcc_cleanup_pending(task)
        ]
        if pending_cleanup_tasks:
            recovered_cleanup = await asyncio.gather(
                *(self._recover_naturalcc_cleanup(task) for task in pending_cleanup_tasks),
                return_exceptions=True,
            )
            for task, cleaned in zip(pending_cleanup_tasks, recovered_cleanup, strict=True):
                if cleaned is False and self._naturalcc_run_id(task.id) is not None:
                    self._schedule_naturalcc_cleanup_retry(task.id)

        for task in recovered:
            try:
                self._log_final_state(task)
            except Exception:
                logger.warning("failed to log recovered task %s", task.id, exc_info=True)
            if self.workspace_service is None or self._naturalcc_cleanup_pending(
                self.task_store.require(task.id)
            ):
                continue
            try:
                self.workspace_service.cleanup_task_workspaces(task.project_id, task.id)
            except Exception:
                logger.warning(
                    "failed to clean recovered task workspace %s",
                    task.id,
                    exc_info=True,
                )

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "CLOSED":
                return
            self._lifecycle_state = "CLOSING"

        deadline = asyncio.get_running_loop().time() + max(grace_seconds, 0)
        try:
            for task in self.task_store.list():
                if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                    try:
                        await self.cancel_task(task.id, cancel_deadline=deadline)
                    except Exception:
                        logger.warning(
                            "failed to cancel task during shutdown: %s",
                            task.id,
                            exc_info=True,
                        )

            with self._background_lock:
                futures = list(self._background_futures.values())
            for future in futures:
                future.cancel()

            while True:
                with self._background_lock:
                    active_futures = [
                        future for future in self._background_futures.values() if not future.done()
                    ]
                if not active_futures or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.05)
        finally:
            try:
                await self._stop_naturalcc_cleanup_retries()
                self._background_executor.shutdown(wait=False, cancel_futures=True)
            finally:
                with self._lifecycle_lock:
                    self._naturalcc_loop = None
                    self._lifecycle_state = "CLOSED"

    def close_resources_when_idle(self) -> None:
        """Close persistent resources immediately or after timed-out workers finish."""
        with self._background_lock:
            futures = list(self._background_futures.values())
        with self._naturalcc_cleanup_retries_lock:
            retries = list(self._naturalcc_cleanup_retries.values())
        if not futures and not retries:
            self.task_store.close()
            self.log_service.close()
            return

        def close_when_done(_: object) -> None:
            with self._background_lock, self._naturalcc_cleanup_retries_lock:
                if self._background_futures or self._naturalcc_cleanup_retries:
                    return
            self.task_store.close()
            self.log_service.close()

        for future in futures:
            future.add_done_callback(close_when_done)
        for retry in retries:
            retry.add_done_callback(close_when_done)

    def can_close_resources(self) -> bool:
        with (
            self._lifecycle_lock,
            self._background_lock,
            self._naturalcc_cleanup_retries_lock,
        ):
            return self._lifecycle_state == "CLOSED" and all(
                future.done() for future in self._background_futures.values()
            ) and not self._naturalcc_cleanup_retries

    async def cancel_task(
        self,
        task_id: UUID,
        *,
        cancel_deadline: float | None = None,
    ) -> TaskRecord:
        task, changed = self.task_store.request_cancel_with_transition(task_id)
        with self._process_controls_lock:
            process_control = self._process_controls.get(task_id)
        if task.cancel_requested and process_control is not None:
            process_control.request_cancel()
        with self._background_lock:
            future = self._background_futures.get(task_id)
        if task.status == TaskStatus.CANCELLED and future is not None:
            future.cancel()
        if task.cancel_requested:
            await self._cancel_naturalcc_run(task_id, deadline=cancel_deadline)
        if changed:
            try:
                if task.status == TaskStatus.CANCELLED:
                    self._log_final_state(task)
                else:
                    self.log_service.append(task_id, "process cancellation requested")
            except Exception:
                logger.warning("failed to log task cancellation: %s", task_id, exc_info=True)
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

    def _require_succeeded_artifact_task(self, task_id: UUID) -> TaskRecord:
        task = self.require_task(task_id)
        if task.task_type not in {
            TaskType.BUILD,
            TaskType.CODE_GENERATION,
            TaskType.CODE_REPAIR,
            TaskType.CODE_REFACTOR,
        }:
            raise AppError("artifacts are only available for build and code generation tasks")
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError("artifacts are only available for succeeded tasks")
        return task

    def _start_background(
        self,
        task_id: UUID,
        task_factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        def run_factory() -> None:
            asyncio.run(task_factory())

        with self._lifecycle_lock:
            try:
                self._ensure_accepting_tasks()
            except AppError:
                self.task_store.request_cancel(task_id)
                raise
            future = self._background_executor.submit(run_factory)
            with self._background_lock:
                self._background_futures[task_id] = future
        future.add_done_callback(lambda completed: self._forget_background(task_id, completed))

    def _ensure_accepting_tasks(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "RUNNING":
                raise AppError("task service is shutting down")

    def _forget_background(self, task_id: UUID, future: Future[None]) -> None:
        with self._background_lock:
            if self._background_futures.get(task_id) is future:
                self._background_futures.pop(task_id, None)
        if not future.cancelled() and (exception := future.exception()) is not None:
            logger.error(
                "background task failed: %s",
                task_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def _run_process_task(
        self,
        task_id: UUID,
        project_id: UUID,
        command: list[str] | None,
        work_dir: str | None,
        timeout_seconds: int,
        source_workspace=None,
        workspace_name: str | None = None,
        executable_relative_path: str | None = None,
        prepare: TaskPreparer | None = None,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return
        worker_task = asyncio.current_task()
        assert worker_task is not None
        process_control = _ProcessTaskControl(asyncio.get_running_loop(), worker_task)
        with self._process_controls_lock:
            self._process_controls[task_id] = process_control

        task_workspace = None
        try:
            task = self._report_progress(
                task,
                5,
                "task preparation started" if prepare is not None else f"task started: {command}",
            )
            task_workspace = self.workspace_service.create_task_workspace(
                project_id,
                task_id,
                source_path=source_workspace,
                workspace_name=workspace_name,
            )
            if prepare is not None:
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                async with asyncio.timeout_at(deadline):
                    task, prepared = await self._prepare_process(task, task_workspace, prepare)
                    result = await self._run_controlled_process(
                        task_id=task_id,
                        task_workspace=task_workspace,
                        command=list(task.command),
                        work_dir=prepared.work_dir,
                        timeout_seconds=timeout_seconds,
                        executable_relative_path=None,
                        process_control=process_control,
                    )
            else:
                assert command is not None
                assert work_dir is not None
                result = await self._run_controlled_process(
                    task_id=task_id,
                    task_workspace=task_workspace,
                    command=list(command),
                    work_dir=work_dir,
                    timeout_seconds=timeout_seconds,
                    executable_relative_path=executable_relative_path,
                    process_control=process_control,
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
        except CancellationRequested:
            self._mark_cancelled(task)
        except asyncio.CancelledError:
            self._mark_cancelled(task)
        except TimeoutError as exc:
            self._mark_failed(
                task,
                str(exc) or f"task timed out after {timeout_seconds} seconds",
            )
        except FileNotFoundError as exc:
            self._mark_failed(task, f"executable not found: {exc.filename}")
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            with self._process_controls_lock:
                if self._process_controls.get(task_id) is process_control:
                    self._process_controls.pop(task_id, None)
            self._finalize_with_cleanup(
                task,
                lambda finalized: (
                    self.workspace_service.cleanup_task_workspaces(project_id, task_id)
                    if finalized is None
                    or finalized.task_type != TaskType.BUILD
                    or finalized.status != TaskStatus.SUCCEEDED
                    else None
                ),
            )

    async def _run_controlled_process(
        self,
        *,
        task_id: UUID,
        task_workspace: Path,
        command: list[str],
        work_dir: str,
        timeout_seconds: int,
        executable_relative_path: str | None,
        process_control: _ProcessTaskControl,
    ):
        cwd = self.workspace_service.resolve_work_dir_in_workspace(task_workspace, work_dir)
        if executable_relative_path is not None:
            command[2] = str(
                self.workspace_service.resolve_path_in_workspace(
                    task_workspace, executable_relative_path
                )
            )
        def is_cancelled() -> bool:
            return self.task_store.require(task_id).cancel_requested

        process_control.begin_launch(is_cancelled)
        try:
            return await self.process_runner.run(
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                on_log=lambda message, stream: self.log_service.append(
                    task_id,
                    message,
                    stream=stream,
                ),
                is_cancelled=is_cancelled,
                on_process_started=process_control.process_started,
            )
        finally:
            process_control.finish_launch()

    async def _run_code_generation_task(
        self,
        task_id: UUID,
        request: CodeGenerationTaskRequest,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return

        timeout_seconds = request.timeout_seconds or self.default_timeout_seconds
        remote_run_id: str | None = None
        create_outcome_unknown = False
        remote_run_created = False
        try:
            if self.naturalcc_service is None:
                raise AppError("NaturalCC service is not configured")
            task_workspace = self.workspace_service.create_task_workspace(
                request.project_id,
                task_id,
                workspace_name="workspace",
            )
            for target_file in request.target_files:
                target = self.workspace_service.resolve_path_in_workspace(
                    task_workspace,
                    target_file,
                )
                if not target.is_file():
                    raise AppError(f"target file does not exist: {target_file}")

            task = self._report_progress(
                task,
                5,
                "NaturalCC task workspace created",
                stream="code_generation.adapter",
            )
            budget = request.budget.model_dump(exclude_none=True)
            budget["max_seconds"] = min(budget.get("max_seconds", timeout_seconds), timeout_seconds)
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                create_outcome_unknown = True
                self.task_store.merge_metadata(task_id, {"cleanup_pending": True})
                created = await self.naturalcc_service.create_run(
                    workspace=task_workspace,
                    request=NaturalCCRunRequest(
                        goal=self._naturalcc_goal(request.operation.value, request.instruction),
                        target_files=request.target_files,
                        budget=budget,
                    ),
                )
                remote_run_id = created.get("run_id")
                if not isinstance(remote_run_id, str) or not remote_run_id:
                    raise NaturalCCCreateError(outcome_unknown=True)
                remote_run_created = True
                create_outcome_unknown = False
                self._set_naturalcc_run(task_id, remote_run_id)
                self.task_store.merge_metadata(
                    task_id,
                    {"naturalcc_run_id": remote_run_id, "cleanup_pending": True},
                )
                self._raise_if_cancel_requested(task_id)

                approvals = ["write"]
                if self.naturalcc_approve_execute:
                    approvals.append("execute")
                for risk in approvals:
                    await self.naturalcc_service.approve(remote_run_id, risk)
                    self._raise_if_cancel_requested(task_id)

                remaining_timeout_seconds = max(
                    0.1,
                    deadline - asyncio.get_running_loop().time(),
                )
                task, state = await self._run_and_poll_naturalcc(
                    task,
                    remote_run_id,
                    timeout_seconds=remaining_timeout_seconds,
                )
                if not self._naturalcc_state_is_terminal(state):
                    original_state = dict(state)
                    await self._cancel_naturalcc_run(task_id)
                    state = original_state
                else:
                    self._mark_naturalcc_terminal_confirmed(task_id)
                self._apply_naturalcc_state(
                    task,
                    request.operation.value,
                    remote_run_id,
                    state,
                )
        except CancellationRequested:
            if remote_run_id is not None:
                await self._cancel_naturalcc_run(task_id)
            self._mark_cancelled(task)
        except NaturalCCCreateError as exc:
            create_outcome_unknown = exc.outcome_unknown
            self._mark_failed(task, "NaturalCC service request failed")
        except TimeoutError:
            if remote_run_id is not None:
                await self._cancel_naturalcc_run(task_id)
            self._mark_failed(task, "NaturalCC task timed out")
        except NaturalCCClientError:
            if remote_run_id is not None:
                await self._cancel_naturalcc_run(task_id)
            self._mark_failed(task, "NaturalCC service request failed")
        except AppError as exc:
            if remote_run_id is not None:
                await self._cancel_naturalcc_run(task_id)
            self._mark_failed(task, str(exc))
        except Exception:
            if remote_run_id is not None:
                await self._cancel_naturalcc_run(task_id)
            self._mark_failed(task, "NaturalCC task failed")
        finally:
            self._clear_naturalcc_run(task_id)
            self._finalize_with_cleanup(
                task,
                lambda _: self._finish_naturalcc_cleanup(
                    task_id=task_id,
                    project_id=request.project_id,
                    remote_run_created=remote_run_created,
                    create_outcome_unknown=create_outcome_unknown,
                ),
            )
            if self._naturalcc_cleanup_pending(self.task_store.require(task_id)) and (
                self._naturalcc_run_id(task_id) is not None
            ):
                self._schedule_naturalcc_cleanup_retry(task_id)

    async def _prepare_process(
        self,
        task: TaskRecord,
        workspace: Path,
        prepare: TaskPreparer,
    ) -> tuple[TaskRecord, PreparedProcess]:
        self._raise_if_cancel_requested(task.id)

        def report_progress(percent: int, message: str) -> None:
            nonlocal task
            task = self._report_progress(task, percent, message, stream="module.prepare")

        context = TaskPreparationContext(
            task_id=task.id,
            project_id=task.project_id,
            workspace=workspace,
            _append_log=lambda message, stream: self.log_service.append(
                task.id,
                message,
                stream=stream,
            ),
            _report_progress=report_progress,
            _is_cancelled=lambda: self.task_store.require(task.id).cancel_requested,
        )
        prepared = await prepare(context)
        self._raise_if_cancel_requested(task.id)
        self._validate_prepared_process(prepared)
        updated = self.task_store.update_command_if_running(task.id, prepared.command)
        if updated is None:
            raise CancellationRequested()
        self._raise_if_cancel_requested(task.id)
        return updated, prepared

    @staticmethod
    def _validate_prepared_process(prepared: object) -> None:
        if not isinstance(prepared, PreparedProcess):
            raise AppError("prepare must return PreparedProcess")
        if not prepared.command or not all(
            isinstance(item, str) and item for item in prepared.command
        ):
            raise AppError("prepared command must contain non-empty strings")
        if not isinstance(prepared.work_dir, str):
            raise AppError("prepared work_dir must be a string")

    @staticmethod
    def _is_async_preparer(prepare: TaskPreparer) -> bool:
        return inspect.iscoroutinefunction(prepare) or (
            callable(prepare) and inspect.iscoroutinefunction(prepare.__call__)
        )

    async def _run_schedule_experiment(
        self,
        task_id: UUID,
        request: ScheduleExperimentRequest,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return

        try:
            task = self._report_progress(task, 10, "schedule experiment started")
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
        except CancellationRequested:
            self._mark_cancelled(task)
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            self._finalize_with_cleanup(
                task,
                lambda _: self.workspace_service.cleanup_task_workspaces(
                    request.project_id, task_id
                ),
            )

    async def _run_schedule_comparison(
        self,
        task_id: UUID,
        request: ScheduleComparisonRequest,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return

        try:
            task = self._report_progress(task, 5, "schedule comparison started")
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
        except CancellationRequested:
            self._mark_cancelled(task)
        except Exception as exc:
            self._mark_failed(task, str(exc))
        finally:
            self._finalize_with_cleanup(
                task,
                lambda _: self.workspace_service.cleanup_task_workspaces(
                    request.project_id, task_id
                ),
            )

    def _mark_cancelled(self, task: TaskRecord) -> None:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        task.error = "cancelled"

    def _report_progress(
        self,
        task: TaskRecord,
        percent: int,
        message: str,
        stream: str = "co_debug.executor",
    ) -> TaskRecord:
        latest = self.task_store.update_progress(task.id, percent)
        task.progress = latest.progress
        if latest.status != TaskStatus.RUNNING or latest.cancel_requested:
            raise CancellationRequested()
        self.log_service.append(
            task.id,
            message,
            stream=stream,
            progress=latest.progress,
        )
        return latest

    def _set_naturalcc_run(self, task_id: UUID, run_id: str) -> None:
        with self._naturalcc_runs_lock:
            self._naturalcc_runs[task_id] = run_id

    def _clear_naturalcc_run(self, task_id: UUID) -> None:
        with self._naturalcc_runs_lock:
            self._naturalcc_runs.pop(task_id, None)

    def _naturalcc_run_id(self, task_id: UUID) -> str | None:
        with self._naturalcc_runs_lock:
            run_id = self._naturalcc_runs.get(task_id)
        if run_id is not None:
            return run_id
        metadata_run_id = self.task_store.require(task_id).metadata.get("naturalcc_run_id")
        return metadata_run_id if isinstance(metadata_run_id, str) else None

    async def _cancel_naturalcc_run(
        self,
        task_id: UUID,
        *,
        deadline: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        run_id = self._naturalcc_run_id(task_id)
        if run_id is None or self.naturalcc_service is None:
            return False, {}
        loop = asyncio.get_running_loop()
        end = min(deadline, loop.time() + _NATURALCC_CANCEL_TIMEOUT_SECONDS) if deadline else (
            loop.time() + _NATURALCC_CANCEL_TIMEOUT_SECONDS
        )
        last_state: dict[str, Any] = {}
        for attempt in range(_NATURALCC_CANCEL_ATTEMPTS):
            remaining = end - loop.time()
            if remaining <= 0:
                break
            timeout_seconds = min(0.5, remaining)
            try:
                await self.naturalcc_service.cancel(run_id, timeout_seconds=timeout_seconds)
            except Exception:
                logger.warning("NaturalCC cancellation request failed for task %s", task_id)
            remaining = end - loop.time()
            if remaining > 0:
                try:
                    last_state = await self.naturalcc_service.get_run(
                        run_id,
                        timeout_seconds=min(0.5, remaining),
                    )
                    if self._naturalcc_state_is_terminal(last_state):
                        self._mark_naturalcc_terminal_confirmed(task_id)
                        return True, last_state
                except Exception:
                    logger.warning(
                        "NaturalCC cancellation confirmation failed for task %s",
                        task_id,
                    )
            if attempt < _NATURALCC_CANCEL_ATTEMPTS - 1:
                await asyncio.sleep(min(0.1 * (attempt + 1), max(0, end - loop.time())))
        return False, last_state

    @staticmethod
    def _naturalcc_state_is_terminal(state: dict[str, Any]) -> bool:
        return state.get("status") in _NATURALCC_TERMINAL_STATUSES

    @staticmethod
    def _naturalcc_cleanup_pending(task: TaskRecord) -> bool:
        return task.metadata.get("cleanup_pending") is True

    def _clear_naturalcc_cleanup_pending(self, task_id: UUID) -> None:
        self.task_store.merge_metadata(task_id, {"cleanup_pending": False})

    def _mark_naturalcc_terminal_confirmed(self, task_id: UUID) -> None:
        self.task_store.merge_metadata(task_id, {"naturalcc_terminal_confirmed": True})

    def _finish_naturalcc_cleanup(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        remote_run_created: bool,
        create_outcome_unknown: bool,
    ) -> None:
        task = self.task_store.require(task_id)
        if not self._naturalcc_cleanup_pending(task):
            return
        if task.status == TaskStatus.SUCCEEDED:
            if task.metadata.get("naturalcc_terminal_confirmed") is True:
                self._clear_naturalcc_cleanup_pending(task_id)
            return
        if remote_run_created and task.metadata.get("naturalcc_terminal_confirmed") is not True:
            return
        if not remote_run_created and create_outcome_unknown:
            return
        try:
            self.workspace_service.cleanup_task_workspaces(project_id, task_id)
        except Exception:
            logger.warning("failed to clean NaturalCC workspace %s", task_id, exc_info=True)
            return
        self._clear_naturalcc_cleanup_pending(task_id)

    async def _recover_naturalcc_cleanup(self, task: TaskRecord) -> bool:
        latest = self.task_store.require(task.id)
        if latest.metadata.get("naturalcc_terminal_confirmed") is not True:
            confirmed, _ = await self._cancel_naturalcc_run(task.id)
            if not confirmed:
                return False
        latest = self.task_store.require(task.id)
        if latest.status == TaskStatus.SUCCEEDED:
            self._clear_naturalcc_cleanup_pending(task.id)
            return True
        self._finish_naturalcc_cleanup(
            task_id=task.id,
            project_id=task.project_id,
            remote_run_created=True,
            create_outcome_unknown=False,
        )
        return not self._naturalcc_cleanup_pending(self.task_store.require(task.id))

    def _schedule_naturalcc_cleanup_retry(self, task_id: UUID) -> None:
        with self._lifecycle_lock:
            loop = self._naturalcc_loop
            if self._lifecycle_state != "RUNNING" or loop is None or loop.is_closed():
                return
        try:
            loop.call_soon_threadsafe(self._start_naturalcc_cleanup_retry, task_id)
        except RuntimeError:
            return

    def _start_naturalcc_cleanup_retry(self, task_id: UUID) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "RUNNING":
                return
        with self._naturalcc_cleanup_retries_lock:
            existing = self._naturalcc_cleanup_retries.get(task_id)
            if existing is not None and not existing.done():
                return
            retry = asyncio.create_task(self._retry_naturalcc_cleanup(task_id))
            self._naturalcc_cleanup_retries[task_id] = retry
        retry.add_done_callback(
            lambda completed: self._forget_naturalcc_cleanup_retry(task_id, completed)
        )

    def _forget_naturalcc_cleanup_retry(
        self,
        task_id: UUID,
        retry: asyncio.Task[None],
    ) -> None:
        with self._naturalcc_cleanup_retries_lock:
            if self._naturalcc_cleanup_retries.get(task_id) is retry:
                self._naturalcc_cleanup_retries.pop(task_id, None)

    async def _retry_naturalcc_cleanup(self, task_id: UUID) -> None:
        for _ in range(_NATURALCC_CLEANUP_RETRY_ATTEMPTS):
            await asyncio.sleep(_NATURALCC_CLEANUP_RETRY_SECONDS)
            with self._lifecycle_lock:
                if self._lifecycle_state != "RUNNING":
                    return
            try:
                if await self._recover_naturalcc_cleanup(self.task_store.require(task_id)):
                    return
            except Exception:
                logger.warning("NaturalCC cleanup retry failed for task %s", task_id, exc_info=True)

    async def _stop_naturalcc_cleanup_retries(self) -> None:
        with self._naturalcc_cleanup_retries_lock:
            retries = list(self._naturalcc_cleanup_retries.values())
        for retry in retries:
            retry.cancel()
        if retries:
            await asyncio.gather(*retries, return_exceptions=True)

    @staticmethod
    def _naturalcc_goal(operation: str, instruction: str) -> str:
        return f"{_NATURALCC_OPERATION_GOALS[operation]}\n\n{instruction}"

    def _raise_if_cancel_requested(self, task_id: UUID) -> None:
        if self.task_store.require(task_id).cancel_requested:
            raise CancellationRequested()

    async def _run_and_poll_naturalcc(
        self,
        task: TaskRecord,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> tuple[TaskRecord, dict[str, Any]]:
        assert self.naturalcc_service is not None
        run_task = asyncio.create_task(
            self.naturalcc_service.run(run_id, timeout_seconds=timeout_seconds)
        )
        after = 0
        try:
            while not run_task.done():
                if self.task_store.require(task.id).cancel_requested:
                    await self._cancel_naturalcc_run(task.id)
                    raise CancellationRequested()
                events = await self.naturalcc_service.events(run_id, after=after)
                task, after = self._report_naturalcc_events(task, events, after)
                await asyncio.sleep(0.25)
            state = await run_task
            events = await self.naturalcc_service.events(run_id, after=after)
            task, _ = self._report_naturalcc_events(task, events, after)
            return task, state
        finally:
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)

    def _report_naturalcc_events(
        self,
        task: TaskRecord,
        response: dict[str, Any],
        after: int,
    ) -> tuple[TaskRecord, int]:
        events = response.get("events")
        if not isinstance(events, list):
            return task, after
        for event in events:
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, int):
                after = max(after, sequence)
            event_type = event.get("type")
            progress = (
                _NATURALCC_EVENT_PROGRESS.get(event_type)
                if isinstance(event_type, str)
                else None
            )
            if progress is None:
                progress = event.get("progress")
                if not isinstance(progress, int):
                    payload = event.get("payload")
                    progress = payload.get("progress") if isinstance(payload, dict) else None
            message = (
                f"NaturalCC event: {event_type}"
                if isinstance(event_type, str) and event_type in _NATURALCC_EVENT_PROGRESS
                else "NaturalCC event received"
            )
            if isinstance(progress, int) and 0 <= progress <= 95 and progress > task.progress:
                task = self._report_progress(
                    task,
                    progress,
                    message,
                    stream="code_generation.adapter",
                )
            else:
                self.log_service.append(
                    task.id,
                    message,
                    stream="code_generation.adapter",
                    progress=task.progress,
                )
        return task, after

    def _apply_naturalcc_state(
        self,
        task: TaskRecord,
        operation: str,
        run_id: str,
        state: dict[str, Any],
    ) -> None:
        status = state.get("status")
        if status == "cancelled":
            self._mark_cancelled(task)
            return
        if status == "completed":
            task.status = TaskStatus.SUCCEEDED
            task.finished_at = utc_now()
            task.exit_code = 0
            task.progress = 100
            task.result = {
                "operation": operation,
                "naturalcc_run_id": run_id,
                "final_answer": state.get("final_answer", ""),
                "changed_files": self._changed_files(state),
            }
            return
        if status in {"paused", "waiting_approval"}:
            self._mark_failed(
                task,
                f"NaturalCC run remained {status}; cancelled for the approval safety policy",
            )
            return
        if status in {"failed", "budget_exhausted", "unsupported"}:
            self._mark_failed(task, f"NaturalCC run ended with status {status}")
            return
        self._mark_failed(task, "NaturalCC run returned an unsupported status")

    @staticmethod
    def _changed_files(state: dict[str, Any]) -> list[str]:
        changed_files = state.get("changed_files")
        if not isinstance(changed_files, list):
            working_state = state.get("working_state")
            changed_files = (
                working_state.get("changed_files", []) if isinstance(working_state, dict) else []
            )
        return [item for item in changed_files if isinstance(item, str)]

    def _mark_failed(self, task: TaskRecord, message: str) -> None:
        task.status = TaskStatus.FAILED
        task.finished_at = utc_now()
        task.error = message

    def _log_final_state(self, task: TaskRecord) -> None:
        if task.status == TaskStatus.SUCCEEDED:
            message = "task succeeded"
        elif task.status == TaskStatus.FAILED:
            message = f"task failed: {task.error}"
        else:
            message = "task cancelled"
        self.log_service.append(task.id, message, progress=task.progress)

    def _finalize_with_cleanup(
        self,
        task: TaskRecord,
        cleanup: Callable[[TaskRecord | None], None],
    ) -> None:
        finalized: TaskRecord | None = None
        try:
            finalized, changed = self.task_store.finalize_with_transition(task)
            if changed:
                self._log_final_state(finalized)
        finally:
            cleanup(finalized)

