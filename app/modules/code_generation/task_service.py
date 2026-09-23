from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path, PureWindowsPath
from shutil import copytree, rmtree
from typing import Any
from uuid import UUID

from app.core.errors import AppError, CancellationRequested
from app.modules.code_generation.client import NaturalCCClientError, NaturalCCCreateError
from app.modules.code_generation.schemas import CodeGenerationTaskRequest, NaturalCCRunRequest
from app.modules.code_generation.service import NaturalCCService
from app.modules.vulnerability.client import (
    PipelineClient,
    PipelineError,
    analyzer_coverage_status,
)
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.task_execution import ManagedTaskContext, ManagedTaskResult
from app.platform.services.task_service import TaskService

logger = logging.getLogger(__name__)

_EVENT_PROGRESS = {
    "run.started": 10,
    "model.requested": 20,
    "model.responded": 35,
    "tool.started": 55,
    "tool.finished": 70,
    "verification.finished": 85,
    "run.completed": 95,
}
_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "budget_exhausted",
    "cancelled",
    "unsupported",
}
_CANCEL_TIMEOUT_SECONDS = 2.0
_CANCEL_ATTEMPTS = 3
_CLEANUP_RETRY_ATTEMPTS = 3
_CLEANUP_RETRY_SECONDS = 0.1
_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".py", ".js", ".ts", ".java", ".go", ".php",
}
_OPERATION_GOALS = {
    "completion": (
        "Complete the requested code change. Only edit the selected target files. "
        "Do not run commands, tests, or builds."
    ),
    "repair": (
        "Repair the reported code issue. Only edit the selected target files. "
        "Do not run commands, tests, or builds."
    ),
    "refactor": (
        "Refactor the requested code while preserving behavior. "
        "Only edit the selected target files. Do not run commands, tests, or builds."
    ),
}
_TASK_TYPES = {
    TaskType.CODE_GENERATION,
    TaskType.CODE_REPAIR,
    TaskType.CODE_REFACTOR,
}


