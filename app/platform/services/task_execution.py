from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.core.errors import CancellationRequested
from app.platform.domain.enums import TaskStatus


# 准备器提交命令和相对工作目录，实际启动由平台负责。
# 记录命令可不同于运行参数，方便模块隐藏不宜持久化的细节。
@dataclass(slots=True, frozen=True)
class PreparedProcess:
    """The local process a module asks the platform to execute."""

    # 真实 argv 可以含副本绝对路径；HTTP 记录可用另一份逻辑命令隐藏机器相关信息。
    command: list[str]
    # 工作目录字段相对本次副本解释；准备器不把宿主路径作为工作目录承诺。
    work_dir: str = "."
    # 省略记录用命令时上层可沿用执行 argv；需要隐藏实现细节的模块应显式提供公开版本。
    recorded_command: list[str] | None = None


# 类型别名描述回调的最小能力；调用模块不需要访问 TaskService 的完整接口。
LogAppender = Callable[[str, str], None]
ProgressReporter = Callable[[int, str], None]
# 取消谓词和进度回调可以由后台任务线程使用；调用模块只消费绑定当前任务的能力。
CancelCheck = Callable[[], bool]
PathResolver = Callable[[str], Path]
WorkspaceCreator = Callable[[str | None], Path]
WorkspacePathResolver = Callable[[Path, str], Path]
ManagedLogAppender = Callable[[str, str, int | None], None]
ManagedProgressReporter = Callable[[int, str, str], None]
MetadataMerger = Callable[[dict[str, object]], None]
WorkspaceHolder = Callable[[dict[str, object] | None], None]
WorkspaceReleaser = Callable[[bool, dict[str, object] | None], None]


# 交互模块通过最小会话接口驱动进程，平台保留传输和清理责任。
class InteractiveProcessSession(Protocol):
    @property
    # 无退出码只表示尚未观察到进程退出，不表明协议已经可以接收命令。
    def returncode(self) -> int | None: ...

    # 写入面向交互进程标准输入，是否等待协议应答由模块自己决定。
    async def write(self, data: str) -> None: ...

    # wait 给出底层退出码；terminate 是资源收尾能力，不替模块定义业务终态。
    async def wait(self) -> int: ...

    # 终止是回收动作，命令是否成功仍由模块自己的协议或退出结果定义。
    async def terminate(self) -> None: ...


# 输出回调接收内容和流标识，由模块在不接触管道句柄的情况下解析协议。
InteractiveOutputCallback = Callable[[str, str], None]
InteractiveProcessOpener = Callable[
    [list[str], Path, str, InteractiveOutputCallback | None],
    Awaitable[InteractiveProcessSession],
]


# 准备阶段只获得已分配的副本和受限能力，不能自行替换平台任务记录。
@dataclass(slots=True)
class TaskPreparationContext:
    """The bounded platform capabilities available while a module prepares a task."""

    # 上下文的任务与项目标识由平台绑定，模块不为准备阶段重新分配任务 ID。
    task_id: UUID
    project_id: UUID
    # 准备器拿到的是本次要运行的隔离副本；提交入口不通过该对象访问原项目写路径。
    workspace: Path
    _append_log: LogAppender = field(repr=False)
    _report_progress: ProgressReporter = field(repr=False)
    _is_cancelled: CancelCheck = field(repr=False)
    _resolve_path: PathResolver = field(repr=False)

    # 日志和进度是两个不同接口；仅记录诊断文本不会改变任务进度。
    def log(self, message: str, stream: str = "module.prepare") -> None:
        # 默认日志来源标记准备阶段，可与真实子进程产生的 stdout 或 stderr 区分。
        self._append_log(message, stream)

    # 准备器通过平台推进进度，不直接改持久任务对象。
    def report_progress(self, percent: int, message: str) -> None:
        self._report_progress(percent, message)

    # 取消检查读取平台给定的当前事实；不从日志或局部进度推断取消。
    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    # 准备器在耗时步骤前主动检查，使用统一的取消异常结束执行。
    def raise_if_cancelled(self) -> None:
        # 只有平台回调确认本任务收到取消才抛异常；该方法不发起额外取消。
        if self.is_cancelled():
            raise CancellationRequested()

    # 路径解析委托给绑定当前副本的平台回调，准备器无需处理工程根推导。
    def resolve_path(self, path: str) -> Path:
        # 这一解析器已绑定当前工作区，调用方不用再把工作区根附进相对路径。
        return self._resolve_path(path)


# 准备器必须异步返回单次进程规格；不负责收集退出状态。
class TaskPreparer(Protocol):
    async def __call__(
        self,
        context: TaskPreparationContext,
    ) -> PreparedProcess: ...


