from __future__ import annotations

import asyncio
import stat
import time
from pathlib import Path
from uuid import UUID

from app.core.errors import AppError
from app.modules.co_debug.debug.broker import DebugCommandBroker
from app.modules.co_debug.debug.events import DebugSessionEventPublisher
from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
    GdbMiCommandDispatcher,
)
from app.modules.co_debug.debug.platform_transport import (
    PlatformGdbTransport,
)
from app.modules.co_debug.debug.session import GdbMiSession
from app.modules.co_debug.debug.transport import GdbTransportClosed
from app.modules.co_debug.schemas.debug import (
    DebugBuildCandidate,
    DebugExecutableCandidate,
)
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import DebugTaskRequest
from app.platform.services.task_execution import (
    ManagedTaskContext,
    ManagedTaskResult,
)
from app.platform.services.task_service import TaskService


class InteractiveDebugService:
    """
    B7/B8 的真实 GDB/MI Managed Task 运行入口。

    当前阶段先支持：

        uploaded project source
            ↓
        Managed Task workspace
            ↓
        GDB/MI interactive process

    build_task_id 对应的 retained build workspace
    等 A 补充 managed source workspace 后再接入。
    """

    def __init__(
        self,
        *,
        task_service: TaskService,
        session_manager: DebugSessionManager,
        event_publisher: DebugSessionEventPublisher | None = None,
    ) -> None:
        # 任务提交使用平台服务；注册表和发布器与命令执行循环共享同一组会话身份。
        self.task_service = task_service
        self.session_manager = session_manager
        self.event_publisher = event_publisher
        self.dispatcher = GdbMiCommandDispatcher()


    # 候选以平台可下载产物为来源，不递归枚举服务器目录或任意任务副本。
    def list_candidates(
        self,
        *,
        project_id: UUID | None = None,
    ) -> list[DebugBuildCandidate]:
        """Return retained successful builds that contain debuggable ELF executables."""
        candidates: list[DebugBuildCandidate] = []

        # 只从成功构建且仍保留的产物发现候选；任务历史本身不能证明可运行文件还存在。
        for task in self.task_service.list_tasks():
            if (
                task.task_type != TaskType.BUILD
                or task.status != TaskStatus.SUCCEEDED
            ):
                continue

            # 未指定项目时浏览全部符合条件的构建；指定时先筛选项目再检查产物可用性。
            if (
                project_id is not None
                and task.project_id != project_id
            ):
                continue

            # 构建记录存在也可能未公开产物，无法访问时继续检查其他构建。
            try:
                artifacts = self.task_service.list_task_artifacts(
                    task.id
                )
            except AppError:
                # Task history can outlive retained build artifacts.
                # Such records are not valid debug candidates.
                continue

            # 后续入口使用构建任务 ID 和产物相对路径，客户端不依赖宿主存储目录。
            executables: list[DebugExecutableCandidate] = []

            # 逐个解析文件并检查 ELF 类型，不能只按文件后缀把目标文件当作程序。
            for artifact_path, _ in artifacts:
                # 列举到的相对路径仍要重新解析，清单本身不能授权稍后的文件读取。
                try:
                    artifact = self.task_service.resolve_task_artifact(
                        task.id,
                        artifact_path,
                    )
                except AppError:
                    # Artifact availability can change between list/resolve.
                    continue

                # 候选识别只检查文件形状；是否能用本机调试器启动仍由实际会话确认。
                if not self._is_debuggable_executable(artifact):
                    continue

                # 展示名称可重复，真正用于启动的是该构建目录内的相对路径。
                executables.append(
                    DebugExecutableCandidate(
                        name=artifact.name,
                        executable_path=artifact_path,
                    )
                )

            # 没有可运行候选的构建不返回空分组，避免前端提供无效选项。
            if not executables:
                continue

            # 按相对路径稳定排序，便于界面重复刷新后保持选择。
            executables.sort(
                key=lambda item: item.executable_path
            )

            # 任务元数据区分普通构建与依赖修复构建；二者复用同一调试启动路径。
            build_kind = (
                "repair-build"
                if task.metadata.get("operation")
                == "dependency_repair_build"
                else "build"
            )

            # 一条构建记录分组包含多个可执行候选；创建时间与来源任务 ID 都从构建记录取得。
            candidates.append(
                DebugBuildCandidate(
                    project_id=task.project_id,
                    build_task_id=task.id,
                    build_kind=build_kind,
                    created_at=task.created_at,
                    executables=executables,
                )
            )

        return candidates

    @staticmethod
    # 候选识别仅读取 ELF 头和必要的程序头；不执行程序试探格式。
    def _is_debuggable_executable(
        path: Path,
    ) -> bool:
        # 先排除当前不是文件的路径，设备、目录等不进入 ELF 探测。
        if not path.is_file():
            return False

        # 通过文件内容识别候选，不启动目标。
        # ELF 类别与字节序决定头部字段位置及解释方式。
        try:
            # 只读取足够判断 ELF 类别与类型的头部。
            # 需要区分 PIE 时才访问程序头表。
            with path.open("rb") as executable:
                header = executable.read(64)
                if len(header) < 18 or header[:4] != b"\x7fELF":
                    return False

                # 字节序决定后续字段解析，无法识别时不猜测当前主机端序。
                byte_order = {
                    1: "little",
                    2: "big",
                }.get(header[5])
                # 端序编码不受宿主默认值影响；不能识别的编码不会被当作本机可执行。
                if byte_order is None:
                    return False

                # ZIP extraction intentionally does not trust archived permission
                # bits, so retained build outputs can lose X_OK. Inspect the ELF
                # object type instead; ET_REL objects stay excluded.
                elf_type = int.from_bytes(header[16:18], byteorder=byte_order)
                # ET_EXEC 可直接列为候选；ET_DYN 需要进一步区分可运行的 PIE 和普通库。
                if elf_type == 2:  # ET_EXEC
                    return True
                if elf_type != 3:  # ET_DYN can be either a PIE or a shared library.
                    return False

                # PIE 与共享库使用相同 ELF 类型，按 ELF32 或 ELF64 位置读取程序头索引。
                # ET_DYN 也可能是共享库，因此检查程序头里的解释器记录再接受候选。
                elf_class = header[4]
                # ELF32 与 ELF64 的程序头偏移布局不同，先确保数据长度足够再取字段。
                if elf_class == 1 and len(header) >= 46:  # ELF32
                    program_offset = int.from_bytes(header[28:32], byteorder=byte_order)
                    entry_size = int.from_bytes(header[42:44], byteorder=byte_order)
                    entry_count = int.from_bytes(header[44:46], byteorder=byte_order)
                elif elf_class == 2 and len(header) >= 58:  # ELF64
                    program_offset = int.from_bytes(header[32:40], byteorder=byte_order)
                    entry_size = int.from_bytes(header[54:56], byteorder=byte_order)
                    entry_count = int.from_bytes(header[56:58], byteorder=byte_order)
                else:
                    return False

                # 约束条目宽度和数量，避免损坏头部导致无界的文件读取。
                # 限制程序头条目数，避免异常文件头使候选探测变成长扫描。
                if entry_size < 4 or not 0 < entry_count <= 1024:
                    return False
                # 动态对象须有解释器程序头才当作可调试 PIE，普通共享库不进入候选。
                for index in range(entry_count):
                    # 每个程序头仅读取类型字段，不加载节或运行目标；解释器条目只是格式层面的可执行证据。
                    executable.seek(program_offset + index * entry_size)
                    if int.from_bytes(executable.read(4), byteorder=byte_order) == 3:
                        return True  # PT_INTERP identifies a runnable PIE.
                return False
        # 读文件失败只淘汰当前候选，继续允许调用方选择其他产物。
        except OSError:
            return False

    @classmethod
    # 对本次任务副本补执行权限；原构建产物不因启动会话而修改。
    def _ensure_debuggable_executable(
        cls,
        path: Path,
    ) -> None:
        if not cls._is_debuggable_executable(path):
            raise AppError("debug executable is not a runnable ELF file")

        # 校验后仅补所有者执行位，保留其他权限；调用方应只传私有副本。
        try:
            # 执行位修改只在本次复制的文件上发生；这一步失败会在启动 GDB 前报告。
            current_mode = path.stat().st_mode
            if not current_mode & stat.S_IXUSR:
                path.chmod(current_mode | stat.S_IXUSR)
        except OSError as exc:
            raise AppError("failed to prepare debug executable permissions") from exc

    # 创建会话前验证可执行文件来源，后续工作区由平台按同一来源复制。
    async def create_task(
        self,
        request: DebugTaskRequest,
    ) -> TaskRecord:
        # 显式指定构建来源时验证项目、类型和成功状态，避免跨项目引用文件。
        if request.build_task_id is not None:
            # 构建任务仅作为快照来源，不会因启动调试重新进入运行状态。
            build_task = (
                self.task_service
                .require_task(
                    request.build_task_id
                )
            )

            # 使用项目与任务类型同时校验引用，防止同名产物掩盖错误的构建来源。
            if (
                build_task.project_id
                != request.project_id
                or build_task.task_type
                != TaskType.BUILD
            ):
                raise AppError(
                    "build_task_id must reference "
                    "a build task in the same project"
                )

            # 状态成功是选择构建来源的前提；产物的实际存在性仍在下面核对。
            if (
                build_task.status
                != TaskStatus.SUCCEEDED
            ):
                raise AppError(
                    "build task must succeed "
                    "before starting debug"
                )

        # 提交阶段解析逻辑目标；实际启动前仍会在隔离工作区重新解析。
        logical_executable = (
            self.task_service
            .find_process_source_file(
                project_id=request.project_id,
                path=request.executable_path,
                 source_task_id=(
                    request.build_task_id
                ),
            )
        )

        # 无法在选定来源找到文件时不登记一个不可启动的会话任务。
        if logical_executable is None:
            raise AppError(
                "debug executable does not exist: "
                f"{request.executable_path}"
            )

        # 任务记录保留逻辑命令；交给进程的是隔离目录中目标的实际路径。
        logical_command = self._gdb_command(
            logical_executable,
            request.args,
        )

        # 命令通道跨 API 和后台任务循环共享，实际 GDB 会话在 worker 创建。
        broker = DebugCommandBroker()

        # 这里声明后台执行步骤，HTTP 提交线程不会打开或持有 GDB 的管道。
        async def execute(
            context: ManagedTaskContext,
        ) -> ManagedTaskResult:
            # 会话耗时含副本准备与 GDB 启动，不只测命令交互阶段。
            started = time.perf_counter()

            # 平台负责准备项目或指定构建来源的副本，调试层不直接就地执行归档。
            workspace = context.create_workspace(
                "debug"
            )

            # 副本内路径再次交由平台解析；实际执行位置不直接信任提交阶段的逻辑路径。
            executable = context.resolve_path(
                workspace,
                logical_executable,
            )

            # 副本内容可能与提交时观察不同，必须在打开传输前重新验证程序。
            if not executable.is_file():
                raise AppError(
                    "debug executable does not exist: "
                    f"{request.executable_path}"
                )

            # This is the DEBUG task's private copy. The retained BUILD artifact
            # and Project source remain immutable.
            # 对私有副本再次识别 ELF 并补权限，既不修改留存构建也不修改上传源码。
            self._ensure_debuggable_executable(executable)

            # 跟踪各阶段已取得的资源，以便准备失败时按实际所有权收尾。
            # 启动可能在进程创建、握手和注册等不同阶段中止。
            # 分别记录已获得的句柄，只收尾实际存在的资源。
            transport: PlatformGdbTransport | None = None
            session: GdbMiSession | None = None
            dispatcher_task: asyncio.Task[None] | None = None
            process_wait_task: asyncio.Task[int] | None = None
            registered = False

            try:
                # 进度事件仅表示开始启动；从这里到注册之间还可能因 GDB 配置失败而结束。
                context.report_progress(
                    10,
                    "starting GDB/MI session",
                    stream="co_debug.gdb",
                )

                # 进程位置使用副本中解析得到的实际路径；平台统一约束 cwd 和进程归属。
                transport = await PlatformGdbTransport.open(
                    context=context,
                    command=self._gdb_command(
                        str(executable),
                        request.args,
                    ),
                    workspace=workspace,
                    work_dir=request.work_dir,
                )

                # 会话绑定平台任务标识，断点命中事件可据此路由到协同规则。
                # 发布的协同事件用当前平台任务 ID 定位成员，不依赖宿主 PID。
                session = GdbMiSession(
                    transport,
                    command_timeout_seconds=(
                        context.timeout_seconds
                    ),
                    session_task_id=context.task_id,
                    event_publisher=self.event_publisher,
                )

                # 除了首个提示符，还要配置全停与异步命令模式后才公开会话。
                # 先等待提示符，再配置全停与异步控制；均成功后才注册会话。
                await session.start(
                    timeout_seconds=(
                        context.timeout_seconds
                    )
                )
                # 全停和异步控制在通道开放前设置。
                # 调用者看到已就绪时不用自行发送这些初始化命令。
                await session.enforce_all_stop()
                await session.enable_async_execution()

                # 只有真正收到 (gdb) prompt 后，
                # 才向API侧暴露这个Session。
                # 注册只发生在协议握手和初始化完成后；后续 HTTP 命令必须通过这个任务 ID。
                self.session_manager.register(
                    context.task_id,
                    broker,
                )
                registered = True

                # 已就绪日志不代表目标已启动；目标运行需要之后的命令。
                context.report_progress(
                    50,
                    "GDB/MI session ready",
                    stream="co_debug.gdb",
                )

                # 命令分发和进程结束并行等待，任一路结束都会进入终态判定。
                # 命令消费者与进程退出监听分别运行，任一先结束都可推进收尾。
                dispatcher_task = asyncio.create_task(
                    self.dispatcher.run(
                        broker=broker,
                        session=session,
                    )
                )

                # 退出监听只等待底层调试器；它不把 inferior 暂停或退出直接视为调试器退出。
                process_wait_task = asyncio.create_task(
                    transport.wait()
                )

                # 先完成的一方决定收尾分支；若两者同时完成，优先检查客户端分发。
                done, _ = await asyncio.wait(
                    {
                        dispatcher_task,
                        process_wait_task,
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # 会话耗时从准备副本前累计，到首次观察到关闭或进程完成时截取。
                elapsed_ms = round(
                    (
                        time.perf_counter()
                        - started
                    )
                    * 1000
                )

                 # --------------------------
                # 情况1：
                # Dispatcher先结束。
                #
                # 当前正常情况就是客户端发送CLOSE。
                # --------------------------

                # 命令分发结束仍须 await 传播异常，不能一律视为主动关闭成功。
                # 客户端完成关闭时保存 MI 最终状态，主动终止按成功收束。
                if dispatcher_task in done:
                    # 读取分发器结果以传播真正的循环故障，不能只因任务出现在完成集合中就判成功。
                    await dispatcher_task

                    return ManagedTaskResult(
                        status=TaskStatus.SUCCEEDED,
                        result={
                            "protocol": "GDB/MI",
                            "final_state": (
                                session.state.state.value
                            ),
                            "closed_by": "client",
                        },
                        exit_code=0,
                        elapsed_ms=elapsed_ms,
                    )

                # --------------------------
                # 情况2：
                # GDB进程先结束。
                # --------------------------
                assert (
                    process_wait_task
                    in done
                )

                # 先取真实退出值，再检查主动关闭标志。
                # 只有非主动退出路径把该值直接作为业务成败依据。
                exit_code = (
                    await process_wait_task
                )
                # 这里非常关键：
                #
                # client CLOSE 会先令
                #
                #     session._closing = True
                #
                # 然后 terminate GDB。
                #
                # 因此GDB可能比dispatcher更早结束，
                # 此时 -15 / -9 属于主动关闭，
                # 不能解释为DEBUG失败。
                # 主动关闭可能先终止底层 GDB，随后才把最后快照回复给客户端。
                # 等消费者完成后才返回业务成功。
                if session.closing:
                    await dispatcher_task

                    return ManagedTaskResult(
                        status=TaskStatus.SUCCEEDED,
                        result={
                            "protocol": "GDB/MI",
                            "final_state": (
                                session.state.state.value
                            ),
                            "closed_by": "client",
                        },
                        exit_code=0,
                        elapsed_ms=elapsed_ms,
                    )
                # --------------------------
                # 情况3：
                # 没有client CLOSE，
                # GDB自己先退出。
                # --------------------------
                # 非主动进程退出时先关闭命令通道，唤醒尚未完成的客户端等待。
                # 进程自行退出后先关投递通道，再等待消费者结束，排队请求不会无故挂起。
                broker.close(
                    GdbTransportClosed(
                        exit_code
                    )
                )
                # 关闭命令通道后等待消费者停止，防止结果返回后仍在该通道执行命令。
                await dispatcher_task
                # 进程自主退出以实际退出码判定任务成功与否；MI 状态作为独立证据。
                succeeded = (
                    exit_code == 0
                )

                # 自主退出的结果保留关闭来源；非零退出码交给平台作为失败证据。
                return ManagedTaskResult(
                    status=(
                        TaskStatus.SUCCEEDED
                        if succeeded
                        else TaskStatus.FAILED
                    ),
                    result={
                        "protocol": "GDB/MI",
                        "final_state": (
                            session.state.state.value
                        ),
                        "closed_by": "process",
                    },
                    exit_code=exit_code,
                    elapsed_ms=elapsed_ms,
                    error=(
                        None
                        if succeeded
                        else (
                            "GDB exited with code "
                            f"{exit_code}"
                        )
                    ),
                )

            # 撤下会话注册后再清理内部任务，使后续 API 不会取得正在销毁的通道。
            finally:
                # 先停止对外路由，再收回等待任务。
                # 无论启动失败还是正常关闭，都不让注册表留下失效引用。
                if registered:
                    self.session_manager.unregister(
                        context.task_id
                    )

                # 这些只是B自己的等待Task，
                # 真正的OS进程由A负责最终terminate。
                # 这里只有分发和退出观察两个本地任务。
                # 它们的取消不是系统进程已经被终止的证明。
                for task in (
                    dispatcher_task,
                    process_wait_task,
                ):
                    if (
                        task is not None
                        and not task.done()
                    ):
                        task.cancel()

                # 收回等待任务时不让清理异常覆盖已产生的任务结论，进程最终释放由平台负责。
                await asyncio.gather(
                    *(
                        task
                        for task in (
                            dispatcher_task,
                            process_wait_task,
                        )
                        if task is not None
                    ),
                    return_exceptions=True,
                )

        # 平台以受管任务持有副本和交互进程，模块不把进程生命周期绑定到单次 HTTP 请求。
        # 平台接管任务元数据、超时和来源工作区；这里提交的执行闭包只拥有本会话资源。
        return await self.task_service.create_managed_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.DEBUG,
            command=logical_command,
            execute=execute,
            metadata={
                **request.metadata,
                "debug_protocol": "GDB/MI",
                "interactive": True,
            },
            timeout_seconds=request.timeout_seconds,
            source_task_id=(
                request.build_task_id
            ),
        )

    # 构造器同时服务任务记录和真实启动，只在调用方提供的 executable 位置上有差别。
    @staticmethod
    # 逐项组装 argv，固定使用 MI 解释器，不在此处拼接 shell 命令。
    def _gdb_command(
        executable: str,
        args: list[str],
    ) -> list[str]:
        return [
            "gdb",
            "--interpreter=mi2",
            executable,
            *args,
        ]
