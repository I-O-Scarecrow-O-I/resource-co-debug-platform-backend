import asyncio
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.core.errors import AppError, CancellationRequested
from app.core.time import utc_now
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_execution import (
    ManagedTaskCancellationHandler,
    ManagedTaskContext,
    ManagedTaskExecutor,
    ManagedTaskResult,
    PreparedProcess,
    TaskPreparationContext,
    TaskPreparer,
)
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)

_MANAGED_WORKSPACE_CLEANUP_ATTEMPTS = 3
_MANAGED_WORKSPACE_CLEANUP_RETRY_SECONDS = 0.01


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
        default_timeout_seconds: int,
    ) -> None:
        self.workspace_service = workspace_service
        self.task_store = task_store
        self.log_service = log_service
        self.process_runner = process_runner
        self.default_timeout_seconds = default_timeout_seconds
        self._background_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="backend-task",
        )
        self._background_futures: dict[UUID, Future[None]] = {}
        self._background_lock = threading.Lock()
        self._process_controls: dict[UUID, _ProcessTaskControl] = {}
        self._process_controls_lock = threading.Lock()
        self._managed_cancellation_handlers: dict[
            UUID,
            ManagedTaskCancellationHandler,
        ] = {}
        self._managed_cancellation_handlers_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = "RUNNING"

    async def create_process_task(
        self,
        *,
        module: BackendModuleName,
        project_id: UUID,
        task_type: TaskType,
        command: list[str],
        work_dir: str = ".",
        timeout_seconds: int | None = None,
        metadata: dict | None = None,
        artifacts_on_success: bool = False,
    ) -> TaskRecord:
        self._ensure_accepting_tasks()
        self.workspace_service.require_project(project_id)
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=command,
            metadata=metadata or {},
        )
        self._start_background(
            task.id,
            lambda: self._run_process_task(
                task_id=task.id,
                project_id=project_id,
                command=command,
                work_dir=work_dir,
                timeout_seconds=timeout_seconds or self.default_timeout_seconds,
                workspace_name="workspace",
                artifacts_on_success=artifacts_on_success,
            ),
        )
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
        source_task_id: UUID | None = None,
        initial_command: list[str] | None = None,
        artifacts_on_success: bool = False,
    ) -> TaskRecord:
        """Create a task whose module prepares a command inside its task workspace."""
        self._ensure_accepting_tasks()
        source_workspace = self._resolve_process_source_workspace(project_id, source_task_id)
        if not self._is_async_preparer(prepare):
            raise AppError("prepare must be async")
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=list(initial_command or []),
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
                source_workspace=source_workspace,
                workspace_name="workspace",
                prepare=prepare,
                artifacts_on_success=artifacts_on_success,
            ),
        )
        return task

    async def create_managed_task(
        self,
        *,
        module: BackendModuleName,
        project_id: UUID,
        task_type: TaskType,
        command: list[str],
        execute: ManagedTaskExecutor,
        metadata: dict | None = None,
        timeout_seconds: int | None = None,
        total_timeout_seconds: int | float | None = None,
        cancellation_handler: ManagedTaskCancellationHandler | None = None,
        preserve_workspace_on_success: bool = False,
        workspace_completion_metadata_on_success: dict[str, object] | None = None,
        artifacts_on_success: bool = False,
    ) -> TaskRecord:
        """Run module orchestration with an operation timeout and optional total deadline.

        ``timeout_seconds`` is exposed to the module for individual operations.
        ``total_timeout_seconds`` bounds the complete executor only when provided.
        """
        self._ensure_accepting_tasks()
        self.workspace_service.require_project(project_id)
        if not self._is_async_executor(execute):
            raise AppError("execute must be async")
        if cancellation_handler is not None and not self._is_async_cancellation_handler(
            cancellation_handler
        ):
            raise AppError("cancellation_handler must be async")
        resolved_timeout = timeout_seconds or self.default_timeout_seconds
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=command,
            metadata=metadata or {},
        )
        if cancellation_handler is not None:
            with self._managed_cancellation_handlers_lock:
                self._managed_cancellation_handlers[task.id] = cancellation_handler
        try:
            self._start_background(
                task.id,
                lambda: self._run_managed_task(
                    task_id=task.id,
                    project_id=project_id,
                    execute=execute,
                    timeout_seconds=resolved_timeout,
                    total_timeout_seconds=total_timeout_seconds,
                    preserve_workspace_on_success=preserve_workspace_on_success,
                    workspace_completion_metadata_on_success=(
                        workspace_completion_metadata_on_success
                    ),
                    artifacts_on_success=artifacts_on_success,
                ),
            )
        except Exception:
            self._unregister_managed_cancellation_handler(task.id)
            raise
        return task

    def list_tasks(self) -> list[TaskRecord]:
        return self.task_store.list()

    def require_task(self, task_id: UUID) -> TaskRecord:
        return self.task_store.require(task_id)

    def merge_task_metadata(
        self,
        task_id: UUID,
        updates: dict[str, object],
    ) -> TaskRecord:
        self.task_store.require(task_id)
        return self.task_store.merge_metadata(task_id, updates)

    def hold_task_workspaces(
        self,
        task_id: UUID,
        metadata_updates: dict[str, object] | None = None,
    ) -> None:
        self.task_store.hold_workspaces(task_id, metadata_updates)

    def release_task_workspaces(
        self,
        task_id: UUID,
        *,
        cleanup: bool = False,
        completion_metadata: dict[str, object] | None = None,
    ) -> None:
        task = self.task_store.require(task_id)
        if cleanup:
            self.task_store.queue_workspace_cleanup_and_release_hold(
                task_id,
                completion_metadata,
            )
            self._cleanup_managed_task_workspaces(task.project_id, task_id)
        else:
            self.task_store.release_workspaces(task_id, completion_metadata)

    def find_process_source_file(
        self,
        *,
        project_id: UUID,
        path: str,
        source_task_id: UUID | None = None,
    ) -> str | None:
        source_workspace = self._resolve_process_source_workspace(project_id, source_task_id)
        source_root = (
            source_workspace.resolve()
            if source_workspace is not None
            else self.workspace_service.require_project(project_id).source_path.resolve()
        )
        resolved = self.workspace_service.resolve_path_in_workspace(source_root, path)
        return str(resolved.relative_to(source_root)) if resolved.is_file() else None

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
            recovered = self.task_store.recover_interrupted_tasks()

        try:
            managed_cleanup_task_ids = self.task_store.list_workspace_cleanup_pending()
        except Exception:
            managed_cleanup_task_ids = []
            logger.warning("failed to list managed workspace cleanup queue", exc_info=True)
        if managed_cleanup_task_ids:
            cleanup_results = await asyncio.gather(
                *(
                    asyncio.to_thread(self._recover_managed_task_workspace, task_id)
                    for task_id in managed_cleanup_task_ids
                ),
                return_exceptions=True,
            )
            for task_id, cleanup_result in zip(
                managed_cleanup_task_ids,
                cleanup_results,
                strict=True,
            ):
                if isinstance(cleanup_result, BaseException):
                    logger.warning(
                        "failed to recover managed task workspace %s",
                        task_id,
                        exc_info=(
                            type(cleanup_result),
                            cleanup_result,
                            cleanup_result.__traceback__,
                        ),
                    )

        for task in recovered:
            try:
                self._log_final_state(task)
            except Exception:
                logger.warning("failed to log recovered task %s", task.id, exc_info=True)
            if self.workspace_service is None or self._task_workspaces_are_held(task.id):
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
            self._background_executor.shutdown(wait=False, cancel_futures=True)
            with self._lifecycle_lock:
                self._lifecycle_state = "CLOSED"

    def close_resources_when_idle(self) -> None:
        """Close persistent resources immediately or after timed-out workers finish."""
        with self._background_lock:
            futures = list(self._background_futures.values())
        if not futures:
            self.task_store.close()
            self.log_service.close()
            return

        def close_when_done(_: object) -> None:
            with self._background_lock:
                if self._background_futures:
                    return
            self.task_store.close()
            self.log_service.close()

        for future in futures:
            future.add_done_callback(close_when_done)

    def can_close_resources(self) -> bool:
        with self._lifecycle_lock, self._background_lock:
            return self._lifecycle_state == "CLOSED" and all(
                future.done() for future in self._background_futures.values()
            )

    async def cancel_task(
        self,
        task_id: UUID,
        *,
        cancel_deadline: float | None = None,
    ) -> TaskRecord:
        task, changed = self.task_store.request_cancel_with_transition(task_id)
        with self._managed_cancellation_handlers_lock:
            cancellation_handler = self._managed_cancellation_handlers.get(task_id)
        with self._process_controls_lock:
            process_control = self._process_controls.get(task_id)
        if task.cancel_requested and process_control is not None:
            process_control.request_cancel()
        with self._background_lock:
            future = self._background_futures.get(task_id)
        if task.status == TaskStatus.CANCELLED and future is not None:
            future.cancel()
        if task.cancel_requested:
            if cancellation_handler is not None:
                try:
                    await cancellation_handler(task_id, cancel_deadline)
                except Exception:
                    logger.warning(
                        "managed task cancellation handler failed: %s",
                        task_id,
                        exc_info=True,
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "managed task cancellation handler was cancelled: %s",
                        task_id,
                    )
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
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError("artifacts are only available for succeeded tasks")
        if not self.task_store.artifacts_are_available(task_id):
            raise AppError("artifacts are not available for this task")
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
        self._unregister_managed_cancellation_handler(task_id)
        if not future.cancelled() and (exception := future.exception()) is not None:
            logger.error(
                "background task failed: %s",
                task_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    def _unregister_managed_cancellation_handler(self, task_id: UUID) -> None:
        with self._managed_cancellation_handlers_lock:
            self._managed_cancellation_handlers.pop(task_id, None)

    async def _run_managed_task(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        execute: ManagedTaskExecutor,
        timeout_seconds: int,
        total_timeout_seconds: int | float | None,
        preserve_workspace_on_success: bool,
        workspace_completion_metadata_on_success: dict[str, object] | None,
        artifacts_on_success: bool,
    ) -> None:
        task = self.task_store.try_start(task_id)
        if task is None:
            return

        def report_progress(percent: int, message: str, stream: str) -> None:
            nonlocal task
            task = self._report_progress(task, percent, message, stream=stream)

        def create_workspace(workspace_name: str | None) -> Path:
            self._raise_if_cancel_requested(task_id)
            return self.workspace_service.create_task_workspace(
                project_id,
                task_id,
                workspace_name=workspace_name,
            )

        context = ManagedTaskContext(
            task_id=task_id,
            project_id=project_id,
            timeout_seconds=timeout_seconds,
            _append_log=lambda message, stream, progress: self.log_service.append(
                task_id,
                message,
                stream=stream,
                progress=progress,
            ),
            _report_progress=report_progress,
            _is_cancelled=lambda: self.task_store.require(task_id).cancel_requested,
            _create_workspace=create_workspace,
            _resolve_path=self.workspace_service.resolve_path_in_workspace,
            _merge_metadata=lambda updates: self.merge_task_metadata(task_id, updates),
            _hold_workspaces=lambda metadata_updates: self.hold_task_workspaces(
                task_id,
                metadata_updates,
            ),
            _release_workspaces=lambda cleanup, completion_metadata: (
                self.release_task_workspaces(
                    task_id,
                    cleanup=cleanup,
                    completion_metadata=completion_metadata,
                )
            ),
        )
        try:
            self._raise_if_cancel_requested(task_id)
            if total_timeout_seconds is None:
                managed_result = await execute(context)
            else:
                async with asyncio.timeout(total_timeout_seconds):
                    managed_result = await execute(context)
            self._raise_if_cancel_requested(task_id)
            self._validate_managed_task_result(managed_result)
            task.status = managed_result.status
            task.result = dict(managed_result.result)
            task.exit_code = managed_result.exit_code
            task.elapsed_ms = managed_result.elapsed_ms
            task.error = managed_result.error
            task.finished_at = utc_now()
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
                task.progress = 100
            else:
                task.error = task.error or "cancelled"
        except (CancellationRequested, asyncio.CancelledError):
            self._mark_cancelled(task)
        except TimeoutError as exc:
            error = (
                str(exc)
                if total_timeout_seconds is None
                else f"managed task timed out after {total_timeout_seconds} seconds"
            )
            self._mark_failed(task, error)
            task.progress = 100
        except Exception as exc:
            self._mark_failed(task, str(exc))
            task.progress = 100
        finally:
            self._finalize_with_cleanup(
                task,
                lambda finalized: self._finish_managed_task_workspaces(
                    finalized=finalized,
                    project_id=project_id,
                    task_id=task_id,
                    preserve_workspace_on_success=preserve_workspace_on_success,
                    workspace_completion_metadata_on_success=(
                        workspace_completion_metadata_on_success
                    ),
                ),
                artifacts_on_success=artifacts_on_success,
                queue_workspace_cleanup_on_finalize=True,
                preserve_workspace_on_success=preserve_workspace_on_success,
            )

    @staticmethod
    def _validate_managed_task_result(result: object) -> None:
        if not isinstance(result, ManagedTaskResult):
            raise AppError("execute must return ManagedTaskResult")
        if not isinstance(result.status, TaskStatus) or result.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            raise AppError("managed task result must have a terminal status")
        if not isinstance(result.result, dict):
            raise AppError("managed task result must be a dict")
        try:
            json.dumps(
                result.result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise AppError("managed task result must be JSON serializable") from exc
        if result.exit_code is not None and type(result.exit_code) is not int:
            raise AppError("managed task exit_code must be an int or None")
        if result.elapsed_ms is not None and (
            type(result.elapsed_ms) is not int or result.elapsed_ms < 0
        ):
            raise AppError("managed task elapsed_ms must be a non-negative int or None")
        if result.error is not None and not isinstance(result.error, str):
            raise AppError("managed task error must be a string or None")

    def _finish_managed_task_workspaces(
        self,
        *,
        finalized: TaskRecord | None,
        project_id: UUID,
        task_id: UUID,
        preserve_workspace_on_success: bool,
        workspace_completion_metadata_on_success: dict[str, object] | None,
    ) -> None:
        if finalized is None:
            return
        if (
            finalized.status == TaskStatus.SUCCEEDED
            and preserve_workspace_on_success
        ):
            try:
                self.release_task_workspaces(
                    task_id,
                    completion_metadata=workspace_completion_metadata_on_success,
                )
            except Exception:
                logger.warning(
                    "failed to release managed workspace hold %s",
                    task_id,
                    exc_info=True,
                )
            return
        if self._task_workspaces_are_held(task_id):
            return
        try:
            cleanup_is_pending = (
                task_id in self.task_store.list_workspace_cleanup_pending()
            )
        except Exception:
            logger.warning(
                "failed to read managed workspace cleanup state %s; skipping cleanup",
                task_id,
                exc_info=True,
            )
            return
        if not cleanup_is_pending:
            logger.warning(
                "managed workspace cleanup intent is missing for task %s; skipping cleanup",
                task_id,
            )
            return
        self._cleanup_managed_task_workspaces(project_id, task_id)

    def _cleanup_managed_task_workspaces(self, project_id: UUID, task_id: UUID) -> None:
        if self._task_workspaces_are_held(task_id):
            return
        for attempt in range(_MANAGED_WORKSPACE_CLEANUP_ATTEMPTS):
            try:
                self.workspace_service.cleanup_task_workspaces(project_id, task_id)
            except Exception:
                if attempt + 1 < _MANAGED_WORKSPACE_CLEANUP_ATTEMPTS:
                    time.sleep(_MANAGED_WORKSPACE_CLEANUP_RETRY_SECONDS)
                    continue
                logger.warning(
                    "failed to clean managed task workspace %s",
                    task_id,
                    exc_info=True,
                )
                self._mark_managed_workspace_cleanup_pending(task_id)
                return
            try:
                self.task_store.complete_workspace_cleanup(task_id)
            except Exception:
                logger.warning(
                    "failed to complete managed workspace cleanup state %s",
                    task_id,
                    exc_info=True,
                )
            return

    def _mark_managed_workspace_cleanup_pending(self, task_id: UUID) -> None:
        try:
            self.task_store.mark_workspace_cleanup_pending(task_id)
        except Exception:
            logger.warning(
                "failed to persist managed workspace cleanup state %s",
                task_id,
                exc_info=True,
            )

    def _recover_managed_task_workspace(self, task_id: UUID) -> None:
        task = self.task_store.require(task_id)
        self._cleanup_managed_task_workspaces(task.project_id, task.id)

    def _task_workspaces_are_held(self, task_id: UUID) -> bool:
        try:
            return self.task_store.workspaces_are_held(task_id)
        except Exception:
            logger.warning(
                "failed to read managed workspace hold state %s; skipping cleanup",
                task_id,
                exc_info=True,
            )
            return True

    async def _run_process_task(
        self,
        task_id: UUID,
        project_id: UUID,
        command: list[str] | None,
        work_dir: str | None,
        timeout_seconds: int,
        source_workspace=None,
        workspace_name: str | None = None,
        prepare: TaskPreparer | None = None,
        artifacts_on_success: bool = False,
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
                        command=list(prepared.command),
                        work_dir=prepared.work_dir,
                        timeout_seconds=timeout_seconds,
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
                    process_control=process_control,
                )
            task.finished_at = utc_now()
            task.exit_code = result.exit_code
            task.elapsed_ms = result.elapsed_ms
            task.progress = 100
            if result.exit_code == 0:
                task.status = TaskStatus.SUCCEEDED
                task.result = {"success": True}
                if artifacts_on_success:
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
                    or not artifacts_on_success
                    or finalized.status != TaskStatus.SUCCEEDED
                    else None
                ),
                artifacts_on_success=artifacts_on_success,
            )

    async def _run_controlled_process(
        self,
        *,
        task_id: UUID,
        task_workspace: Path,
        command: list[str],
        work_dir: str,
        timeout_seconds: int,
        process_control: _ProcessTaskControl,
    ):
        cwd = self.workspace_service.resolve_work_dir_in_workspace(task_workspace, work_dir)
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
            _resolve_path=lambda path: self.workspace_service.resolve_path_in_workspace(
                workspace, path
            ),
        )
        prepared = await prepare(context)
        self._raise_if_cancel_requested(task.id)
        self._validate_prepared_process(prepared)
        recorded_command = (
            prepared.recorded_command
            if prepared.recorded_command is not None
            else prepared.command
        )
        updated = self.task_store.update_command_if_running(task.id, recorded_command)
        if updated is None:
            raise CancellationRequested()
        self._raise_if_cancel_requested(task.id)
        return updated, prepared

    @staticmethod
    def _validate_prepared_process(prepared: object) -> None:
        if not isinstance(prepared, PreparedProcess):
            raise AppError("prepare must return PreparedProcess")
        if not isinstance(prepared.command, list) or not prepared.command or not all(
            isinstance(item, str) and item for item in prepared.command
        ):
            raise AppError("prepared command must contain non-empty strings")
        if prepared.recorded_command is not None and (
            not isinstance(prepared.recorded_command, list)
            or not prepared.recorded_command
            or not all(
                isinstance(item, str) and item for item in prepared.recorded_command
            )
        ):
            raise AppError("recorded command must contain non-empty strings")
        if not isinstance(prepared.work_dir, str):
            raise AppError("prepared work_dir must be a string")

    @staticmethod
    def _is_async_preparer(prepare: TaskPreparer) -> bool:
        return inspect.iscoroutinefunction(prepare) or (
            callable(prepare) and inspect.iscoroutinefunction(prepare.__call__)
        )

    @staticmethod
    def _is_async_executor(execute: ManagedTaskExecutor) -> bool:
        return inspect.iscoroutinefunction(execute) or (
            callable(execute) and inspect.iscoroutinefunction(execute.__call__)
        )

    @staticmethod
    def _is_async_cancellation_handler(
        handler: ManagedTaskCancellationHandler,
    ) -> bool:
        return inspect.iscoroutinefunction(handler) or (
            callable(handler) and inspect.iscoroutinefunction(handler.__call__)
        )

    def _resolve_process_source_workspace(
        self,
        project_id: UUID,
        source_task_id: UUID | None,
    ) -> Path | None:
        self.workspace_service.require_project(project_id)
        if source_task_id is None:
            return None
        source_task = self.task_store.require(source_task_id)
        if source_task.project_id != project_id or source_task.status != TaskStatus.SUCCEEDED:
            raise AppError("source_task_id must reference a succeeded task in the same project")
        return self.workspace_service.resolve_task_workspace(project_id, source_task_id)

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

    def _raise_if_cancel_requested(self, task_id: UUID) -> None:
        if self.task_store.require(task_id).cancel_requested:
            raise CancellationRequested()

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
        *,
        artifacts_on_success: bool = False,
        queue_workspace_cleanup_on_finalize: bool = False,
        preserve_workspace_on_success: bool = False,
    ) -> None:
        finalized: TaskRecord | None = None
        try:
            finalized, changed = self.task_store.finalize_with_transition(
                task,
                artifacts_on_success=artifacts_on_success,
                queue_workspace_cleanup_on_finalize=(
                    queue_workspace_cleanup_on_finalize
                ),
                preserve_workspace_on_success=preserve_workspace_on_success,
            )
            if changed:
                self._log_final_state(finalized)
        finally:
            cleanup(finalized)