class CodeGenerationTaskService:
    def __init__(
        self,
        task_service: TaskService,
        naturalcc_service: NaturalCCService,
        approve_execute: bool,
        pipeline: PipelineClient | None = None,
    ) -> None:
        self.task_service = task_service
        self.naturalcc_service = naturalcc_service
        self.approve_execute = approve_execute
        self.pipeline = pipeline
        self._self_scans: dict[UUID, tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = {}
        self._self_scans_lock = threading.Lock()
        self._runs: dict[UUID, str] = {}
        self._runs_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cleanup_retries: dict[UUID, asyncio.Task[None]] = {}
        self._cleanup_retries_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = "NEW"

    async def create_code_generation_task(
        self,
        request: CodeGenerationTaskRequest,
    ) -> TaskRecord:
        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            return await self._execute(context, request)

        return await self.task_service.create_managed_task(
            module=BackendModuleName.CODE_GENERATION,
            project_id=request.project_id,
            task_type={
                "completion": TaskType.CODE_GENERATION,
                "repair": TaskType.CODE_REPAIR,
                "refactor": TaskType.CODE_REFACTOR,
            }[request.operation.value],
            command=["naturalcc", request.operation.value],
            execute=execute,
            timeout_seconds=request.timeout_seconds,
            cancellation_handler=self.cancel_task,
            preserve_workspace_on_success=True,
            workspace_completion_metadata_on_success={"cleanup_pending": False},
            artifacts_on_success=True,
        )

    async def startup(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "RUNNING":
                return
            if self._lifecycle_state == "CLOSED":
                raise AppError("code generation task service is closed")
            self._loop = asyncio.get_running_loop()
            self._lifecycle_state = "RUNNING"

        pending_tasks = [
            task
            for task in self.task_service.list_tasks()
            if task.task_type in _TASK_TYPES and self._cleanup_pending(task)
        ]
        if not pending_tasks:
            return
        results = await asyncio.gather(
            *(self._recover_cleanup(task) for task in pending_tasks),
            return_exceptions=True,
        )
        for task, result in zip(pending_tasks, results, strict=True):
            if result is not True and self._run_id(task.id) is not None:
                self._schedule_cleanup_retry(task.id)
            if isinstance(result, BaseException):
                logger.warning(
                    "NaturalCC cleanup recovery failed for task %s",
                    task.id,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "CLOSED":
                return
            self._lifecycle_state = "CLOSING"
        try:
            await self._stop_cleanup_retries()
        finally:
            with self._lifecycle_lock:
                self._loop = None
                self._lifecycle_state = "CLOSED"

    async def cancel_task(
        self,
        task_id: UUID,
        deadline: float | None = None,
    ) -> None:
        with self._self_scans_lock:
            active_scan = self._self_scans.get(task_id)
        if active_scan is not None:
            loop, scan_task = active_scan
            loop.call_soon_threadsafe(scan_task.cancel)
            return
        confirmed, _ = await self._cancel_run(task_id, deadline=deadline)
        if confirmed:
            self._finish_confirmed_workspace(task_id, cleanup=True)

    async def _execute(
        self,
        context: ManagedTaskContext,
        request: CodeGenerationTaskRequest,
    ) -> ManagedTaskResult:
        remote_run_id: str | None = None
        try:
            task_workspace = context.create_workspace("workspace")
            for target_file in request.target_files:
                target = context.resolve_path(task_workspace, target_file)
                if not target.is_file():
                    raise AppError(f"target file does not exist: {target_file}")

            context.report_progress(
                5,
                "NaturalCC task workspace created",
                stream="code_generation.adapter",
            )
            timeout_seconds = context.timeout_seconds
            budget = request.budget.model_dump(exclude_none=True)
            budget["max_seconds"] = min(
                budget.get("max_seconds", timeout_seconds),
                timeout_seconds,
            )
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with asyncio.timeout(timeout_seconds):
                context.hold_workspaces(metadata_updates={"cleanup_pending": True})
                created = await self.naturalcc_service.create_run(
                    workspace=task_workspace,
                    request=NaturalCCRunRequest(
                        goal=self._goal(request.operation.value, request.instruction),
                        target_files=request.target_files,
                        budget=budget,
                    ),
                )
                remote_run_id = created.get("run_id")
                if not isinstance(remote_run_id, str) or not remote_run_id:
                    raise NaturalCCCreateError(outcome_unknown=True)
                self._set_run(context.task_id, remote_run_id)
                context.merge_metadata({"naturalcc_run_id": remote_run_id})
                context.raise_if_cancelled()

                state = await self._run_and_poll(
                    context,
                    remote_run_id,
                    deadline=deadline,
                )
                if not self._state_is_terminal(state):
                    original_state = dict(state)
                    remote_terminal_confirmed, _ = await self._cancel_run(
                        context.task_id
                    )
                    state = original_state
                else:
                    self._mark_terminal_confirmed(context.task_id)
                    remote_terminal_confirmed = True
                result = self._result_from_state(
                    request.operation.value,
                    remote_run_id,
                    state,
                )
            if result.status == TaskStatus.SUCCEEDED:
                result.result["self_scan"] = await self._self_scan(
                    context, request, result.result["changed_files"], task_workspace, deadline
                )
            elif remote_terminal_confirmed:
                self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return result
        except CancellationRequested:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return ManagedTaskResult(status=TaskStatus.CANCELLED, error="cancelled")
        except asyncio.CancelledError:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            raise
        except NaturalCCCreateError as exc:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            elif not exc.outcome_unknown:
                self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return ManagedTaskResult(
                status=TaskStatus.FAILED,
                error="NaturalCC service request failed",
            )
        except TimeoutError:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return ManagedTaskResult(status=TaskStatus.FAILED, error="NaturalCC task timed out")
        except NaturalCCClientError:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return ManagedTaskResult(
                status=TaskStatus.FAILED,
                error="NaturalCC service request failed",
            )
        except AppError as exc:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return ManagedTaskResult(status=TaskStatus.FAILED, error=str(exc))
        except Exception:
            if remote_run_id is not None:
                confirmed, _ = await self._cancel_run(context.task_id)
                if confirmed:
                    self._finish_confirmed_workspace(context.task_id, cleanup=True)
            return ManagedTaskResult(status=TaskStatus.FAILED, error="NaturalCC task failed")
        finally:
            self._clear_run(context.task_id)
            try:
                latest = self.task_service.require_task(context.task_id)
            except Exception:
                logger.warning(
                    "failed to inspect NaturalCC cleanup state for task %s",
                    context.task_id,
                    exc_info=True,
                )
            else:
                if self._cleanup_pending(latest) and self._run_id(context.task_id) is not None:
                    self._schedule_cleanup_retry(context.task_id)

    async def _run_and_poll(
        self,
        context: ManagedTaskContext,
        run_id: str,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        after = 0
        progress = 5
        while True:
            context.raise_if_cancelled()
            run_task = asyncio.create_task(
                self.naturalcc_service.run(
                    run_id,
                    timeout_seconds=self._remaining_timeout_seconds(deadline),
                )
            )
            try:
                while not run_task.done():
                    context.raise_if_cancelled()
                    events = await self.naturalcc_service.events(
                        run_id,
                        after=after,
                        timeout_seconds=self._remaining_timeout_seconds(deadline),
                    )
                    context.raise_if_cancelled()
                    after, progress = self._report_events(context, events, after, progress)
                    await asyncio.sleep(
                        min(0.25, max(0, deadline - asyncio.get_running_loop().time()))
                    )
                state = await run_task
            finally:
                if not run_task.done():
                    run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            context.raise_if_cancelled()
            events = await self.naturalcc_service.events(
                run_id,
                after=after,
                timeout_seconds=self._remaining_timeout_seconds(deadline),
            )
            context.raise_if_cancelled()
            after, progress = self._report_events(context, events, after, progress)
            if state.get("status") != "waiting_approval":
                return state

            context.raise_if_cancelled()
            approval_state = await self.naturalcc_service.get_run(
                run_id,
                timeout_seconds=self._remaining_timeout_seconds(deadline),
            )
            context.raise_if_cancelled()
            risk, tool_call_id = self._pending_approval(approval_state)
            if risk == "write" or (risk == "execute" and self.approve_execute):
                context.raise_if_cancelled()
                await self.naturalcc_service.approve(
                    run_id,
                    risk,
                    tool_call_id,
                    timeout_seconds=self._remaining_timeout_seconds(deadline),
                )
                context.raise_if_cancelled()
                continue
            raise AppError(f"NaturalCC deferred approval for risk {risk} is not allowed")

    @staticmethod
    def _remaining_timeout_seconds(deadline: float) -> float:
        remaining_timeout_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_timeout_seconds <= 0:
            raise TimeoutError
        return remaining_timeout_seconds

    async def _self_scan(
        self,
        context: ManagedTaskContext,
        request: CodeGenerationTaskRequest,
        changed_files: list[str],
        workspace: Path,
        deadline: float,
    ) -> dict[str, Any]:
        try:
            target_files = self._scan_targets(
                context, workspace, request.target_files, changed_files
            )
        except AppError:
            return {"status": "failed", "error": "Agent changed_files is invalid or missing"}
        if not target_files:
            return {"status": "failed", "error": "No source files available for self scan"}
        if self.pipeline is None:
            return {"status": "unavailable", "error": "Pipeline scanner is not configured"}
        remaining = deadline - asyncio.get_running_loop().time() - 0.25
        if remaining <= 0:
            return {"status": "unavailable", "error": "No time remained for self scan"}
        analyzer = (
            "cppcheck"
            if any(Path(file).suffix.lower() in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}
                   for file in target_files)
            else "builtin"
        )
        loop = asyncio.get_running_loop()
        with self._self_scans_lock:
            self._self_scans[context.task_id] = (loop, asyncio.current_task())
        scan_copy = workspace.parent / "self-scan"
        copy_created = False
        cleanup_failed = False
        failure = None
        try:
            context.raise_if_cancelled()
            self._create_self_scan_copy(context, workspace, scan_copy)
            copy_created = True
            copytree(workspace, scan_copy, symlinks=True, dirs_exist_ok=True)
            scan_remaining = deadline - loop.time() - 0.25
            if scan_remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(scan_remaining):
                done = await self.pipeline.scan(
                    workspace=scan_copy,
                    target_files=target_files,
                    scope="targets",
                    analyzer=analyzer,
                    incremental=False,
                    severity_threshold="medium",
                    max_findings=30,
                    timeout_seconds=scan_remaining,
                    on_event=lambda kind: context.log(
                        f"Pipeline self scan event: {kind}", stream="code_generation.adapter"
                    ),
                )
            context.raise_if_cancelled()
        except PipelineError as exc:
            artifacts = exc.done.get("artifacts") if exc.done else None
            coverage = artifacts.get("coverage") if isinstance(artifacts, dict) else None
            failure = {
                "status": "failed",
                "error": str(exc),
                **({"coverage": coverage} if isinstance(coverage, list) else {}),
            }
        except TimeoutError:
            failure = {"status": "failed", "error": "Pipeline self scan timed out"}
        except CancellationRequested:
            raise
        except Exception:
            failure = {"status": "failed", "error": "Pipeline self scan failed"}
        finally:
            try:
                if copy_created:
                    self._remove_self_scan_copy(context, workspace, scan_copy)
            except (AppError, OSError):
                cleanup_failed = True
                logger.warning("self scan copy cleanup failed for task %s", context.task_id)
            with self._self_scans_lock:
                self._self_scans.pop(context.task_id, None)
        if cleanup_failed:
            return {"status": "failed", "error": "Pipeline self scan copy cleanup failed"}
        if failure is not None:
            return failure
        artifacts = done.get("artifacts")
        findings = artifacts.get("findings") if isinstance(artifacts, dict) else None
        coverage = artifacts.get("coverage") if isinstance(artifacts, dict) else None
        if not isinstance(findings, list) or not isinstance(coverage, list):
            return {"status": "failed", "error": "Pipeline self scan returned invalid artifacts"}
        if done.get("files_modified"):
            return {
                "status": "failed",
                "error": "Pipeline self scan modified files",
                "coverage": coverage,
            }
        coverage_status = analyzer_coverage_status(coverage, analyzer)
        if coverage_status is None:
            return {
                "status": "failed",
                "error": f"{analyzer} scan coverage unavailable",
                "coverage": coverage,
            }
        return {
            "status": coverage_status,
            "findings": findings,
            "coverage": coverage,
            "report": done.get("report", ""),
        }

    def _create_self_scan_copy(
        self, context: ManagedTaskContext, workspace: Path, scan_copy: Path
    ) -> None:
        workspace_service = self.task_service.workspace_service
        if workspace != workspace_service.resolve_task_workspace(
            context.project_id, context.task_id
        ) or scan_copy != workspace.parent / "self-scan":
            raise AppError("invalid self scan workspace")
        if workspace_service._is_link_or_reparse_point(scan_copy) or scan_copy.exists():
            raise AppError("self scan copy already exists")
        scan_copy.mkdir()

    def _remove_self_scan_copy(
        self, context: ManagedTaskContext, workspace: Path, scan_copy: Path
    ) -> None:
        if not scan_copy.exists() and not scan_copy.is_symlink():
            return
        workspace_service = self.task_service.workspace_service
        if (
            workspace != workspace_service.resolve_task_workspace(
                context.project_id, context.task_id
            )
            or scan_copy != workspace.parent / "self-scan"
            or scan_copy.resolve().parent != workspace.parent.resolve()
            or workspace_service._is_link_or_reparse_point(scan_copy)
        ):
            raise AppError("invalid self scan cleanup path")
        rmtree(scan_copy)

    def _scan_targets(
        self,
        context: ManagedTaskContext,
        workspace: Path,
        requested: list[str],
        changed: list[str],
    ) -> list[str]:
        targets: list[str] = []
        for value in [*requested, *changed]:
            path = Path(value)
            windows_path = PureWindowsPath(value)
            if (
                not value
                or "\\" in value
                or path.is_absolute()
                or windows_path.drive
                or any(part == ".." for part in path.parts)
            ):
                raise AppError("invalid self scan target")
            target = context.resolve_path(workspace, value)
            current = workspace
            for part in path.parts:
                current /= part
                if self.task_service.workspace_service._is_link_or_reparse_point(current):
                    raise AppError("invalid self scan target")
            if not target.is_file():
                raise AppError("missing self scan target")
            if path.suffix.lower() in _SOURCE_SUFFIXES and value not in targets:
                targets.append(value)
        return targets

    @staticmethod
    def _pending_approval(state: dict[str, Any]) -> tuple[str, str]:
        if state.get("status") != "waiting_approval":
            raise AppError("NaturalCC deferred approval changed before it could be approved")
        pending_approval = state.get("pending_approval")
        if not isinstance(pending_approval, dict):
            raise AppError("NaturalCC deferred approval is invalid: pending approval is missing")
        risk = pending_approval.get("risk")
        if not isinstance(risk, str) or not risk:
            raise AppError("NaturalCC deferred approval is invalid: risk is missing")
        tool_call = pending_approval.get("tool_call")
        if not isinstance(tool_call, dict):
            raise AppError("NaturalCC deferred approval is invalid: tool call is missing")
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise AppError("NaturalCC deferred approval is invalid: tool call id is missing")
        return risk, tool_call_id

    @staticmethod
    def _report_events(
        context: ManagedTaskContext,
        response: dict[str, Any],
        after: int,
        current_progress: int,
    ) -> tuple[int, int]:
        events = response.get("events")
        if not isinstance(events, list):
            return after, current_progress
        for event in events:
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, int):
                after = max(after, sequence)
            event_type = event.get("type")
            progress = _EVENT_PROGRESS.get(event_type) if isinstance(event_type, str) else None
            if progress is None:
                progress = event.get("progress")
                if not isinstance(progress, int):
                    payload = event.get("payload")
                    progress = payload.get("progress") if isinstance(payload, dict) else None
            message = (
                f"NaturalCC event: {event_type}"
                if isinstance(event_type, str) and event_type in _EVENT_PROGRESS
                else "NaturalCC event received"
            )
            if isinstance(progress, int) and 0 <= progress <= 95 and progress > current_progress:
                context.report_progress(
                    progress,
                    message,
                    stream="code_generation.adapter",
                )
                current_progress = progress
            else:
                context.log(
                    message,
                    stream="code_generation.adapter",
                    progress=current_progress,
                )
        return after, current_progress

    @staticmethod
    def _result_from_state(
        operation: str,
        run_id: str,
        state: dict[str, Any],
    ) -> ManagedTaskResult:
        status = state.get("status")
        if status == "cancelled":
            return ManagedTaskResult(status=TaskStatus.CANCELLED, error="cancelled")
        if status == "completed":
            return ManagedTaskResult(
                status=TaskStatus.SUCCEEDED,
                exit_code=0,
                result={
                    "operation": operation,
                    "naturalcc_run_id": run_id,
                    "final_answer": state.get("final_answer", ""),
                    "changed_files": CodeGenerationTaskService._changed_files(state),
                },
            )
        if status == "waiting_approval":
            return ManagedTaskResult(
                status=TaskStatus.FAILED,
                error="NaturalCC deferred approval was not resolved safely",
            )
        if status == "paused":
            return ManagedTaskResult(
                status=TaskStatus.FAILED,
                error=(
                    f"NaturalCC run remained {status}; cancelled for the approval safety policy"
                ),
            )
        if status in {"failed", "budget_exhausted", "unsupported"}:
            return ManagedTaskResult(
                status=TaskStatus.FAILED,
                error=f"NaturalCC run ended with status {status}",
            )
        return ManagedTaskResult(
            status=TaskStatus.FAILED,
            error="NaturalCC run returned an unsupported status",
        )

    @staticmethod
    def _changed_files(state: dict[str, Any]) -> list[str]:
        changed_files = state.get("changed_files")
        if not isinstance(changed_files, list):
            working_state = state.get("working_state")
            changed_files = (
                working_state.get("changed_files", [])
                if isinstance(working_state, dict)
                else []
            )
        return [item for item in changed_files if isinstance(item, str)]

    def _set_run(self, task_id: UUID, run_id: str) -> None:
        with self._runs_lock:
            self._runs[task_id] = run_id

    def _clear_run(self, task_id: UUID) -> None:
        with self._runs_lock:
            self._runs.pop(task_id, None)

    def _run_id(self, task_id: UUID) -> str | None:
        with self._runs_lock:
            run_id = self._runs.get(task_id)
        if run_id is not None:
            return run_id
        metadata_run_id = self.task_service.require_task(task_id).metadata.get(
            "naturalcc_run_id"
        )
        return metadata_run_id if isinstance(metadata_run_id, str) else None

    async def _cancel_run(
        self,
        task_id: UUID,
        *,
        deadline: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        run_id = self._run_id(task_id)
        if run_id is None:
            return False, {}
        loop = asyncio.get_running_loop()
        end = (
            min(deadline, loop.time() + _CANCEL_TIMEOUT_SECONDS)
            if deadline is not None
            else loop.time() + _CANCEL_TIMEOUT_SECONDS
        )
        last_state: dict[str, Any] = {}
        for attempt in range(_CANCEL_ATTEMPTS):
            remaining = end - loop.time()
            if remaining <= 0:
                break
            timeout_seconds = min(0.5, remaining)
            try:
                await self.naturalcc_service.cancel(
                    run_id,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                logger.warning("NaturalCC cancellation request failed for task %s", task_id)
            remaining = end - loop.time()
            if remaining > 0:
                try:
                    last_state = await self.naturalcc_service.get_run(
                        run_id,
                        timeout_seconds=min(0.5, remaining),
                    )
                    if self._state_is_terminal(last_state):
                        self._mark_terminal_confirmed(task_id)
                        return True, last_state
                except Exception:
                    logger.warning(
                        "NaturalCC cancellation confirmation failed for task %s",
                        task_id,
                    )
            if attempt < _CANCEL_ATTEMPTS - 1:
                await asyncio.sleep(
                    min(0.1 * (attempt + 1), max(0, end - loop.time()))
                )
        return False, last_state

    def _mark_terminal_confirmed(self, task_id: UUID) -> None:
        self.task_service.merge_task_metadata(
            task_id,
            {"naturalcc_terminal_confirmed": True},
        )

    def _finish_confirmed_workspace(self, task_id: UUID, *, cleanup: bool) -> bool:
        try:
            self.task_service.release_task_workspaces(
                task_id,
                cleanup=cleanup,
                completion_metadata={"cleanup_pending": False},
            )
            latest = self.task_service.require_task(task_id)
        except Exception:
            logger.warning(
                "failed to finish confirmed NaturalCC workspace for task %s",
                task_id,
                exc_info=True,
            )
            return False
        return not self._cleanup_pending(latest)

    async def _recover_cleanup(self, task: TaskRecord) -> bool:
        latest = self.task_service.require_task(task.id)
        if not self._cleanup_pending(latest):
            return True
        if latest.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return False
        if latest.metadata.get("naturalcc_terminal_confirmed") is not True:
            confirmed, _ = await self._cancel_run(task.id)
            if not confirmed:
                return False
        latest = self.task_service.require_task(task.id)
        return self._finish_confirmed_workspace(
            task.id,
            cleanup=latest.status != TaskStatus.SUCCEEDED,
        )

    def _schedule_cleanup_retry(self, task_id: UUID) -> None:
        with self._lifecycle_lock:
            loop = self._loop
            if self._lifecycle_state != "RUNNING" or loop is None or loop.is_closed():
                return
        try:
            loop.call_soon_threadsafe(self._start_cleanup_retry, task_id)
        except RuntimeError:
            return

    def _start_cleanup_retry(self, task_id: UUID) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state != "RUNNING":
                return
        with self._cleanup_retries_lock:
            existing = self._cleanup_retries.get(task_id)
            if existing is not None and not existing.done():
                return
            retry = asyncio.create_task(self._retry_cleanup(task_id))
            self._cleanup_retries[task_id] = retry
        retry.add_done_callback(
            lambda completed: self._forget_cleanup_retry(task_id, completed)
        )

    def _forget_cleanup_retry(
        self,
        task_id: UUID,
        retry: asyncio.Task[None],
    ) -> None:
        with self._cleanup_retries_lock:
            if self._cleanup_retries.get(task_id) is retry:
                self._cleanup_retries.pop(task_id, None)

    async def _retry_cleanup(self, task_id: UUID) -> None:
        for _ in range(_CLEANUP_RETRY_ATTEMPTS):
            await asyncio.sleep(_CLEANUP_RETRY_SECONDS)
            with self._lifecycle_lock:
                if self._lifecycle_state != "RUNNING":
                    return
            try:
                if await self._recover_cleanup(self.task_service.require_task(task_id)):
                    return
            except Exception:
                logger.warning(
                    "NaturalCC cleanup retry failed for task %s",
                    task_id,
                    exc_info=True,
                )

    async def _stop_cleanup_retries(self) -> None:
        with self._cleanup_retries_lock:
            retries = list(self._cleanup_retries.values())
        for retry in retries:
            retry.cancel()
        if retries:
            await asyncio.gather(*retries, return_exceptions=True)

    @staticmethod
    def _state_is_terminal(state: dict[str, Any]) -> bool:
        return state.get("status") in _TERMINAL_STATUSES

    @staticmethod
    def _cleanup_pending(task: TaskRecord) -> bool:
        return task.metadata.get("cleanup_pending") is True

    @staticmethod
    def _goal(operation: str, instruction: str) -> str:
        return f"{_OPERATION_GOALS[operation]}\n\n{instruction}"
