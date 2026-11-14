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
    InteractiveOutputCallback,
    InteractiveProcessSession,
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

# 短重试只覆盖工作区删除，不能用来重新执行用户命令。
# 跨进程恢复依赖持久化的清理记录。
_MANAGED_WORKSPACE_CLEANUP_ATTEMPTS = 3
_MANAGED_WORKSPACE_CLEANUP_RETRY_SECONDS = 0.01


# 进程句柄尚未返回时，取消不能打断启动协程。
# 否则平台可能失去回收已由系统创建的进程所需的句柄。
class _ProcessTaskControl:
    """Coordinate cancellation with one task's subprocess launch boundary."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        worker_task: asyncio.Task[Any],
    ) -> None:
        # 线程锁保护启动窗口中状态的组合读写；它不覆盖整条进程命令的运行时间。
        # 取消调用只决定何时通知协程，不在 HTTP 线程直接操作进程句柄。
        self._lock = threading.Lock()
        # 保存 worker 所在事件循环，API 线程不得直接调用另一个循环上的 Task.cancel。
        self._loop = loop
        self._worker_task = worker_task
        # 取消标记、启动中标记和进程句柄必须在同一把锁下观察，单独读取任一项会遗漏启动窗口。
        self._cancel_requested = False
        self._launching = False
        self._process: object | None = None

    # 先在锁内记住取消请求。
    # 尚在启动且没有句柄时，由启动完成回调继续处理。
    def request_cancel(self) -> None:
        with self._lock:
            # 即使无法立即安全地取消，也先记下请求，让得到进程句柄的回调接着处理。
            self._cancel_requested = True
            should_cancel_worker = not self._launching or self._process is not None
        if should_cancel_worker:
            self._cancel_worker()

    # 将持久化取消检查与设置启动标记放在同一控制锁内。
    def begin_launch(self, is_cancelled: Callable[[], bool]) -> None:
        with self._lock:
            # 进入启动前若持久状态或控制对象已收到取消，直接结束，不创建新的子进程。
            if self._cancel_requested or is_cancelled():
                raise CancellationRequested()
            # 通过取消检查后进入可能尚无句柄的窗口。
            # 此后来的取消要等句柄可清理或启动失败。
            self._launching = True

    # 拿到可终止的进程后再补发启动窗口内积累的取消。
    def process_started(self, process: asyncio.subprocess.Process) -> None:
        with self._lock:
            # 句柄登记与是否已有取消请求在同一临界区完成，避免回调和 API 两边都错过通知。
            self._process = process
            should_cancel_worker = self._cancel_requested
        # 先释放控制锁再投递取消，工作循环可进入运行器自己的进程树收尾流程。
        if should_cancel_worker:
            self._cancel_worker()

    # 结束启动控制后不再保留句柄，实际管道和进程生命周期归运行器。
    def finish_launch(self) -> None:
        with self._lock:
            # 启动控制器只在本次运行过程中持有进程引用；真正释放进程由运行器完成。
            self._launching = False
            self._process = None

    # 取消可能由 API 线程或关闭流程发出，必须投递到 worker 所在循环。
    # 循环已经退出时无需再次调度回收。
    def _cancel_worker(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._worker_task.cancel)
        # 工作循环关闭后不再接受回调；这条路径不试图启动另一个循环接管旧任务。
        except RuntimeError:
            pass


# 通用任务编排负责持久化、独立执行上下文和终态。
# 模块只提供准备器、执行器或取消回调。
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
        # 后台工作从请求协程分离；线程池限制同时占用执行槽的任务数量。
        self._background_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="backend-task",
        )
        # Future 表表示已提交且尚未从后台目录移除的工作；它不等同于数据库里的 RUNNING 集合。
        # 排队项可能还未开始，已提交终态的任务也可能仍在释放资源。
        self._background_futures: dict[UUID, Future[None]] = {}
        # 后台表跨线程读写；每次关停观察先复制当前项，再在锁外等待。
        self._background_lock = threading.Lock()
        # 分别登记本地启动控制和模块取消钩子，避免把不同资源的取消语义混为一谈。
        self._process_controls: dict[UUID, _ProcessTaskControl] = {}
        self._process_controls_lock = threading.Lock()
        # 只有需要向模块自己持有的资源发送取消时才登记钩子，普通命令由进程控制器处理。
        self._managed_cancellation_handlers: dict[
            UUID,
            ManagedTaskCancellationHandler,
        ] = {}
        self._managed_cancellation_handlers_lock = threading.Lock()
        # 生命周期锁保护接受任务与执行器关闭的边界；任务记录本身仍由 SQLite 更新。
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = "RUNNING"

    # 命令已知时仍先建任务，再由后台创建副本并运行。
    # 提交响应表示接收，不表示进程已经启动。
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
        # 先检查是否接受任务，再验证工程存在。
        # 关闭与入队仍可能交错，提交后台前要复查服务状态。
        self._ensure_accepting_tasks()
        # 先核对项目 ID，避免为不存在的源码根留下待运行记录。
        self.workspace_service.require_project(project_id)
        # 记录先落库再安排 worker，轮询与后台执行通过相同任务 ID 汇合。
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=command,
            metadata=metadata or {},
        )
        # 把命令及工程身份交给任务工厂。
        # 真正复制源树和启动子进程发生在后台执行槽中。
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
        # 此处返回提交时记录，worker 可能已经认领；需要新状态时重新读取。
        return task

    # 先验证源任务归属及准备器契约，再创建持久化记录。
    # 准备器在工作区副本中计算实际命令，避免修改原工程。
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
        # 指定来源时提前解析成功副本，类型与目录错误在提交阶段可见。
        source_workspace = self._resolve_process_source_workspace(project_id, source_task_id)
        # 普通同步函数不能作为准备器；任务工作循环需要明确的异步等待边界。
        if not self._is_async_preparer(prepare):
            raise AppError("prepare must be async")
        # 命令尚未准备好时使用空列表或调用方提供的记录命令。
        # 准备完成后的发布会另行核验任务仍能运行。
        # 实际命令尚待准备，初始显示可为空或逻辑命令。
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=list(initial_command or []),
            metadata=metadata or {},
        )
        # 准备式任务与直接执行共享运行器；准备器只在自己的副本建立后调用。
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

    # 受管任务可以包含多个本地或外部步骤，平台只负责总生命周期。
    # 总时限可以独立于模块各步骤所用的默认超时。
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
        source_task_id: UUID | None = None,
        total_timeout_seconds: int | float | None = None,
        cancellation_handler: ManagedTaskCancellationHandler | None = None,
        preserve_workspace_on_success: bool = False,
        workspace_completion_metadata_on_success: dict[str, object] | None = None,
        artifacts_on_success: bool = False,
        workspace_target_files: list[str] | None = None,
    ) -> TaskRecord:
        """Run module orchestration with an operation timeout and optional total deadline.

        ``timeout_seconds`` is exposed to the module for individual operations.
        ``total_timeout_seconds`` bounds the complete executor only when provided.
        """
        # 异步入口与取消回调在建单前验证，避免持久化不可执行的请求。
        self._ensure_accepting_tasks()
        # 来源副本归属在创建任务时核对，准备和执行都使用同一个来源选择。
        source_workspace = self._resolve_process_source_workspace(project_id, source_task_id)
        # 把异步执行器和取消钩子分别验证，两者由不同调用路径触发。
        # 取消钩子可在执行器尚未启动或正在等待外部服务时被调用。
        if not self._is_async_executor(execute):
            raise AppError("execute must be async")
        # 异步取消钩子在登记前校验，避免取消到达后才发现无法等待模块清理。
        if cancellation_handler is not None and not self._is_async_cancellation_handler(
            cancellation_handler
        ):
            raise AppError("cancellation_handler must be async")
        # 有效操作超时传入模块上下文。
        # 是否限制编排总耗时仍取决于另一个独立参数。
        resolved_timeout = timeout_seconds or self.default_timeout_seconds
        # 只有来源和调用契约检查通过才创建持久任务，避免为不合约的执行器留下排队记录。
        task = self._new_task(
            module=module,
            project_id=project_id,
            task_type=task_type,
            command=command,
            metadata=metadata or {},
        )
        # 先登记取消钩子再启动后台工作，确保接收后立即取消也有通知路径。
        if cancellation_handler is not None:
            with self._managed_cancellation_handlers_lock:
                self._managed_cancellation_handlers[task.id] = cancellation_handler
        # 已登记的取消路径必须与入队一起收束；入队失败后不会有 Future 回调替它清理。
        try:
            self._start_background(
                task.id,
                lambda: self._run_managed_task(
                    task_id=task.id,
                    project_id=project_id,
                    execute=execute,
                    timeout_seconds=resolved_timeout,
                    source_workspace=source_workspace,
                    total_timeout_seconds=total_timeout_seconds,
                    preserve_workspace_on_success=preserve_workspace_on_success,
                    workspace_completion_metadata_on_success=(
                        workspace_completion_metadata_on_success
                    ),
                    artifacts_on_success=artifacts_on_success,
                    workspace_target_files=workspace_target_files,
                ),
            )
        # 若未能安排后台任务，撤销已登记的钩子，防止悬挂引用。
        except Exception:
            # 提交失败时撤销内存取消钩子，避免后续针对未启动的模块编排器发送通知。
            self._unregister_managed_cancellation_handler(task.id)
            raise
        return task

    # 任务列表由持久化存储刷新；后台活动表并不是历史记录来源。
    def list_tasks(self) -> list[TaskRecord]:
        return self.task_store.list()

    # 按 ID 读取的是存储快照，不能从它取得后台线程或进程控制句柄。
    def require_task(self, task_id: UUID) -> TaskRecord:
        return self.task_store.require(task_id)

    # 元数据可能由后台编排与外部控制接口并发更新，合并交给存储层。
    def merge_task_metadata(
        self,
        task_id: UUID,
        updates: dict[str, object],
    ) -> TaskRecord:
        # 元数据合并只更新已存在记录，不用于隐式创建任务。
        self.task_store.require(task_id)
        return self.task_store.merge_metadata(task_id, updates)

    # 占用标记持久化，避免进程退出或重启时误删仍有消费者的副本。
    def hold_task_workspaces(
        self,
        task_id: UUID,
        metadata_updates: dict[str, object] | None = None,
    ) -> None:
        # 本入口只建立持有标记和可选元数据；它不会在磁盘上另建副本。
        self.task_store.hold_workspaces(task_id, metadata_updates)

    # 需要清理时，先登记可恢复的意图再释放占用，文件系统操作随后执行。
    def release_task_workspaces(
        self,
        task_id: UUID,
        *,
        cleanup: bool = False,
        completion_metadata: dict[str, object] | None = None,
    ) -> None:
        # 先取得任务的工程归属，再把清理意图与该任务关联。
        # 调用方不需要向回收入口传递任意磁盘路径。
        task = self.task_store.require(task_id)
        # 删除副本与单纯释放外部占用分开。
        # 先释放并入队可让崩溃后的恢复知道有工作尚待执行。
        if cleanup:
            self.task_store.queue_workspace_cleanup_and_release_hold(
                task_id,
                completion_metadata,
            )
            # 释放并要求回收时先落库意图再删除目录，使删除失败可以在启动恢复中被发现。
            self._cleanup_managed_task_workspaces(task.project_id, task_id)
        # 仅释放占用时不进入目录清理队列，产物可由后继任务继续消费。
        else:
            self.task_store.release_workspaces(task_id, completion_metadata)

    # 可选择从成功的上游副本查找文件；返回相对于实际输入根的名称。
    def find_process_source_file(
        self,
        *,
        project_id: UUID,
        path: str,
        source_task_id: UUID | None = None,
    ) -> str | None:
        # 查询文件与真正执行不共用文件句柄；运行时必须在新建的私有副本再次定位。
        source_workspace = self._resolve_process_source_workspace(project_id, source_task_id)
        # 路径查找基准与实际复制来源一致。
        # 不在请求的路径对象中向模块泄露服务器根目录。
        source_root = (
            source_workspace.resolve()
            if source_workspace is not None
            else self.workspace_service.require_project(project_id).source_path.resolve()
        )
        resolved = self.workspace_service.resolve_path_in_workspace(source_root, path)
        # 存在但不是普通文件时不返回可供执行的相对名称；非法越界路径则沿用路径错误。
        return str(resolved.relative_to(source_root)) if resolved.is_file() else None

    # 平台提供有字节上限的源文本读接口，不将二进制或大文件读入 API 响应。
    def read_project_source_text(
        self, *, project_id: UUID, path: str, max_bytes: int = 64 * 1024
    ) -> str:
        # 这个入口用于小型文本配置，例如调试工作负载清单，返回内容而非宿主路径。
        """Read a small project source file without exposing a server filesystem path."""
        requested = Path(path)
        # 同时拒绝 Windows 风格的锚点表达，即使当前平台可能把反斜线识别为普通字符。
        if requested.is_absolute() or requested.anchor or "\\" in path or ":" in path:
            raise AppError("project source path must be relative")
        # 相对语法验证后还要由工作区解析实际位置，二者分别约束输入格式和目录归属。
        source_file = self.workspace_service.resolve_project_path(project_id, path)
        if not source_file.is_file():
            raise AppError(f"project source file does not exist: {path}")
        # 读取前限制文件大小，适合处理工作负载清单一类小文本。
        # 代码或归档下载应走单独的文件响应。
        if source_file.stat().st_size > max_bytes:
            raise AppError("project source file exceeds size limit")
        # 源文件必须是 UTF-8，解码失败作为可预期的输入错误处理。
        try:
            return source_file.read_text(encoding="utf-8")
        # 文本查询只接受可解码的源文件，不用替换字符掩盖损坏的 UTF-8。
        except UnicodeDecodeError as exc:
            raise AppError("project source file must be UTF-8") from exc

    # 列表和单文件下载都先验证成功终态和产物发布标记。
    def list_task_artifacts(self, task_id: UUID) -> list[tuple[str, int]]:
        task = self._require_succeeded_artifact_task(task_id)
        return self.workspace_service.list_task_artifacts(task.project_id, task.id)

    # 文件下载先拿持久化的工程 ID，不能由客户端另选工作区根。
    def resolve_task_artifact(self, task_id: UUID, artifact_path: str) -> Path:
        task = self._require_succeeded_artifact_task(task_id)
        return self.workspace_service.resolve_task_artifact(
            task.project_id,
            task.id,
            artifact_path,
        )

    # 启动时处理上次进程中断留下的记录，不能直接重跑原命令。
    async def startup(self) -> None:
        # 恢复与拒绝新任务的状态共享生命周期锁。
        # 离开锁后再做并行目录清理，避免持锁等待文件系统。
        with self._lifecycle_lock:
            if self._lifecycle_state != "RUNNING":
                raise AppError("task service is not accepting tasks")
            # 启动只恢复记录，旧进程或外部运行并不会由这一调用恢复成活跃会话。
            recovered = self.task_store.recover_interrupted_tasks()

        # 持久化清理队列独立于任务中断恢复，成功终态也可能还需要收尾。
        try:
            # 清理队列读取失败不阻止服务完成中断任务的状态恢复；保留日志供后续处理。
            managed_cleanup_task_ids = self.task_store.list_workspace_cleanup_pending()
        # 清理队列不能读取时记录诊断并继续处理已恢复的任务，不以空列表证明没有待清理目录。
        except Exception:
            managed_cleanup_task_ids = []
            logger.warning("failed to list managed workspace cleanup queue", exc_info=True)
        # 清理交给线程；单条记录失败不会取消其他恢复工作。
        if managed_cleanup_task_ids:
            # 每项清理的异常随结果返回，单个目录失败不会取消其他项目的恢复。
            cleanup_results = await asyncio.gather(
                *(
                    asyncio.to_thread(self._recover_managed_task_workspace, task_id)
                    for task_id in managed_cleanup_task_ids
                ),
                return_exceptions=True,
            )
            # 逐项关联恢复结果与任务 ID，单个异常要能从日志追踪。
            for task_id, cleanup_result in zip(
                managed_cleanup_task_ids,
                cleanup_results,
                strict=True,
            ):
                # 按原 ID 关联清理异常，保持并行恢复后的日志可定位。
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

        # 为已转终态的记录补写日志。
        # 持久化占用优先于中断恢复中的目录回收。
        for task in recovered:
            try:
                # 中断恢复日志与文件回收独立尝试，一个失败不会跳过另一项。
                self._log_final_state(task)
            # 终态日志失败不改变已经落库的状态，也不应阻止后续文件回收。
            except Exception:
                logger.warning("failed to log recovered task %s", task.id, exc_info=True)
            # 旧任务已结束不代表目录无人持有；远端代码生成仍可能使用它。
            if self.workspace_service is None or self._task_workspaces_are_held(task.id):
                continue
            try:
                # 无持久化占用的中断任务走平台工作区回收；被远端占用的目录由所属模块确认后释放。
                self.workspace_service.cleanup_task_workspaces(task.project_id, task.id)
            except Exception:
                logger.warning(
                    "failed to clean recovered task workspace %s",
                    task.id,
                    exc_info=True,
                )

    # 先停止接收新任务，然后用统一截止时间请求取消既有任务。
    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "CLOSED":
                return
            # 先将生命周期置为关闭中，工作线程回收期间 API 不再接收新的任务。
            self._lifecycle_state = "CLOSING"

        # 关闭预算以事件循环的单调时间为准。
        # 同一截止时刻传给所有模块取消钩子。
        deadline = asyncio.get_running_loop().time() + max(grace_seconds, 0)
        try:
            # 开始停止后台池之前，逐项通过正常取消路径通知进程控制器和模块钩子。
            for task in self.task_store.list():
                # 只向仍排队或运行的任务送出关闭取消；历史终态不在这次遍历中重写。
                if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                    try:
                        await self.cancel_task(task.id, cancel_deadline=deadline)
                    # 某个取消通知失败只记录诊断，关停流程仍尝试处理后续任务。
                    except Exception:
                        logger.warning(
                            "failed to cancel task during shutdown: %s",
                            task.id,
                            exc_info=True,
                        )

            # 线程池 Future 的取消只覆盖尚未启动的工作；运行中的任务需走各自取消通道。
            with self._background_lock:
                futures = list(self._background_futures.values())
            # 直接取消尚未领取的线程池任务；运行线程不会被 Future 强制打断。
            for future in futures:
                # Future.cancel 只能阻止尚未执行的工作项，已经运行的资源仍靠各任务的取消与清理路径。
                future.cancel()

            # 等待活动任务退出，但服务关闭不会无限等待外部运行。
            while True:
                with self._background_lock:
                    active_futures = [
                        future for future in self._background_futures.values() if not future.done()
                    ]
                # 关闭期限到达时停止等待；即使仍有线程收尾，也必须关闭接收入口。
                # 连接留给延迟关闭路径处理，避免在此强行切断写入。
                if not active_futures or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.05)
        # 关闭执行入口与关闭存储分开，仍在收尾的任务可能需要写入终态。
        finally:
            # 关停只等待到共同截止，不在最终步骤阻塞等待所有线程。
            self._background_executor.shutdown(wait=False, cancel_futures=True)
            with self._lifecycle_lock:
                self._lifecycle_state = "CLOSED"

    # 最后一个未退出的后台任务完成后再关闭共享存储和日志。
    # 延迟资源关闭与任务服务关闭不同步。
    # 线程仍在写日志时，缓存引用即便清掉也要让旧连接继续存在。
    def close_resources_when_idle(self) -> None:
        """Close persistent resources immediately or after timed-out workers finish."""
        # 锁内取得登记快照即可；完成回调不能在同一非重入锁内执行关闭逻辑。
        with self._background_lock:
            futures = list(self._background_futures.values())
        # 此时没有登记中的工作才直接关闭连接；否则注册回调接续实际释放。
        if not futures:
            self.task_store.close()
            self.log_service.close()
            return

        # 每个未结束 Future 都可触发回调。
        # 共享连接只有在当前后台表没有活跃工作时才真正关闭。
        def close_when_done(_: object) -> None:
            # 多个回调可能先后进入，只有观察到后台表清空才关闭共享资源。
            with self._background_lock:
                if self._background_futures:
                    return
            self.task_store.close()
            self.log_service.close()

        # 回调附加到这些 Future，包括可能刚完成的项。
        # 真正关闭前再次看当前目录，防止第一个完成的任务提前关闭共享资源。
        for future in futures:
            future.add_done_callback(close_when_done)

    # 缓存清理必须同时确认任务服务已关闭且不再有运行 Future。
    # 仅看到没有正在运行的任务不足以阻止此后继续接收新任务。
    def can_close_resources(self) -> bool:
        with self._lifecycle_lock, self._background_lock:
            # 入口关闭和后台执行结束是两个条件；仅凭生命周期为 CLOSED 不能提前关闭存储。
            return self._lifecycle_state == "CLOSED" and all(
                future.done() for future in self._background_futures.values()
            )

    # 先持久化取消请求，再通知实际执行者；通知失败不能撤销已接收的取消。
    async def cancel_task(
        self,
        task_id: UUID,
        *,
        cancel_deadline: float | None = None,
    ) -> TaskRecord:
        # 同一操作同时得到最新状态和是否发生变更。
        # 取消通知可以重发，日志只在首次迁移时追加。
        task, changed = self.task_store.request_cancel_with_transition(task_id)
        # 回调和进程控制分别在锁内取快照。
        # 发通知时不持锁，以免模块回调重入造成死锁。
        with self._managed_cancellation_handlers_lock:
            # 取消处理先读取已落库状态再查内存处理者，重启后没有处理者也不丢取消记录。
            cancellation_handler = self._managed_cancellation_handlers.get(task_id)
        with self._process_controls_lock:
            process_control = self._process_controls.get(task_id)
        # 有活动进程控制器时将取消交给它处理启动窗口。
        if task.cancel_requested and process_control is not None:
            # 交由进程控制器决定是否能立即取消协程，避免在子进程刚创建但尚未登记时失去所有权。
            process_control.request_cancel()
        # 排队 Future 与运行中进程的控制句柄不同；只对已转为取消终态的记录尝试直接取消 Future。
        with self._background_lock:
            future = self._background_futures.get(task_id)
        # 已经转为取消终态的排队任务可直接尝试取消其 Future。
        if task.status == TaskStatus.CANCELLED and future is not None:
            # 任务若在队列中直接转为取消终态，尝试从后台池撤下其待执行工作。
            future.cancel()
        # 受管任务还可能持有远端运行等资源，单靠停止本地 worker 不足以清理。
        if task.cancel_requested:
            # 模块的远端清理是尽力执行；异常只留下诊断，不撤销存储中的取消标记。
            if cancellation_handler is not None:
                try:
                    # 传给模块的是共享绝对截止时间；具体远端请求、重试和释放条件由模块负责。
                    await cancellation_handler(task_id, cancel_deadline)
                # 外部取消通知失败不回滚已持久化的取消事实，仍可由实际执行者收束。
                except Exception:
                    logger.warning(
                        "managed task cancellation handler failed: %s",
                        task_id,
                        exc_info=True,
                    )
                # 取消钩子自身取消时留下诊断；先前持久化的取消请求仍有效。
                except asyncio.CancelledError:
                    logger.warning(
                        "managed task cancellation handler was cancelled: %s",
                        task_id,
                    )
        # 只有首次状态变化记录取消消息，重复请求不会重复添加终态日志。
        if changed:
            try:
                # 排队取消立即有终态，因此可以直接写终态日志；运行中只记录已请求。
                if task.status == TaskStatus.CANCELLED:
                    self._log_final_state(task)
                else:
                    # 运行中请求只写请求日志；最终取消日志由成功提交终态的路径负责。
                    self.log_service.append(task_id, "process cancellation requested")
            except Exception:
                logger.warning("failed to log task cancellation: %s", task_id, exc_info=True)
        return task

    # 创建任务时持久化初始状态，随后才允许后台以该 ID 认领。
    def _new_task(
        self,
        module: BackendModuleName,
        project_id: UUID,
        task_type: TaskType,
        command: list[str],
        metadata: dict,
    ) -> TaskRecord:
        # 新任务一次确定 ID、归属、命令和创建时间，认领前没有运行起始时间。
        # 任务主键在提交时确定，所有工作区、日志和运行记录都以它关联。
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
        # 在交给后台工作者前先提交存储，使其他请求能查询任务并提交取消。
        self.task_store.save(task)
        return task

    # 成功状态本身不意味着公开副本可下载，还需检查显式发布标记。
    def _require_succeeded_artifact_task(self, task_id: UUID) -> TaskRecord:
        # 列表和单文件读取都通过同一资格检查，未发布副本不会因知道目录名称而可下载。
        task = self.require_task(task_id)
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError("artifacts are only available for succeeded tasks")
        # 已成功仍必须有独立的发布记录；生成了文件但未发布的任务不通过下载检查。
        if not self.task_store.artifacts_are_available(task_id):
            raise AppError("artifacts are not available for this task")
        return task

    # 在生命周期锁内复查接收状态，封住建单到入队之间的关闭竞态。
    def _start_background(
        self,
        task_id: UUID,
        task_factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        # 每个线程任务运行独立事件循环，跨线程控制必须通过线程安全投递。
        def run_factory() -> None:
            # 每个后台工作项创建自己的事件循环，不能将请求线程的协程状态直接交给运行器。
            asyncio.run(task_factory())

        # 检查和提交由同一生命周期锁保护。
        # 如果建单后刚好进入关闭，显式标记取消以免遗留排队任务。
        with self._lifecycle_lock:
            try:
                self._ensure_accepting_tasks()
            # 建单后关闭时只撤销本次新任务；不重新打开已经进入关闭的线程池。
            except AppError:
                self.task_store.request_cancel(task_id)
                raise
            # 工作入口在工作线程创建并运行事件循环，避免把 API 的协程运行对象跨线程复用。
            future = self._background_executor.submit(run_factory)
            # 注册 Future 后再安装完成回调。
            # 即使工作已经结束，回调也能清掉已登记的索引。
            with self._background_lock:
                self._background_futures[task_id] = future
        # 登记后再绑定完成回调；即使工作项已经结束，回调也会读取实际完成结果。
        future.add_done_callback(lambda completed: self._forget_background(task_id, completed))

    def _ensure_accepting_tasks(self) -> None:
        # 入口检查也被后台提交阶段重复使用，以覆盖创建记录与关停之间的竞态。
        with self._lifecycle_lock:
            if self._lifecycle_state != "RUNNING":
                raise AppError("task service is shutting down")

    # 完成回调移除与该 Future 对应的索引，同时释放模块取消钩子。
    def _forget_background(self, task_id: UUID, future: Future[None]) -> None:
        # 只有当前 Future 仍是此任务的登记项才移除；不能按 ID 无条件擦掉新的持有者。
        with self._background_lock:
            # 只撤销当前对应的 Future，避免误删后来登记的引用。
            # 已经取消的 Future 不读取异常值。
            if self._background_futures.get(task_id) is future:
                self._background_futures.pop(task_id, None)
        # 后台结束时清掉取消钩子，后续重复取消不应再调用已收尾的模块。
        self._unregister_managed_cancellation_handler(task_id)
        # 异常在 Future 完成回调中读取，便于定位未能正常进入任务终态的工作。
        if not future.cancelled() and (exception := future.exception()) is not None:
            logger.error(
                "background task failed: %s",
                task_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    # 钩子移除是幂等的，正常完成与入队错误路径可共用。
    def _unregister_managed_cancellation_handler(self, task_id: UUID) -> None:
        with self._managed_cancellation_handlers_lock:
            self._managed_cancellation_handlers.pop(task_id, None)

    # 受管执行先认领任务，再为该次运行绑定工作区和交互进程资源。
    async def _run_managed_task(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        execute: ManagedTaskExecutor,
        timeout_seconds: int,
        source_workspace: Path | None,
        total_timeout_seconds: int | float | None,
        preserve_workspace_on_success: bool,
        workspace_completion_metadata_on_success: dict[str, object] | None,
        artifacts_on_success: bool,
        workspace_target_files: list[str] | None,
    ) -> None:
        # 未认领到说明状态已变化，例如排队期间已取消。
        task = self.task_store.try_start(task_id)
        # 未认领意味着任务已被取消或不再排队，不能继续创建工作区。
        if task is None:
            return
        # 记录副本、会话和监视器的创建历史。
        # 初始化任何一部分失败，最终收尾都能按已取得的资源处理。
        task_workspaces: set[Path] = set()
        # 交互会话与取消监视分别跟踪；终态前既要处理进程，也要等待监视退出。
        interactive_sessions: list[InteractiveProcessSession] = []
        interactive_monitors: list[asyncio.Task[None]] = []

        # 进度更新会回读存储快照，后续终态计算使用较新的记录。
        def report_progress(percent: int, message: str, stream: str) -> None:
            nonlocal task
            # 每次报告后使用最新任务记录，避免后面的逻辑依赖提交时进度。
            task = self._report_progress(task, percent, message, stream=stream)

        # 记录由本任务创建的副本，交互启动时会校验该集合。
        def create_workspace(workspace_name: str | None) -> Path:
            # 复制前再检查取消。
            # 模块可能请求多轮工作区，不能让已取消任务继续复制下一轮。
            self._raise_if_cancel_requested(task_id)
            # 平台按项目归属选择项目源或已解析的来源副本，模块只选私有副本名称。
            selection = (
                {"target_files": workspace_target_files} if workspace_target_files is not None else {}
            )
            workspace = self.workspace_service.create_task_workspace(
                project_id,
                task_id,
                source_path=source_workspace,
                workspace_name=workspace_name,
                **selection,
            )
            # 记录解析后的目录身份，后续只能把这些目录作为交互进程的工作区根。
            task_workspaces.add(workspace.resolve())
            # 只在成功创建并登记后把副本交给执行器；创建失败没有可打开的工作区。
            return workspace

        # 模块可请求交互进程，但命令形状、目录归属及资源收尾仍由平台把关。
        async def open_interactive_process(
            command: list[str],
            workspace: Path,
            work_dir: str,
            on_output: InteractiveOutputCallback | None,
        ) -> InteractiveProcessSession:
            # 入口检查命令参数和回调形状。
            # 模块不能用非字符串参数绕开平台统一的进程调用契约。
            if not isinstance(command, list) or not command or not all(
                isinstance(item, str) and item for item in command
            ):
                raise AppError("interactive command must contain non-empty strings")
            # 目录与输出回调的形状分别校验，工作目录随后还需通过磁盘归属检查。
            if not isinstance(work_dir, str):
                raise AppError("interactive work_dir must be a string")
            # 无输出回调时仍由平台保存日志；提供回调则要求可在该任务的输出处理路径调用。
            if on_output is not None and not callable(on_output):
                raise AppError("interactive on_output must be callable")
            # 实际目录可能由路径别名指向同一位置；归属比较使用解析后的路径集合。
            resolved_workspace = workspace.resolve()
            # 拒绝拿其他任务的副本启动进程，即使路径本身存在。
            if resolved_workspace not in task_workspaces:
                raise AppError("interactive workspace must be created by this task")
            # 已确认归属的副本仍须校验工作目录。
            # 其中的相对路径不能把子进程带到另一个工程。
            cwd = self.workspace_service.resolve_work_dir_in_workspace(
                resolved_workspace,
                work_dir,
            )
            # 在真正打开系统进程前再查取消，覆盖工作区路径校验期间到达的请求。
            self._raise_if_cancel_requested(task_id)

            # 输出保留平台日志，同时允许模块解析与自身协议有关的内容。
            def handle_output(message: str, stream: str) -> None:
                # 先记录平台日志，再把同一条输出转给模块解析；模块不持有日志连接。
                self.log_service.append(task_id, message, stream=stream)
                # 模块收到的是已经写入任务日志的同一条输出，不能绕过平台历史记录。
                if on_output is not None:
                    on_output(message, stream)

            # 平台运行器负责管道和实际启动。
            # 会话取得后先登记资源，再交给模块继续读取协议。
            # 运行器只接收校验后的 argv 与 cwd；会话创建失败由运行器处理其部分初始化资源。
            session = await self.process_runner.open_interactive_process(
                command=command,
                cwd=cwd,
                on_output=handle_output,
            )
            # 监视器和会话同步登记，模块获得会话前已有独立的取消观察路径。
            interactive_sessions.append(session)
            # 每个交互会话都附独立取消监视器。
            # 模块可能阻塞在调试协议读取，不能依赖它主动轮询任务记录。
            interactive_monitors.append(
                asyncio.create_task(
                    self._monitor_managed_interactive_session(task_id, session)
                )
            )
            # 进程启动与取消可能并发；取得会话后再检查持久化标记。
            if self.task_store.require(task_id).cancel_requested:
                # 启动完成后才看到的取消必须终止刚得到的句柄。
                # 会话此时已登记，外层仍可重复执行安全收尾。
                await session.terminate()
                # 新进程已启动而任务已被取消时先回收会话，再抛统一取消异常。
                raise CancellationRequested()
            return session

        # 通过绑定当前任务的回调公开能力，不把底层存储连接交给模块。
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
            _open_interactive_process=open_interactive_process,
        )
        # 模块执行前后都检查取消，防止临近完成的结果盖过已提交的取消请求。
        try:
            # 开始执行模块前重新确认取消状态，避免排队期间取消仍触发外部运行。
            self._raise_if_cancel_requested(task_id)
            # 总时限缺省时不为整段编排增加新的 deadline。
            # 各阶段仍可以使用上下文中的执行超时。
            if total_timeout_seconds is None:
                managed_result = await execute(context)
            else:
                # 总期限覆盖模块协程，单次操作期限仍在上下文中由模块消费。
                async with asyncio.timeout(total_timeout_seconds):
                    # 外层期限涵盖模块编排调用；超时后仍进入平台收尾段回收已取得的资源。
                    managed_result = await execute(context)
            # 模块返回值先校验形状，再复制到通用任务记录。
            self._raise_if_cancel_requested(task_id)
            # 只接收可存储的终态结果。
            # 模块返回后不直接发布产物，仍需完成交互资源回收。
            self._validate_managed_task_result(managed_result)
            # 只有验证完整的模块响应才写进本地终态候选；数据库最终仍可按取消事实裁决。
            task.status = managed_result.status
            # 复制业务结果字典，随后持久化平台状态、退出码和耗时。
            # 模块没有直接写任务记录的权限，竞态由最终提交决定。
            task.result = dict(managed_result.result)
            task.exit_code = managed_result.exit_code
            task.elapsed_ms = managed_result.elapsed_ms
            task.error = managed_result.error
            # 结束时间记录模块返回后的时刻；下方还有进程与工作区收尾。
            task.finished_at = utc_now()
            # 成功和失败都结束进度条；取消保持已观测的进度。
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
                task.progress = 100
            else:
                # 取消结果若没有解释，填入稳定的取消原因，且不强制将进度标记为完成。
                task.error = task.error or "cancelled"
        # 统一取消与协程取消走同一终态转换，保留已有进度。
        except (CancellationRequested, asyncio.CancelledError):
            # 模块抛平台取消或协程取消统一生成取消候选，不伪装成业务失败。
            self._mark_cancelled(task)
        # 配置了总时限时用统一提示，否则保留执行器自己的超时信息。
        except TimeoutError as exc:
            error = (
                str(exc)
                if total_timeout_seconds is None
                else f"managed task timed out after {total_timeout_seconds} seconds"
            )
            # 超时作为失败收尾，保留已分配资源的清理机会。
            self._mark_failed(task, error)
            task.progress = 100
        # 模块异常先形成失败结果，再走与正常返回相同的回收入口。
        except Exception as exc:
            self._mark_failed(task, str(exc))
            task.progress = 100
        # 先结束交互资源，再发布终态和处理目录。
        # 否则目录可能仍被运行中的进程占用。
        finally:
            # 关闭读写资源后再持久化结果，避免后继用户取得仍在写入的副本。
            interactive_failure = await self._finish_managed_interactive_sessions(
                task_id,
                interactive_sessions,
                interactive_monitors,
            )
            # 会话回收失败影响正常结束的任务，但不覆盖已经接受的取消。
            if interactive_failure is not None and task.status != TaskStatus.CANCELLED:
                # 回收失败可能改变任务结论；已经取消的任务仍保留取消语义。
                self._mark_failed(task, str(interactive_failure))
                task.progress = 100
            # 执行器未给出耗时时，用平台记录的起止时间补齐。
            # 仅在模块没有报告时长时才从平台生命周期估算，避免覆盖模块自己的测量。
            if (
                task.elapsed_ms is None
                and task.started_at is not None
                and task.finished_at is not None
            ):
                # 补计耗时仅在两个平台时间戳都存在时进行。
                # 模块已提供实测耗时时保持其原口径。
                task.elapsed_ms = max(
                    0, round((task.finished_at - task.started_at).total_seconds() * 1000)
                )
            # 终态记录、产物可用标记和清理意图由平台统筹，文件回收随后处理。
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

    # 监视器独立检查取消，让等待调试协议的模块也能终止进程。
    async def _monitor_managed_interactive_session(
        self,
        task_id: UUID,
        session: InteractiveProcessSession,
    ) -> None:
        # 退出码可用后停止查询取消标记，已结束进程不需要再次轮询任务存储。
        while session.returncode is None:
            # 监视只能看到当前是否存在退出码；未退出时需要持续读取消状态。
            try:
                cancelled = self.task_store.require(task_id).cancel_requested
            # 存储故障使监视器失去可靠的取消状态，采用保守终止策略。
            except Exception:
                cancelled = True
            # 读取消状态失败时按需要终止处理，避免交互进程在失去状态监控后继续运行。
            # 状态明确未取消时才继续轮询。
            if cancelled:
                # 检测到取消或无法可靠读取状态时，转入主动终止并退出监视。
                await session.terminate()
                return
            await asyncio.sleep(0.05)

    # 先取消监视器，再按资源创建的逆序结束会话，最后收集监视器退出。
    async def _finish_managed_interactive_sessions(
        self,
        task_id: UUID,
        sessions: list[InteractiveProcessSession],
        monitors: list[asyncio.Task[None]],
    ) -> Exception | None:
        # 保留第一个会话终止异常供最终任务状态判断；所有异常仍分别写诊断日志。
        failure: Exception | None = None
        # 先让监视器不再触发终止，再处理它们监视的会话。
        # 顺序关闭时仍继续处理其他已取得的资源。
        for monitor in monitors:
            # 先向监视发出取消，避免它在逐个收尾时再次发起检查。
            monitor.cancel()
        # 逆序终止支持模块先创建基础会话、后创建依赖会话的场景。
        for session in reversed(sessions):
            try:
                # 按会话创建的逆序释放，模块后打开的会话先结束。
                await session.terminate()
            # 某个会话的终止等待被取消，也继续尝试其余已登记会话。
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # 保存首个终止异常作为任务诊断，同时继续处理其余会话。
                failure = failure or exc
                logger.warning(
                    "failed to terminate managed interactive process: %s",
                    task_id,
                    exc_info=True,
                )
        # 等待所有监视协程结束，避免在任务终态之后继续访问存储。
        if monitors:
            # 监视回收的异常不替代已记录的进程终止错误。
            await asyncio.gather(*monitors, return_exceptions=True)
        return failure

    @staticmethod
    # 模块只能给出终态，并且业务结果必须能按 JSON 持久化。
    def _validate_managed_task_result(result: object) -> None:
        # 必须返回约定对象，不能把模块的任意字典隐式当成任务终态。
        if not isinstance(result, ManagedTaskResult):
            raise AppError("execute must return ManagedTaskResult")
        # 模块不能交回 PENDING 或 RUNNING；执行器退出后不再保有继续推进任务的资格。
        if not isinstance(result.status, TaskStatus) or result.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            raise AppError("managed task result must have a terminal status")
        # 业务结果需要字典包装并能序列化，避免只有运行态内存可读而轮询失败。
        if not isinstance(result.result, dict):
            raise AppError("managed task result must be a dict")
        # 验证结果可序列化为实际 JSON，而不是等到数据库写入时才发现不支持的类型。
        try:
            json.dumps(
                result.result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        # 序列化错误在提交前发现；否则终态事务可能因模块负载不能编码而失败。
        except (TypeError, ValueError) as exc:
            raise AppError("managed task result must be JSON serializable") from exc
        # 拒绝布尔值混作退出码或耗时，保持轮询响应的数值契约。
        if result.exit_code is not None and type(result.exit_code) is not int:
            raise AppError("managed task exit_code must be an int or None")
        # 耗时作为可选的非负整数，缺省时由平台用任务起止时间补齐。
        if result.elapsed_ms is not None and (
            type(result.elapsed_ms) is not int or result.elapsed_ms < 0
        ):
            raise AppError("managed task elapsed_ms must be a non-negative int or None")
        # 对外错误作为可读文本保存，不接受异常对象等运行时引用。
        if result.error is not None and not isinstance(result.error, str):
            raise AppError("managed task error must be a string or None")

    # 只依据已提交的终态处理目录，未成功写入终态则不清理。
    def _finish_managed_task_workspaces(
        self,
        *,
        finalized: TaskRecord | None,
        project_id: UUID,
        task_id: UUID,
        preserve_workspace_on_success: bool,
        workspace_completion_metadata_on_success: dict[str, object] | None,
    ) -> None:
        # 终态未取得时没有可靠的成功与持有判断，返回留待持久恢复或诊断。
        if finalized is None:
            return
        # 成功且声明保留副本时释放占用，并持久化后继步骤要用的元数据。
        if (
            finalized.status == TaskStatus.SUCCEEDED
            and preserve_workspace_on_success
        ):
            # 保存成功工作区时只撤销占用，完成元数据与释放同步交给存储。
            # 此分支不删除随后可下载的副本。
            try:
                self.release_task_workspaces(
                    task_id,
                    completion_metadata=workspace_completion_metadata_on_success,
                )
            # 保留成功副本时释放占用失败只记录，不能通过删除副本尝试修复状态。
            except Exception:
                logger.warning(
                    "failed to release managed workspace hold %s",
                    task_id,
                    exc_info=True,
                )
            return
        # 占用存在或无法确认时，宁可保留目录，也不能误删活跃会话的输入。
        if self._task_workspaces_are_held(task_id):
            # 存在持有者时不删除，释放方负责再次安排清理。
            return
        # 非保留路径仍要求持久化清理意图存在，单凭目录还在不能决定删除。
        try:
            cleanup_is_pending = (
                task_id in self.task_store.list_workspace_cleanup_pending()
            )
        # 清理队列读取异常时保留磁盘副本。
        # 缺少可靠的状态比清理延迟风险更大。
        except Exception:
            logger.warning(
                "failed to read managed workspace cleanup state %s; skipping cleanup",
                task_id,
                exc_info=True,
            )
            return
        # 缺少持久化清理意图时不删除，重启后的恢复不能依赖内存判断。
        if not cleanup_is_pending:
            # 缺少持久意图时不擅自删除，日志保留这次不一致供检查。
            logger.warning(
                "managed workspace cleanup intent is missing for task %s; skipping cleanup",
                task_id,
            )
            return
        # 只有通过终态、占用和清理意图检查后才进入真实磁盘回收。
        self._cleanup_managed_task_workspaces(project_id, task_id)

    # 回收入口再次读占用状态，因为它也用于后台重试和应用启动恢复。
    def _cleanup_managed_task_workspaces(self, project_id: UUID, task_id: UUID) -> None:
        # 真实删除前再查占用，覆盖安排清理与执行删除之间的阶段。
        if self._task_workspaces_are_held(task_id):
            return
        # 有限重试处理短暂目录占用；持续失败保留清理意图供以后恢复。
        for attempt in range(_MANAGED_WORKSPACE_CLEANUP_ATTEMPTS):
            try:
                self.workspace_service.cleanup_task_workspaces(project_id, task_id)
            # 文件锁等瞬时故障可以短暂重试；最终失败时仍保留清理意图。
            except Exception:
                if attempt + 1 < _MANAGED_WORKSPACE_CLEANUP_ATTEMPTS:
                    # 只有删除失败且还有次数时才短暂等待；不会重新执行任务内容。
                    time.sleep(_MANAGED_WORKSPACE_CLEANUP_RETRY_SECONDS)
                    continue
                logger.warning(
                    "failed to clean managed task workspace %s",
                    task_id,
                    exc_info=True,
                )
                # 耗尽本地重试后保留或重建清理意图，目录存在不能当作回收完成。
                self._mark_managed_workspace_cleanup_pending(task_id)
                return
            # 目录回收成功后再提交完成状态，保持删除操作可重放。
            try:
                # 目录先成功删除，再确认任务元数据和队列状态。
                # 完成状态写入失败时，下次可安全再试一个已不存在的目录。
                self.task_store.complete_workspace_cleanup(task_id)
            # 目录已删除但写完成元数据失败时，持久记录仍可用于以后完成账务收尾。
            except Exception:
                logger.warning(
                    "failed to complete managed workspace cleanup state %s",
                    task_id,
                    exc_info=True,
                )
            return

    # 回收失败后保留清理意图；如果记录本身也失败，只记录诊断，不再次猜测目录状态。
    def _mark_managed_workspace_cleanup_pending(self, task_id: UUID) -> None:
        try:
            self.task_store.mark_workspace_cleanup_pending(task_id)
        except Exception:
            logger.warning(
                "failed to persist managed workspace cleanup state %s",
                task_id,
                exc_info=True,
            )

    # 启动恢复按任务 ID 取得项目归属，而不直接消费持久记录提供的文件路径。
    def _recover_managed_task_workspace(self, task_id: UUID) -> None:
        # 恢复按任务记录定位项目，避免从未知路径猜测副本所有者。
        task = self.task_store.require(task_id)
        self._cleanup_managed_task_workspaces(task.project_id, task.id)

    # 无法读取占用状态时保守返回仍被持有。
    def _task_workspaces_are_held(self, task_id: UUID) -> bool:
        try:
            return self.task_store.workspaces_are_held(task_id)
        # 读不到占用状态时采用保守判断，不把未知占用当成允许删除。
        except Exception:
            logger.warning(
                "failed to read managed workspace hold state %s; skipping cleanup",
                task_id,
                exc_info=True,
            )
            return True

    # 单进程任务和受管任务共享持久化状态，但前者统一管理进程启动和退出码。
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
        # 固定和预备命令都先认领 PENDING；已取消的排队项不会创建工作区。
        task = self.task_store.try_start(task_id)
        # 进程任务没有拿到运行所有权时不登记进程控制器。
        if task is None:
            return
        # 记录当前事件循环和 worker，供外部取消请求安全投递。
        worker_task = asyncio.current_task()
        assert worker_task is not None
        # 注册启动控制供其他线程查询；实际启动点还会读数据库覆盖登记前的取消竞态。
        process_control = _ProcessTaskControl(asyncio.get_running_loop(), worker_task)
        # 控制器在复制副本前登记，以便处理准备阶段而不仅是进程运行期间的取消。
        with self._process_controls_lock:
            self._process_controls[task_id] = process_control

        task_workspace = None
        # 创建副本、准备和实际运行共用异常处理；任何阶段失败均需撤销控制注册。
        try:
            # 准备开始日志先于副本创建，轮询可区分排队和工作区准备阶段。
            task = self._report_progress(
                task,
                5,
                "task preparation started" if prepare is not None else f"task started: {command}",
            )
            # 即使调用方未指定来源，也先从工程源创建副本，不在原工程目录运行。
            # 复制发生在后台任务而不是提交请求，构建写入只影响这次副本。
            task_workspace = self.workspace_service.create_task_workspace(
                project_id,
                task_id,
                source_path=source_workspace,
                workspace_name=workspace_name,
            )
            # 动态准备结束后使用模块的实际命令；固定命令路径直接使用提交规格。
            if prepare is not None:
                # 带准备器时，准备和命令执行共享外层截止时刻。
                # 运行器自己的命令超时仍作为内层保护。
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                async with asyncio.timeout_at(deadline):
                    # 异步准备与进程执行在同一外层截止时间内，因此准备阶段会消耗总可用时间。
                    task, prepared = await self._prepare_process(task, task_workspace, prepare)
                    result = await self._run_controlled_process(
                        task_id=task_id,
                        task_workspace=task_workspace,
                        command=list(prepared.command),
                        work_dir=prepared.work_dir,
                        timeout_seconds=timeout_seconds,
                        process_control=process_control,
                    )
            # 固定命令没有准备器；运行器负责它的超时。
            # 两类任务在此前都已独立复制工作区。
            else:
                # 直接模式没有准备器，命令与工作目录由提交者提供；仍需走同一运行控制。
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
            # 保存运行器实测的退出码和耗时，再依退出码解释成功。
            task.finished_at = utc_now()
            # 退出码只在运行器交付结果时填充，启动失败走另一类诊断。
            task.exit_code = result.exit_code
            task.elapsed_ms = result.elapsed_ms
            task.progress = 100
            # 只有退出码为零才进入成功分支；产物发布仍须符合任务创建时的选项。
            if result.exit_code == 0:
                # 退出成功才附加发布副本的逻辑引用；是否公开仍由终态事务决定。
                task.status = TaskStatus.SUCCEEDED
                # 平台通用结果只标命令成功与可选产物引用；构建日志仍从日志服务查询。
                task.result = {"success": True}
                # 产物引用使用固定工作区名和任务 ID，下载路径仍要重新授权与解析。
                if artifacts_on_success:
                    task.result["artifact"] = {
                        "build_task_id": str(task.id),
                        "workspace": "workspace",
                    }
            # 非零退出表明命令执行但失败，保留退出码供调用方与启动异常区分。
            else:
                # 非零退出保留真实退出码并转换成平台失败，不能因进程正常回收而判成功。
                task.status = TaskStatus.FAILED
                task.error = f"command exited with code {result.exit_code}"
        # 模块检测的取消和底层协程取消都按相同终态处理，不归为命令非零退出。
        except CancellationRequested:
            self._mark_cancelled(task)
        except asyncio.CancelledError:
            # 协程取消也走统一终态路径，工作区清理仍会在 finally 执行。
            self._mark_cancelled(task)
        # 区分截止到期、工具未找到与普通执行错误，便于前端给出不同诊断。
        except TimeoutError as exc:
            self._mark_failed(
                task,
                str(exc) or f"task timed out after {timeout_seconds} seconds",
            )
        # 找不到工具单独报告，避免误解为编译器已运行并输出构建错误。
        except FileNotFoundError as exc:
            # 工具未找到时保留可执行文件诊断；不回退到另一条命令。
            self._mark_failed(task, f"executable not found: {exc.filename}")
        except Exception as exc:
            self._mark_failed(task, str(exc))
        # 撤下进程控制后再提交终态，并按最终持久化结果决定是否保留副本。
        finally:
            # 只移除这次注册的控制器，然后读取真实终态决定副本去留。
            with self._process_controls_lock:
                # 只撤销自己的控制器，避免结束时按任务键误删其他登记对象。
                if self._process_controls.get(task_id) is process_control:
                    self._process_controls.pop(task_id, None)
            # 只有产物已请求且存储确认成功时保留副本；其他结论进入进程任务的回收路径。
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

    # 启动前解析工作目录，再进入不可丢失进程句柄的取消控制区间。
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
        # 运行前解析 cwd，再进入受取消保护的启动段，路径问题无需启动任何进程。
        cwd = self.workspace_service.resolve_work_dir_in_workspace(task_workspace, work_dir)
        # 检查取消位要读持久化记录，不能使用运行开始时捕获的快照。
        def is_cancelled() -> bool:
            return self.task_store.require(task_id).cancel_requested

        # 控制器将持久化取消检查与进程启动的过渡区连接起来。
        # 运行器取得句柄后通过回调解除启动期的取消限制。
        process_control.begin_launch(is_cancelled)
        # 从登记启动到交回句柄都保持控制器活跃；失败也由 finally 解除窗口。
        try:
            # 运行器负责 stdout 和 stderr、进程退出及超时；平台只用回调关联任务和流。
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
        # 无论运行器成功还是异常都清除启动标记，避免后续取消等待不存在的句柄。
        finally:
            # 清理启动标记不执行系统终止；运行器应已对自己取得的进程进行收尾。
            process_control.finish_launch()

    # 准备阶段在任务副本内运行，并且不能在取消后更新正式命令。
    async def _prepare_process(
        self,
        task: TaskRecord,
        workspace: Path,
        prepare: TaskPreparer,
    ) -> tuple[TaskRecord, PreparedProcess]:
        # 先检查取消再构造准备上下文，避免已取消的任务做下一次准备。
        self._raise_if_cancel_requested(task.id)

        # 准备器的进度更新会同步更新本地任务快照，后续记录不会覆盖这次新进度。
        def report_progress(percent: int, message: str) -> None:
            nonlocal task
            task = self._report_progress(task, percent, message, stream="module.prepare")

        # 准备回调只得到当前副本解析器和任务相关的日志与进度能力。
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
        # 异步准备可能等待文件和工具。
        # 返回后重新校验取消与命令结构，才发布将执行的规格。
        prepared = await prepare(context)
        # 准备器返回后再次检查取消，防止把已生成的命令发布给正在退出的任务。
        self._raise_if_cancel_requested(task.id)
        # 验证的是模块交付形状，真正的工作目录存在性还要在运行前解析。
        self._validate_prepared_process(prepared)
        # 持久化的记录命令可以与实际命令不同，避免保存不适合展示的参数。
        recorded_command = (
            prepared.recorded_command
            if prepared.recorded_command is not None
            else prepared.command
        )
        # 按运行状态更新命令；如果已经不能更新，不能继续启动进程。
        updated = self.task_store.update_command_if_running(task.id, recorded_command)
        # 条件更新未命中等同于已失去执行资格；不能继续使用刚计算出的命令。
        if updated is None:
            # 准备后的命令未能在运行态提交时，不允许继续调用运行器。
            raise CancellationRequested()
        # 保存命令后再检查一次取消，覆盖数据库更新与启动之间的窗口。
        self._raise_if_cancel_requested(task.id)
        return updated, prepared

    @staticmethod
    # 先验证 argv 结构和目录类型，具体目录归属在实际运行前处理。
    def _validate_prepared_process(prepared: object) -> None:
        # 要求结构化进程规格，使运行参数和记录参数各自有清晰来源。
        if not isinstance(prepared, PreparedProcess):
            raise AppError("prepare must return PreparedProcess")
        if not isinstance(prepared.command, list) or not prepared.command or not all(
            isinstance(item, str) and item for item in prepared.command
        ):
            raise AppError("prepared command must contain non-empty strings")
        # 记录命令如果提供，也必须是完整的非空参数列表。
        # 它可以隐藏实际参数，但不能破坏任务响应形状。
        if prepared.recorded_command is not None and (
            not isinstance(prepared.recorded_command, list)
            or not prepared.recorded_command
            or not all(
                isinstance(item, str) and item for item in prepared.recorded_command
            )
        ):
            raise AppError("recorded command must contain non-empty strings")
        # 这里只检查目录字段类型，解析后的归属与存在性由平台启动路径验证。
        if not isinstance(prepared.work_dir, str):
            # 工作目录类型检查不允许模块传入任意路径对象；路径语义在后续平台解析处统一。
            raise AppError("prepared work_dir must be a string")

    @staticmethod
    # 允许异步函数或实现异步调用方法的对象，统一按协程等待。
    def _is_async_preparer(prepare: TaskPreparer) -> bool:
        return inspect.iscoroutinefunction(prepare) or (
            callable(prepare) and inspect.iscoroutinefunction(prepare.__call__)
        )

    @staticmethod
    # 支持异步可调用对象，与单独协程函数共用同一种执行契约。
    def _is_async_executor(execute: ManagedTaskExecutor) -> bool:
        return inspect.iscoroutinefunction(execute) or (
            callable(execute) and inspect.iscoroutinefunction(execute.__call__)
        )

    @staticmethod
    # 取消回调须能被调用路径等待；不能把同步网络请求直接放进关闭流程。
    def _is_async_cancellation_handler(
        handler: ManagedTaskCancellationHandler,
    ) -> bool:
        return inspect.iscoroutinefunction(handler) or (
            callable(handler) and inspect.iscoroutinefunction(handler.__call__)
        )

    # 仅允许同一项目内成功任务的固定副本作为后续输入。
    # 缺省时交给调用方选择项目源树。
    def _resolve_process_source_workspace(
        self,
        project_id: UUID,
        source_task_id: UUID | None,
    ) -> Path | None:
        # 先要求项目存在；无来源任务时由调用方使用项目源，不生成伪造的工作区路径。
        self.workspace_service.require_project(project_id)
        # 没有来源任务时沿用项目原始源码作为复制输入，而非某个历史成功构建。
        if source_task_id is None:
            return None
        # 检查工程与成功终态后，才在该工程的任务树中定位来源。
        source_task = self.task_store.require(source_task_id)
        if source_task.project_id != project_id or source_task.status != TaskStatus.SUCCEEDED:
            raise AppError("source_task_id must reference a succeeded task in the same project")
        # 来源任务状态验证通过后仍需磁盘副本可用，历史记录不保证目录未被回收。
        return self.workspace_service.resolve_task_workspace(project_id, source_task_id)

    # 取消时设置完成时间和通用原因，进度不强行写为百分之百。
    def _mark_cancelled(self, task: TaskRecord) -> None:
        # 这里只更新 worker 的本地候选；记录真正转终态由统一提交函数完成。
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        task.error = "cancelled"

    # 以存储层回读结果同步进度，取消或非运行状态不再追加进度事件。
    def _report_progress(
        self,
        task: TaskRecord,
        percent: int,
        message: str,
        stream: str = "co_debug.executor",
    ) -> TaskRecord:
        # 持久化进度更新还负责阻止进度倒退。
        # 当前快照用于同时决定可否继续和写入哪一个进度值。
        latest = self.task_store.update_progress(task.id, percent)
        # 先对齐数据库接受的进度，再判定任务是否仍允许继续工作。
        task.progress = latest.progress
        # 如果并发请求让记录失去运行资格，进度报告本身也成为取消检查点。
        if latest.status != TaskStatus.RUNNING or latest.cancel_requested:
            raise CancellationRequested()
        # 先提交进度，再追加同一数值的日志事件，轮询与流消费可对照。
        # 只记录已接受的进度值，不把被存储拒绝的后退进度写成事件。
        self.log_service.append(
            task.id,
            message,
            stream=stream,
            progress=latest.progress,
        )
        return latest

    # 读取最新持久化标记，不依赖 worker 手上的旧任务快照。
    def _raise_if_cancel_requested(self, task_id: UUID) -> None:
        if self.task_store.require(task_id).cancel_requested:
            raise CancellationRequested()

    # 失败标记在内存记录上组装；真正提交还要经过终态写入的并发检查。
    def _mark_failed(self, task: TaskRecord, message: str) -> None:
        # 失败助手保存诊断与结束时间；不同执行路径可再补充自己的进度和退出码。
        task.status = TaskStatus.FAILED
        task.finished_at = utc_now()
        task.error = message

    # 日志展示存储确认的终态，而不是模块一度尝试提交的状态。
    def _log_final_state(self, task: TaskRecord) -> None:
        if task.status == TaskStatus.SUCCEEDED:
            message = "task succeeded"
        elif task.status == TaskStatus.FAILED:
            message = f"task failed: {task.error}"
        else:
            message = "task cancelled"
        # 最终日志记录实际终态与最后进度，不负责改变平台记录。
        self.log_service.append(task.id, message, progress=task.progress)

    # 集中处理终态持久化与资源回收，让清理在日志错误时仍有机会执行。
    def _finalize_with_cleanup(
        self,
        task: TaskRecord,
        cleanup: Callable[[TaskRecord | None], None],
        *,
        artifacts_on_success: bool = False,
        queue_workspace_cleanup_on_finalize: bool = False,
        preserve_workspace_on_success: bool = False,
    ) -> None:
        # 没有持久终态时传空给清理回调，避免把 worker 的建议结果当作已提交事实。
        finalized: TaskRecord | None = None
        # 终态提交与文件系统清理分开，后者总会按本次可取得的实际结果执行。
        try:
            # 终态和资源标记由存储一次提交，返回记录决定实际清理策略。
            finalized, changed = self.task_store.finalize_with_transition(
                task,
                artifacts_on_success=artifacts_on_success,
                queue_workspace_cleanup_on_finalize=(
                    queue_workspace_cleanup_on_finalize
                ),
                preserve_workspace_on_success=preserve_workspace_on_success,
            )
            # 只有本调用赢得状态迁移才追加终态日志。
            # 重复收尾不会制造两个成功或失败事件。
            if changed:
                self._log_final_state(finalized)
        # 日志写入失败时仍调用清理；收到的记录可能为空，回收策略必须能识别这个边界。
        finally:
            # 即使持久化或最终日志失败，也调用当前执行类型自己的回收策略。
            cleanup(finalized)