# 受管执行器可以编排多个步骤，平台通过回调控制工作区、进程和持久化。
# 每份上下文绑定一个任务及项目，不直接交付底层任务存储对象。
@dataclass(slots=True)
class ManagedTaskContext:
    """Bounded platform capabilities for module-owned task orchestration."""

    # 身份字段贯穿整个托管执行；上下文期限可供模块约束单步操作。
    task_id: UUID
    project_id: UUID
    # 超时参数交给模块用于编排步骤；是否是每命令预算由调用该上下文的执行器决定。
    timeout_seconds: int
    _append_log: ManagedLogAppender = field(repr=False)
    _report_progress: ManagedProgressReporter = field(repr=False)
    _is_cancelled: CancelCheck = field(repr=False)
    _create_workspace: WorkspaceCreator = field(repr=False)
    _resolve_path: WorkspacePathResolver = field(repr=False)
    # 恢复需要的外部引用可用持久元数据提交，不把临时进程对象存进任务记录。
    _merge_metadata: MetadataMerger = field(repr=False)
    _hold_workspaces: WorkspaceHolder = field(repr=False)
    _release_workspaces: WorkspaceReleaser = field(repr=False)
    _open_interactive_process: InteractiveProcessOpener = field(repr=False)

    # 普通日志可附当前进度供展示，但改变任务进度应使用专门的报告方法。
    def log(
        self,
        message: str,
        stream: str = "module.managed",
        progress: int | None = None,
    ) -> None:
        # 日志携带的 progress 是事件字段；单次追加不替代更新任务进度的接口。
        self._append_log(message, stream, progress)

    # 与普通日志区分，报告进度还会更新任务记录供轮询读取。
    def report_progress(
        self,
        percent: int,
        message: str,
        stream: str = "module.managed",
    ) -> None:
        # 这条回调同时携带业务阶段和来源，调用方可按阶段统一显示任务活动。
        self._report_progress(percent, message, stream)

    # 模块可用布尔查询配合自己的循环；需要立即结束时使用抛异常的接口。
    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    # 模块可在外部请求或副本操作之间主动结束，避免继续启动后续步骤。
    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationRequested()

    # 工作区由平台创建；名称用于需要可定位副本的业务步骤。
    def create_workspace(self, workspace_name: str | None = None) -> Path:
        # 多个业务步骤可请求不同的副本名称；目录归属仍绑定平台任务。
        return self._create_workspace(workspace_name)

    # 模块指定要解析的副本，平台仍检查请求位置不能越过该副本边界。
    def resolve_path(self, workspace: Path, relative_path: str) -> Path:
        # 路径解析与副本创建分离，使各轮执行始终以自己的副本为坐标。
        return self._resolve_path(workspace, relative_path)

    # 可持久化元数据由平台按任务 ID 合并，适合保存恢复所需的外部运行引用。
    def merge_metadata(self, updates: dict[str, object]) -> None:
        self._merge_metadata(updates)

    # 完成后的资源如仍被会话使用，可移交给后续释放方。
    def hold_workspaces(
        self,
        metadata_updates: dict[str, object] | None = None,
    ) -> None:
        # 持有关系用于防止平台在后续使用结束前清理副本，和是否可下载是不同标记。
        self._hold_workspaces(metadata_updates)

    # 释放占用时可选择同步更新完成元数据，避免归属信息与资源状态脱节。
    def release_workspaces(
        self,
        *,
        cleanup: bool = False,
        completion_metadata: dict[str, object] | None = None,
    ) -> None:
        # 如果要求清理，完成元数据与实际目录回收的关系由平台服务协调。
        # 模块不需要自行删除任务根。
        self._release_workspaces(cleanup, completion_metadata)

    # 启动后只交付会话接口；工作目录和生命周期由平台回调约束。
    async def open_interactive_process(
        self,
        command: list[str],
        workspace: Path,
        work_dir: str = ".",
        on_output: InteractiveOutputCallback | None = None,
    ) -> InteractiveProcessSession:
        # 启动接口交给绑定本任务的回调实际处理；调用方没有修改进程注册表的入口。
        return await self._open_interactive_process(
            command,
            workspace,
            work_dir,
            on_output,
        )


# 执行器返回平台终态和业务产物；错误信息与退出码允许按任务类型分别提供。
@dataclass(slots=True, frozen=True)
class ManagedTaskResult:
    # 结果是模块提出的终态和业务数据，存储仍可能根据并发取消决定最终发布状态。
    status: TaskStatus
    # 结果字段承载可持久化的数据；进程句柄和事件循环对象应留在执行上下文中。
    result: dict = field(default_factory=dict)
    # 不执行本机命令的任务仍能给出平台终态，退出码可以保持缺省。
    exit_code: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None


# 执行器协程完成时须交回终态结果；平台负责后续统一持久化与回收。
class ManagedTaskExecutor(Protocol):
    async def __call__(self, context: ManagedTaskContext) -> ManagedTaskResult: ...


# 取消钩子用于通知模块自己的资源；截止时间由上层传入并可能缺省。
class ManagedTaskCancellationHandler(Protocol):
    # 取消处理入口通过任务 ID 定位模块所有资源；deadline 若给出可限制该次通知预算。
    def __call__(
        self,
        task_id: UUID,
        deadline: float | None = None,
    ) -> Awaitable[None]: ...
