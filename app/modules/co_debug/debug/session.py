from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from uuid import UUID, uuid4

from app.core.time import utc_now
from app.modules.co_debug.debug.events import (
    DebugSessionEvent,
    DebugSessionEventKind,
    DebugSessionEventPublisher,
)
from app.modules.co_debug.debug.mi_commands import (
    MiCommandBuilder,
)
from app.modules.co_debug.debug.mi_parser import (
    MiParseError,
    parse_mi_line,
)
from app.modules.co_debug.debug.models import (
    DebugBreakpoint,
    DebugSessionState,
    DebugSessionStateModel,
    DebugThread,
    MiCommand,
    MiRecord,
    MiRecordKind,
)
from app.modules.co_debug.debug.transport import (
    GdbTransport,
    GdbTransportClosed,
)

logger = logging.getLogger(__name__)


class GdbMiError(RuntimeError):
    """
    GDB/MI 会话基础异常。
    """


# 超时表示观察者在期限内没有看到信号；它不能证明 GDB 没有执行已经发送的命令。
class GdbMiTimeout(GdbMiError):
    """
    等待 GDB/MI 响应超时。
    """


# 只有结果类的 error 才属于命令拒绝；传输关闭和解析失败另有处理路径。
class GdbMiCommandError(GdbMiError):
    """
    GDB 返回 ^error。
    """

    # 命令错误优先采用 MI 返回的可读提示，同时保留完整记录供上层诊断。
    # 协议拒绝保留原命令和记录供上层判断，用户消息优先取 GDB 给出的可读原因。
    def __init__(
        self,
        command: MiCommand,
        record: MiRecord,
    ) -> None:
        # 异常同时保留命令和原始协议记录。
        # 即使只返回简短错误文案，调试层也不会丢掉具体应答。
        self.command = command
        self.record = record

        # 结构化错误中的 msg 优先用于诊断。
        # 没有可用文本时保留命令上下文，让上层知道是哪次操作未被接受。
        message = None

        if isinstance(
            record.payload,
            dict,
        ):
            value = record.payload.get(
                "msg"
            )

            # GDB 的 msg 若不是文本就采用通用诊断，避免把结构化负载直接当作用户错误消息。
            if isinstance(
                value,
                str,
            ):
                message = value

        super().__init__(
            message
            or (
                "GDB/MI command failed: "
                f"{command.text}"
            )
        )


class GdbMiSession:
    """
    一个独立的 GDB/MI 调试会话。

    主要职责：

        1. 生成带 token 的 MI 命令
        2. 将 token 和 ^result 对应
        3. 处理 GDB 异步事件
        4. 维护 RUNNING / STOPPED / EXITED 状态
        5. 管理断点状态

    不负责：

        1. 创建 subprocess
        2. 管理 Workspace
        3. Task 生命周期
        4. Task 取消
        5. HTTP API

    这些分别属于 Platform 或更上层 Service。
    """

    # 一个会话拥有一条传输及其命令等待表；事件发布需要绑定平台任务身份。
    def __init__(
        self,
        transport: GdbTransport,
        *,
        command_timeout_seconds: float = 10.0,
        session_task_id: UUID | None = None,
        event_publisher: DebugSessionEventPublisher | None = None,
    ) -> None:
        # 默认预算用于协议命令应答，也可作为初始化握手的回退期限。
        if command_timeout_seconds <= 0:
            raise ValueError(
                "command_timeout_seconds "
                "must be greater than 0"
            )

        # 发布给协同服务的事件必须携带平台任务标识。
        # 单独使用协议会话时可以省略发布器，仍能维护断点和线程快照。
        if event_publisher is not None and session_task_id is None:
            raise ValueError("session_task_id is required when event_publisher is configured")

        # 传输由调用方提供，构造本对象不会创建进程或启动读循环。
        self.transport = transport

        self.command_timeout_seconds = (
            command_timeout_seconds
        )
        self.session_task_id = session_task_id
        self.event_publisher = event_publisher

        # 命令编号和状态都归该会话，不与其他 GDB 进程共用。
        self.command_builder = (
            MiCommandBuilder()
        )

        # 会话状态与任务记录分离；它只由拥有 MI reader 的循环更新。
        # 同一个平台任务的生命周期可晚于 inferior 的退出。
        self.state = (
            DebugSessionStateModel()
        )

        # 等待表只收录发出后尚未完成的命令；异步通知不需要登记。
        # 响应对象由唯一的读取任务交付，调用者不直接消费传输队列。
        self._pending: dict[
            int,
            asyncio.Future[MiRecord],
        ] = {}

        # 读取任务和就绪信号分别管理。
        # 有读取任务仅表示开始接收输出，收到首个提示符后才能确认 MI 已可用。
        self._reader_task: (
            asyncio.Task[None]
            | None
        ) = None

        # 握手等待只对应首个提示符；后续提示符不能覆盖先前的握手结果。
        self._ready_future: (
            asyncio.Future[None]
            | None
        ) = None

        # 停止通知使用可以先置位的信号，处理先收到事件、后开始等待的顺序。
        self._stop_event = (
            asyncio.Event()
        )

        # 这三个标记分别表示读循环已启动、主动收尾正在进行、收尾已经完成。
        # 它们不直接等同于被调试程序的运行状态。
        self._started = False
        self._closing=False
        self._closed = False
        # 按协议通道保存输出；后续快照可以分别展示控制台、程序和诊断。
        self._console_output: list[
            str
        ] = []

        # 目标输出可同时来自显式 MI 目标流和不能识别为 MI 的标准输出文本。
        self._target_output: list[
            str
        ] = []

        # MI 日志输出与 OS stderr 各自保留。
        # 前者经 MI 帧解码，后者可能根本不是 MI 语法。
        self._log_output: list[
            str
        ] = []

        # 标准错误是进程层独立来源，和 GDB 通过 MI 日志标记发出的内容分别保存。
        self._stderr_output: list[
            str
        ] = []

    # started 表示读循环已启动的生命周期标记，不承诺目标正在运行。
    @property
    def started(
        self,
    ) -> bool:
        return self._started

    @property
    def closed(
        self,
    ) -> bool:
        return self._closed
    # 关闭进行中与关闭完成分开，reader 可以据此前者识别主动断开。
    # 这些标志不替代 inferior 的运行状态。
    @property
    def closing(
        self,
    ) -> bool:
        return self._closing
    @property
    # 对外访问输出时返回元组快照，调用者不能修改会话维护的列表。
    def console_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._console_output
        )

    @property
    def target_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._target_output
        )

    @property
    def log_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._log_output
        )

    @property
    def stderr_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._stderr_output
        )

    # 启动只初始化会话读循环，实际进程已由传输层提供。
    async def start(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """
        启动 MI reader，并等待：

            (gdb)

        初始 prompt。

        GDB 启动成功后：

            STARTING -> READY
        """

        # 已关闭的会话不支持重新握手；要重新调试应建立新进程和新会话。
        if self._closed:
            raise GdbMiError(
                "GDB/MI session is closed"
            )

        # 同一会话不重复创建 reader；主动关闭后也不能再启动。
        # 重复启动不再创建新的读取者，传输队列由原读循环独占。
        if self._started:
            return

        # 启动标记防止并发启动再占一个 reader；它在完成提示符握手之前就已设置。
        self._started = True

        # 使用当前事件循环创建握手对象，使其与读循环及后续命令 Future 同属一个循环。
        loop = (
            asyncio.get_running_loop()
        )

        # 就绪由首个 MI 提示符确认，进程存在不代表命令已经可以发送。
        # 先创建就绪信号再启动读循环，使首条提示符很快到达时也能通知启动方。
        self._ready_future = (
            loop.create_future()
        )

        # 等待首次提示符之前已安装就绪 Future，reader 可立即处理最早收到的帧。
        # 若先启动 reader 再安装等待器，早到的提示符可能无法唤醒启动者。
        self._reader_task = (
            asyncio.create_task(
                self._reader_loop()
            )
        )

        # 就绪等待可单独限时，缺省时沿用普通命令响应时限。
        # 启动等待可使用与普通命令不同的期限，未传时沿用命令默认值。
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.command_timeout_seconds
        )

        # 仅取消当前超时等待，保留就绪 Future 给读循环正常收尾。
        try:
            # 保护就绪 Future，令外层超时不直接取消 reader 正在使用的对象。
            # 超时路径标记会话失败，进程和 reader 的回收由会话所有者处理。
            await asyncio.wait_for(
                asyncio.shield(
                    self._ready_future
                ),
                timeout=timeout,
            )

        # 提示符超时使会话不可继续；调用者仍负责走关闭流程回收传输。
        # 启动超时表示握手未按期完成，不代表底层进程已经退出。
        # 调用方仍须走会话关闭流程，释放进程和读取任务。
        except TimeoutError as exc:
            self._set_state(
                DebugSessionState.FAILED
            )

            # 保留等待超时的异常链，调用方可区别启动预算耗尽与进程立即关闭。
            raise GdbMiTimeout(
                "timed out waiting for "
                "initial GDB/MI prompt"
            ) from exc

    # 启动参数设置由 GDB 保存，必须收到 done 后才能向调用者确认。
    async def set_arguments(
        self,
        arguments: list[str],
    ) -> None:
        # 目标参数使用独立的 MI 参数设置命令，不需要重新启动调试器或拼接 shell 参数。
        command = (
            self.command_builder
            .exec_arguments(
                arguments
            )
        )

        # 设置参数只提交一次普通 MI 请求。
        # 完成前 GDB 可能拒绝参数，调用者不能以 API 已发送就认定后续运行会采用它们。
        record = await self._execute(
            command
        )

        # 只有 GDB 接受参数后才结束这个调用；目标程序的启动是另一个请求。
        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

    # 先让 GDB 接收运行中的 MI 命令，之后才有独立中断通道。
    # 异步模式允许被调试程序运行期间继续处理 MI 控制请求。
    # 协同中断依赖这条控制路径，不必等目标程序自然停下。
    async def enable_async_execution(self) -> None:
        """Allow MI control commands while the inferior is running."""
        command = self.command_builder.gdb_set_mi_async()
        record = await self._execute(command)
        self._require_success(
            command,
            record,
            allowed_classes={"done"},
        )

    # 采用 all-stop 语义，协同层可以把会话暂停作为统一状态处理。
    # 异步接收命令与线程停止策略是两个设置。
    # 保留全停模式时，断点暂停可作为当前进程内多个线程的检查点。
    async def enforce_all_stop(self) -> None:
        """Explicitly keep the inferior in GDB all-stop mode."""
        command = self.command_builder.gdb_set_all_stop()
        record = await self._execute(command)
        self._require_success(command, record, allowed_classes={"done"})

    # 新增断点从 MI 结果获取实际编号和位置，不能用请求表达式作为标识。
    async def insert_breakpoint(
        self,
        location: str,
        *,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
    ) -> DebugBreakpoint:
        # 断点条件、临时性和启用状态独立传给构造器，实际解析与编号分配由 GDB 完成。
        command = (
            self.command_builder
            .break_insert(
                location,
                temporary=temporary,
                disabled=disabled,
                condition=condition,
            )
        )

        # 断点模型在命令应答通过检查后才建立。
        # 命令未被接受时不先在本地占用一个猜测的断点号。
        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        # 断点登记在 GDB 明确接受命令并提供可解析编号之后。
        # 后续操作及协同规则引用这个编号；请求位置只保留为辅助信息。
        breakpoint = (
            self._breakpoint_from_record(
                location,
                record,
            )
        )

        # 映射使用返回的断点编号；同一个源码位置可以在 GDB 中对应不同断点。
        self.state.breakpoints[
            breakpoint.number
        ] = breakpoint

        return breakpoint

    # 先要求 GDB 确认删除，再移除本地断点快照。
    async def delete_breakpoint(
        self,
        breakpoint_number: str,
    ) -> None:
        command = (
            self.command_builder
            .break_delete(
                breakpoint_number
            )
        )

        record = await self._execute(
            command
        )

        # 移除缓存必须晚于 GDB 的成功确认。
        # 若进程拒绝删除，旧条目仍可用于之后的状态诊断。
        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        # 删除成功后才清理本地快照，命令错误不会让已登记断点从界面消失。
        self.state.breakpoints.pop(
            breakpoint_number,
            None,
        )

    # 启停命令成功后同步已知断点；缓存没有该编号时不合成断点。
    async def enable_breakpoint(
        self,
        breakpoint_number: str,
    ) -> None:
        command = (
            self.command_builder
            .break_enable(
                breakpoint_number
            )
        )

        record = await self._execute(
            command
        )

        # 启用结果确认后，只更新已有缓存条目。
        # 缓存缺项时不由编号反推原来设置的位置。
        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        # 这里只调整已登记的断点快照；命令成功也不会凭编号补造缺少的位置信息。
        breakpoint = (
            self.state.breakpoints.get(
                breakpoint_number
            )
        )

        # 只有此前登记过的断点可就地更新；成功回复并未补充缺失的符号位置信息。
        if breakpoint is not None:
            breakpoint.enabled = True

    # 禁用只改变断点是否生效，原编号和位置仍留在快照中。
    async def disable_breakpoint(
        self,
        breakpoint_number: str,
    ) -> None:
        command = (
            self.command_builder
            .break_disable(
                breakpoint_number
            )
        )

        record = await self._execute(
            command
        )

        # 禁用被拒绝时不能先把界面状态改为失效。
        # 已知断点对象沿用原身份，只在成功时更新标志。
        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        # 先确认 GDB 的更新结果，防止失败的禁用命令让本地视图显示错误状态。
        breakpoint = (
            self.state.breakpoints.get(
                breakpoint_number
            )
        )

        if breakpoint is not None:
            breakpoint.enabled = False

    # 启动成功表示运行命令被接受，实际停住或退出由异步记录确认。
    # 首次启动使用独立 MI 命令；返回允许 done 和 running，后续现场由记录分派更新。
    async def run(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_run()
        )

        record = await self._execute(
            command
        )

        # 允许两种控制结果类。
        # 即使结果类是 done，停止原因也仍须从后续现场记录取得。
        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )

    # 继续执行和等待暂停是独立动作；命令响应不承载下一次暂停原因。
    # 继续针对当前目标恢复执行，不重新创建会话或重新设置参数。
    async def continue_execution(
        self,
    ) -> None:
        # 继续使用当前 GDB 上下文；这里不将旧的停止信号当作继续之后的新结果。
        command = (
            self.command_builder
            .exec_continue()
        )

        record = await self._execute(
            command
        )

        # 继续接受运行态应答，也容许只返回完成应答的实现。
        # 目标是否再次停止由异步通道观察，调用层不能由这次方法返回推断。
        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )


    # 单步类操作发出执行命令后，状态仍由 MI 响应及异步记录推进。
    # 单步请求的成功只确认已发起，调用方可以再等停止后读取栈帧。
    async def next(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_next()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )


    # 步入和步过使用不同 MI 命令，但共享同一套应答与停止事件处理。
    async def step(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_step()
        )

        record = await self._execute(
            command
        )

        # 单步的命令回执和步进完成是两个时刻。
        # 这一检查不尝试合成新的源码帧，保留后续停止通知作为现场来源。
        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )


    # 中断确认可能迟于命令返回，调用方可继续用等待暂停取得状态。
    # 中断命令本身与异步停止通知走不同通道。
    # 需要确认暂停的调用方应再观察会话状态或等待停止信号。
    async def interrupt(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_interrupt()
        )

        # 中断请求送达后仅校验应答类别；未在这条命令里阻塞到全部线程停下。
        record = await self._execute(
            command
        )

        # 中断成功不等于目标已进入停止态。
        # 当前允许的两个结果类都只确认这条命令通过，后续等待另有预算。
        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
                "running",
            },
        )

    # 值按 GDB 返回文本交付，未知结构不转换为任意 Python 对象。
    # 求值结果按 GDB 的文本表示传回；结构异常不尝试在平台侧求值。
    async def evaluate_expression(
        self,
        expression: str,
    ) -> str | None:
        command = (
            self.command_builder
            .data_evaluate_expression(
                expression
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        # 命令成功但缺少文本值时返回空，区分于 GDB 主动拒绝求值。
        if not isinstance(
            record.payload,
            dict,
        ):
            return None

        # 字段按调试器返回的原文本交付，可保留地址、格式化数字或类型名。
        value = record.payload.get(
            "value"
        )

        # 标量原样保留为 GDB 表示的字符串。
        # 诸如十六进制地址或带类型格式的显示值，不在这里转换成 Python 数字。
        return (
            value
            if isinstance(
                value,
                str,
            )
            else None
        )

    # 堆栈项保持上游列表结构，缺省载荷作为空列表处理。
    # 栈帧从当前 GDB 检查上下文取得，不用最近一次停止事件的帧替代整个调用栈。
    async def list_stack_frames(
        self,
    ) -> list[Any]:
        command = (
            self.command_builder
            .stack_list_frames()
        )

        record = await self._execute(
            command
        )

        # 先核实 MI 结果，再处理载荷缺省。
        # 错误响应与一次成功但没有可用帧列表的响应不应得到同一个业务判断。
        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        # 缺少约定的列表负载时不拼造帧，原协议错误在此前已经抛出。
        if not isinstance(
            record.payload,
            dict,
        ):
            return []

        stack = record.payload.get(
            "stack"
        )

        # 栈记录以有序条目返回；不把多个帧折叠为一个键值映射。
        if not isinstance(
            stack,
            list,
        ):
            return []

        # 返回列表保留帧顺序与解析器生成的结构。
        # 会话层不会按源码路径去重，因为递归可以在多个层级有同一位置。
        return stack

    # 局部变量读操作不自行改变线程选择，使用当前 GDB 上下文。
    # 局部变量由 GDB 给出当前帧视图，未提供列表时保留空结果。
    async def list_local_variables(
        self,
    ) -> list[Any]:
        command = (
            self.command_builder
            .stack_list_variables()
        )
        # 局部变量使用当前线程与帧。
        # 选择线程或改变执行状态后不能假设之前的变量列表仍适用。
        record = await self._execute(command)
        self._require_success(
            command,
            record,
            allowed_classes={"done"},
        )
        # 缺少变量列表不会在本地推导值；GDB 的命令拒绝已在此前显式抛出。
        if not isinstance(record.payload, dict):
            return []
        variables = record.payload.get("variables")
        return variables if isinstance(variables, list) else []

    # 线程列表通过显式查询刷新，不只依赖异步通知积累的缓存。
    # 线程查询会刷新完整快照；异步创建和退出通知只在两次查询之间增量维护它。
    async def list_threads(
        self,
    ) -> list[DebugThread]:
        command = self.command_builder.thread_info()
        record = await self._execute(command)
        self._require_success(
            command,
            record,
            allowed_classes={"done"},
        )
        # 刷新成功后返回当前缓存的条目列表；内部映射仍只由此会话维护。
        self._update_threads_from_record(record)
        # 线程缓存仍由会话管理；返回的是本次查询形成的列表容器。
        # 跨循环的公开视图应由分派器转换为普通值。
        return list(self.state.threads.values())

    # 切换前先确认线程存在，成功后刷新选中线程及最新列表。
    async def select_thread(
        self,
        thread_id: str,
    ) -> list[DebugThread]:
        # Refresh first so a stale UI cannot select a thread that has exited.
        # 切换线程前先查询现场，以最近一次 GDB 响应中的线程表约束选择。
        # 目标仍可能在查询后退出，最终以选择命令返回为准。
        await self.list_threads()
        # 刷新后仍找不到的编号在提交选择命令之前拒绝。
        # 这比把过期请求转给另一个看似匹配的线程安全。
        if thread_id not in self.state.threads:
            raise GdbMiError(
                f"thread does not exist in this debug session: {thread_id}"
            )

        # 线程可能在刷新与选择之间退出，实际选择失败会由 MI 应答报告。
        command = self.command_builder.thread_select(thread_id)
        record = await self._execute(command)
        self._require_success(
            command,
            record,
            allowed_classes={"done"},
        )
        # 只在 GDB 接受选择后更新当前线程和帧；最后再查询完整线程表同步现场。
        self.state.current_thread_id = thread_id
        if isinstance(record.payload, dict):
            self._update_current_frame(record.payload.get("frame"))
        # 刷新后的列表可以反映选择过程中发生的线程变化，不把第一份快照当最终事实。
        return await self.list_threads()

    async def wait_for_stop(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> DebugSessionState:
        """
        等待 inferior：

            STOPPED
            EXITED

        或 session：

            FAILED
        """

        # 先读当前状态，避免暂停通知在调用之前已发生时等待下一次事件。
        # 事件可能在调用方开始等待前就已到达，先读状态可直接返回当前快照。
        # 退出或读取失败也会结束等待，返回值并不保证目标处于可单步状态。
        if (
            self.state.state
            in {
                DebugSessionState.STOPPED,
                DebugSessionState.EXITED,
                DebugSessionState.FAILED,
            }
        ):
            return self.state.state

        # 此超时只限制观察暂停，不主动中断被调试程序。
        if timeout_seconds is None:
            await self._stop_event.wait()

        else:
            try:
                # 有界等待只约束这个观察调用，不会向 GDB 发送停止或关闭命令。
                # 超时后目标仍可能在独立 reader 中正常运行或停止。
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=timeout_seconds,
                )

            # 等待停止超时不改变会话状态；后续异步通知仍可更新现场。
            except TimeoutError as exc:
                raise GdbMiTimeout(
                    "timed out waiting "
                    "for GDB to stop"
                ) from exc

        # 这里交付的是唤醒后观察到的状态，调用方仍需区分暂停、退出与故障。
        return self.state.state

    async def close(
        self,
    ) -> None:
        """
        关闭当前 GDB/MI 会话。

        close 是主动的生命周期结束，不应把底层进程
        被 terminate 后产生的非零退出码解释为调试失败。

        OS 级进程清理由 Transport / Platform 负责。
        """

        # 关闭入口幂等，重复调用不能再次终止底层进程。
        if self._closed:
            return

        # 并发关闭调用发现已有执行者时直接返回；此返回不是等待其他调用收尾的承诺。
        if self._closing:
            return

        # 先声明主动关闭，再交由传输层结束进程。
        # 读循环可能在关闭调用返回前就收到 EOF，需要能识别这次退出的意图。
        # 再次关闭在已有收尾过程中不创建第二套清理动作。
        # 需要等完整收尾的调用方应等待第一次关闭者，而不凭重入返回判断进程状态。
        self._closing = True

        # 进程的系统级收尾通过 transport 进行。
        # 会话结束后仍需显式收束自己的 reader。
        try:
            await self.transport.close()

        # 即使关闭传输抛错，也要结束 reader 并标记会话关闭。
        # 即使传输关闭抛错也要收束读取任务，避免它继续占用队列。
        # 既有 FAILED 状态保留，便于上层识别关闭前已经发生的故障。
        finally:
            if (
                self._reader_task
                is not None
                and not self._reader_task.done()
            ):
                self._reader_task.cancel()

            # 等待 reader 真正结束后才标记关闭，取消读取不会留下另一个任务继续处理协议。
            if self._reader_task is not None:
                await asyncio.gather(
                    self._reader_task,
                    return_exceptions=True,
                )

            # transport 和 reader 的收尾结束后才发布已关闭标志。
            # 后续命令会被会话入口拒绝，即使还有旧的快照对象。
            self._closed = True

            # 保留已记录的 FAILED，不能让正常关闭动作抹掉先前故障。
            if (
                self.state.state
                != DebugSessionState.FAILED
            ):
                self._set_state(
                    DebugSessionState.EXITED
                )

    # 命令和响应靠 token 关联，和随时到达的状态通知分开处理。
    async def _execute(
        self,
        command: MiCommand,
    ) -> MiRecord:
        # 先检查会话使用条件，再创建等待项和写协议。
        # 无法接收应答的已关闭会话不应产生待匹配 token。
        self._require_started()

        # 已记录的协议故障会阻断后续写入，即使底层 GDB 进程尚未退出。
        if (
            self.state.state
            == DebugSessionState.FAILED
        ):
            raise GdbMiError(
                "GDB/MI session has failed"
            )

        # 已有退出码说明传输不能再接收命令，不在此创建挂起等待。
        # 状态快照与传输出口可能不同步；写入前额外检查进程是否已有退出码。
        if (
            self.transport.returncode
            is not None
        ):
            raise GdbTransportClosed(
                self.transport.returncode
            )

        # 为这个命令创建只属于会话循环的 Future，网络请求一侧经 broker 取得结果。
        loop = (
            asyncio.get_running_loop()
        )

        # 每条命令单独建立结果槽，异步状态记录无需占用这个槽。
        response_future: (
            asyncio.Future[MiRecord]
        ) = loop.create_future()

        # 一定要先注册 token，
        # 再真正发送命令。
        #
        # 否则极快的 GDB 响应可能先于
        # pending 注册到达。
        # 等待表只关联此会话中一条命令的 token。
        # 异步执行状态仍由 reader 独立消费，不要求 API 在等待另一条命令。
        self._pending[
            command.token
        ] = response_future

        # 注册后再写传输，确保极快到达的 GDB 应答能找到所属 Future。
        try:
            # 发送阶段的异常会清除命令等待项。
            # 即使传输已部分发生，也不能让失去调用者的等待器长期留在表中。
            await self.transport.send(
                command.text
            )

        # 发送阶段失败或被取消后撤销本次等待者，不能留下无响应的 token。
        # 发送过程失败或取消时撤销等待表登记。
        # 尚未交付的响应对象随之取消，避免把未完成请求继续留在读取方。
        except BaseException:
            self._pending.pop(
                command.token,
                None,
            )

            # 如果结果恰在发送失败前送达，不重复取消已完成的 Future。
            if not response_future.done():
                response_future.cancel()

            raise

        # 响应等待与读循环解耦；等待期限不决定读取任务的生命周期。
        try:
            return await asyncio.wait_for(
                asyncio.shield(
                    response_future
                ),
                timeout=(
                    self.command_timeout_seconds
                ),
            )

        # 等待超时只清除本命令配对；迟到的 MI 输出仍可能更新全局状态。
        # 命令超时只解除本次等待，不会撤回已经写入 GDB 的命令。
        # 迟到的结果可能仍推进会话状态，但不会再唤醒原请求。
        except TimeoutError as exc:
            self._pending.pop(
                command.token,
                None,
            )

            # 超时只撤销本地等待，不能撤回 GDB 可能已收到的操作。
            # 迟到的执行状态仍由 reader 正常处理。
            if not response_future.done():
                response_future.cancel()

            # 对外保留具体命令的超时诊断，避免与等待目标暂停的超时混淆。
            raise GdbMiTimeout(
                "timed out waiting for "
                "GDB/MI command response: "
                f"{command.text}"
            ) from exc

    # 单 reader 顺序消费进程输出，让命令配对和状态更新共享同一顺序。
    async def _reader_loop(
        self,
    ) -> None:
        try:
            # reader 保留传输观察到的顺序；不能让另一个任务抢读下一条 token 响应。
            while True:
                message = (
                    await self.transport
                    .receive()
                )

                # stderr 是单独的诊断流，不作为 MI 响应输入。
                # 标准错误直接归档，只有标准输出交给 MI 语法解析。
                # 这避免普通诊断碰巧形似控制记录时改变会话状态。
                if (
                    message.stream
                    == "stderr"
                ):
                    # 标准错误可包含非协议诊断，不交给 MI 解析器。
                    # 单独存放可避免把诊断中的类似提示符当成就绪信号。
                    self._stderr_output.append(
                        message.text
                    )

                    continue

                # 区分未知记录和已识别但无效的记录。
                # 前者可来自程序直接输出，后者不能安全地驱动当前会话。
                try:
                    record = (
                        parse_mi_line(
                            message.text
                        )
                    )

                # 识别为 MI 但载荷损坏时停止读循环，避免后续状态在坏记录上继续推进。
                # 无法解析的 MI 记录和可识别为普通文本的输出区别处理。
                # 前者意味着协议状态无法可靠继续，后者仍作为目标输出保留。
                except MiParseError as exc:
                    raise GdbMiError(
                        "failed to parse "
                        "GDB/MI output: "
                        f"{message.text}"
                    ) from exc
                
                # 无 MI 标记的文本仍对用户可见，但不更改调试状态。
                if (
                    record.kind
                    == MiRecordKind.UNKNOWN
                ):
                    self._target_output.append(
                        record.raw
                    )

                    continue

                # 识别出的记录由同一分派点推进握手、命令应答和状态，避免并发写现场。
                self._handle_record(
                    record
                )

        # 读循环被外层取消时沿用 asyncio 取消语义，不把主动收尾当协议故障。
        except asyncio.CancelledError:
            raise
        # 传输关闭有独立分支，以退出码和主动关闭标记区分不同结束方式。
        # 无论属于哪一类，都还要通知尚在等待协议回应的调用方。
        except GdbTransportClosed as exc:
            # 如果是 session.close() 主动结束 Transport，
            # 即使底层 terminate 导致 -15 / -9，
            # 也属于正常生命周期结束，而不是 GDB 故障。
            if self._closing:
                self._set_state(
                    DebugSessionState.EXITED
                )

            # 非主动关闭时依据底层退出码分类；无退出码或零退出走退出状态。
            elif (
                exc.returncode
                in (
                    None,
                    0,
                )
            ):
                self._set_state(
                    DebugSessionState.EXITED
                )

            else:
                self._set_state(
                    DebugSessionState.FAILED
                )

            # 传输已退出时唤醒所有等候者，不让它们逐个等到命令超时。
            # 流已结束，不可能再收到命令结果或初始提示符。
            # 把底层关闭原因交给所有等待方，无需逐条等各自的超时。
            self._fail_waiters(
                exc
            )

        # 其他读循环错误意味着后续响应不再可靠；标记失败后同步结束等待者。
        except BaseException as exc:
            self._set_state(
                DebugSessionState.FAILED
            )

            self._fail_waiters(
                exc
            )

    def _handle_record(
        self,
        record: MiRecord,
    ) -> None:
        # 提示符只用于从 STARTING 进入 READY；运行中出现提示符不会把现场改回就绪。
        if (
            record.kind
            == MiRecordKind.PROMPT
        ):
            # 只有初次握手可以由提示符推进到 READY。
            # 之后的提示符不是清除停止原因或重置目标状态的命令。
            if (
                self.state.state
                == DebugSessionState.STARTING
            ):
                self._set_state(
                    DebugSessionState.READY
                )

            # 握手等待只完成一次。
            # 后来的提示符可被正常消费，不重新初始化已在运行的会话。
            if (
                self._ready_future
                is not None
                and not self._ready_future.done()
            ):
                self._ready_future.set_result(
                    None
                )

            return

        # 输出流记录只追加文本，不改变命令等待表或执行状态。
        if (
            record.kind
            == MiRecordKind.CONSOLE_STREAM
        ):
            # 只收录解析后的字符串；控制台流负载不会写入表达式结果。
            if isinstance(
                record.payload,
                str,
            ):
                # 输出缓存不承担命令应答关联；包含普通文本的帧不会释放待命令 Future。
                self._console_output.append(
                    record.payload
                )

            return

        # 目标流与 stdout 上的普通目标文本最终进同一个输出容器。
        if (
            record.kind
            == MiRecordKind.TARGET_STREAM
        ):
            if isinstance(
                record.payload,
                str,
            ):
                self._target_output.append(
                    record.payload
                )

            return

        # 调试器自身的 MI 日志和进程 stderr 分别归档，便于追查故障来源。
        if (
            record.kind
            == MiRecordKind.LOG_STREAM
        ):
            if isinstance(
                record.payload,
                str,
            ):
                self._log_output.append(
                    record.payload
                )

            return

        # 带编号的结果先按 token 交付给等待方；未知或过期 token 不会绑定到其他请求。
        # 运行状态的更新独立进行，使没有等待者的运行结果也能反映到快照。
        if (
            record.kind
            == MiRecordKind.RESULT
            and record.token is not None
        ):
            # 结果 token 用于交付单个等待者，和下面的会话状态处理互相独立。
            response_future = (
                self._pending.pop(
                    record.token,
                    None,
                )
            )

            # 超时或取消后的应答仍可能到达，不为它重新建立结果槽。
            if (
                response_future
                is not None
                and not response_future.done()
            ):
                # 取出等待项后只完成尚未结束的 Future。
                # 如果调用方已经超时，仍允许本条记录携带的运行状态影响会话。
                response_future.set_result(
                    record
                )

            # 运行标记可独立于命令等待者生效，实际会话状态不会因 API 超时而忽略它。
            if record.is_running:
                self._set_state(
                    DebugSessionState.RUNNING
                )

            return

        # 执行异步记录描述目标的现场变化，可以先于业务调用开始等待。
        # 暂停原因、线程和栈帧由停止记录一起采集，不从命令成功响应推断。
        if (
            record.kind
            == MiRecordKind.EXEC_ASYNC
        ):
            # 带 token 的回执和执行异步通知都可能报告开始运行。
            # 异步通知没有等待者也可以让之前的停止事件失效。
            if record.is_running:
                self._set_state(
                    DebugSessionState.RUNNING
                )

                return

            # 停止记录可能同时携带线程、原因与帧；交由统一处理器提交现场。
            if record.is_stopped:
                self._handle_stopped_record(
                    record
                )

            return

        # 线程通知只更新辅助线程信息；停止原因由执行异步记录设置。
        if (
            record.kind
            == MiRecordKind.NOTIFY_ASYNC
        ):
            # 通知类记录交给线程现场处理。
            # 无法识别的类别不自动改变 inferior 的运行状态。
            self._handle_notify_record(record)

    # 暂停记录同时更新原因、线程和栈帧，供后续状态查询形成一致快照。
    def _handle_stopped_record(
        self,
        record: MiRecord,
    ) -> None:
        # 缺少字典负载时按无详情的停止记录处理，不猜测线程或源码位置。
        payload = (
            record.payload
            if isinstance(
                record.payload,
                dict,
            )
            else {}
        )

        # 停止原因取自协议字段，非文本原因不参与退出或断点判断。
        # 这避免用缺失字段生成虚假的命中事件。
        reason = payload.get(
            "reason"
        )

        # 当前停止原因用这一次记录覆盖；格式不符时不继续沿用上一次原因。
        self.state.stop_reason = (
            reason
            if isinstance(
                reason,
                str,
            )
            else None
        )

        # 优先用停止事件中的线程定位当前检查上下文。
        # 即使还没有收到对应创建通知，也要能给这次暂停记录线程身份。
        stopped_thread_id = payload.get("thread-id")
        if isinstance(stopped_thread_id, str) and stopped_thread_id:
            self.state.stopped_thread_id = stopped_thread_id
            self.state.current_thread_id = stopped_thread_id
            self.state.threads.setdefault(
                stopped_thread_id,
                DebugThread(thread_id=stopped_thread_id),
            )

        # 全停模式更新所有已知线程；只给出线程 ID 时仅更新该线程。
        # 全停记录更新已知的所有线程；其他记录只标记报告的那个线程。
        # 会话停止与线程逐个状态仍需区分。
        stopped_threads = payload.get("stopped-threads")
        # 全停标记可更新已知线程集合。
        # 具体停止线程仍单独保留，供当前上下文与命中来源定位。
        if stopped_threads == "all":
            for thread in self.state.threads.values():
                thread.state = "stopped"
        elif isinstance(stopped_thread_id, str):
            self.state.threads[stopped_thread_id].state = "stopped"

        # 只有断点暂停才设置命中线程；信号、步进或其他暂停不应沿用该标记。
        self.state.breakpoint_hit_thread_id = (
            stopped_thread_id
            if reason == "breakpoint-hit" and isinstance(stopped_thread_id, str)
            else None
        )

        # 暂停时的栈帧用于定位源文件、行和函数。
        # 若 GDB 没返回帧则清空旧位置，避免前端把上一次暂停地点当成当前地点。
        frame = payload.get(
            "frame"
        )

        if isinstance(
            frame,
            dict,
        ):
            # 完整路径优先于短文件名，便于区分同名源码；没有定位信息时返回缺失值。
            file_value = (
                frame.get("fullname")
                or frame.get("file")
            )

            # 源码文件优先采用完整路径，同时容许只有文件名的帧。
            # 这些值来自 GDB，并不等同于平台已验证可下载的路径。
            self.state.current_file = (
                file_value
                if isinstance(
                    file_value,
                    str,
                )
                else None
            )

            function = frame.get(
                "func"
            )

            self.state.current_function = (
                function
                if isinstance(
                    function,
                    str,
                )
                else None
            )

            self.state.current_line = (
                self._to_int(
                    frame.get(
                        "line"
                    )
                )
            )

        # 新的停止记录未带栈帧时清空此前的现场。
        # 否则界面可能显示上一断点的位置并误认为是本次停止位置。
        else:
            self.state.current_file = None
            self.state.current_function = None
            self.state.current_line = None

        # 退出原因和普通暂停使用同一 MI 记录形态，必须按 reason 分辨。
        # 退出也通过异步 stopped 记录送达，但不能当成可继续执行的暂停。
        # 清掉暂停和断点线程标记后，上层可按退出状态收束会话。
        if (
            isinstance(
                reason,
                str,
            )
            and reason.startswith(
                "exited"
            )
        ):
            # 退出类停止让等待者结束，但不能把最后一个线程当作仍可操作的暂停线程。
            self._set_state(
                DebugSessionState.EXITED
            )
            self.state.stopped_thread_id = None
            self.state.breakpoint_hit_thread_id = None

        # 常规暂停保持会话活跃，允许后续检查上下文或继续运行。
        else:
            self._set_state(
                DebugSessionState.STOPPED
            )

        # 协同层只接收有明确任务及断点编号的命中事件。
        # 先更新本地暂停状态，再通知外部订阅者。
        # 只有真正的断点命中且拿到编号才触发协同事件。
        # 发布前本地状态已更新，消费者读到的是该次停止后的现场。
        if reason == "breakpoint-hit" and self.session_task_id is not None:
            breakpoint_number = payload.get("bkptno")
            # 事件需要 GDB 提供非空断点编号，前端请求时的断点位置不能作为替代。
            if isinstance(breakpoint_number, str) and breakpoint_number:
                # 事件在当前停止状态已经写入之后发布。
                # 组协调方收到的是该断点命中的事实，不需要自行解析 MI 文本。
                self._publish_event(
                    DebugSessionEvent(
                        event_id=uuid4(),
                        session_task_id=self.session_task_id,
                        kind=DebugSessionEventKind.BREAKPOINT_HIT,
                        occurred_at=utc_now(),
                        breakpoint_number=breakpoint_number,
                    )
                )

    # 线程生命周期由异步通知增量维护，退出时清理指向该线程的当前状态。
    def _handle_notify_record(
        self,
        record: MiRecord,
    ) -> None:
        payload = record.payload if isinstance(record.payload, dict) else {}
        thread_id = payload.get("id")
        # 只有带有效线程标识的通知可更新缓存，其他通知不会删除现有现场。
        if not isinstance(thread_id, str) or not thread_id:
            return

        # 创建事件只增加一个最小线程条目；已通过显式查询收录的详情不被覆盖。
        if record.message_class == "thread-created":
            self.state.threads.setdefault(
                thread_id,
                DebugThread(thread_id=thread_id, state="running"),
            )
        # 退出线程时同时解除三个可能指向它的引用。
        # 否则选择线程、停止线程和断点归属会指向已不存在的对象。
        elif record.message_class == "thread-exited":
            # 退出通知移除线程并清理关联角色。
            # 已经离开的线程不应继续出现在选中或断点命中指示中。
            self.state.threads.pop(thread_id, None)
            if self.state.current_thread_id == thread_id:
                self.state.current_thread_id = None
            if self.state.stopped_thread_id == thread_id:
                self.state.stopped_thread_id = None
            if self.state.breakpoint_hit_thread_id == thread_id:
                self.state.breakpoint_hit_thread_id = None
        # 选中通知只更新焦点；它不代表该线程触发了停止或命中断点。
        elif record.message_class == "thread-selected":
            self.state.current_thread_id = thread_id

    # 显式查询是最新线程集合，重建缓存并修正已经无效的选择。
    def _update_threads_from_record(
        self,
        record: MiRecord,
    ) -> None:
        payload = record.payload if isinstance(record.payload, dict) else {}
        raw_threads = payload.get("threads")
        # 先建立刷新表再替换旧映射，避免沿用未在新响应出现的线程详情。
        refreshed: dict[str, DebugThread] = {}
        # GDB 线程列表既可能直接给条目，也可能在外层包一个 thread 键。
        # 只接收能提供有效 GDB 线程 ID 的条目，系统线程号为可选补充。
        if isinstance(raw_threads, list):
            # 刷新接受直接的线程对象，也兼容命名列表项的包装。
            # 无有效编号的项不适合后续通过 GDB 选择，因而不加入新表。
            for item in raw_threads:
                data = item.get("thread") if isinstance(item, dict) and "thread" in item else item
                if not isinstance(data, dict):
                    continue
                thread_id = data.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                target_id = data.get("target-id")
                # 只有线程 ID 必需，名称、状态和核心等诊断字段不齐时仍保留线程。
                refreshed[thread_id] = DebugThread(
                    thread_id=thread_id,
                    linux_tid=self._linux_tid(target_id),
                    name=data.get("name") if isinstance(data.get("name"), str) else None,
                    state=data.get("state") if isinstance(data.get("state"), str) else "unknown",
                    core=self._to_int(data.get("core")),
                )

        # 本次线程查询替换旧表，以反映漏掉的创建或退出通知。
        # 没有新的当前线程字段时，仅在旧线程仍存在的情况下保留选择。
        self.state.threads = refreshed
        # 显式当前线程字段优先于本地缓存；没有时保留仍可在新表定位的旧选择。
        current = payload.get("current-thread-id")
        # 协议明确返回当前线程时采用报告值；缺省时仅在旧线程仍存在的情况下保留。
        # 它与最近停止线程不是同一字段。
        if isinstance(current, str) and current:
            self.state.current_thread_id = current
        elif self.state.current_thread_id not in refreshed:
            self.state.current_thread_id = None

    @staticmethod
    # LWP 编号是可选系统线程标识，不能用 GDB 线程编号冒充。
    # LWP 是 Linux 目标输出中的系统线程号，不能用 GDB 线程编号代替。
    # 未识别的平台格式保留为空，线程仍可按 GDB ID 操作。
    def _linux_tid(target_id: Any) -> int | None:
        if not isinstance(target_id, str):
            return None
        match = re.search(r"\bLWP\s+(\d+)\b", target_id, re.IGNORECASE)
        return int(match.group(1)) if match else None

    # 帧位置优先使用完整文件名，缺省时兼容较短的相对文件名。
    def _update_current_frame(self, frame: Any) -> None:
        # 切换线程应答若没有帧，本方法不覆盖旧位置；停止记录的无帧分支另有清理规则。
        if not isinstance(frame, dict):
            return
        # 线程选择的应答如带帧就更新当前源码位置。
        # 不写最近停止或断点命中标志，避免把选中操作误当成新的断点事件。
        file_value = frame.get("fullname") or frame.get("file")
        self.state.current_file = file_value if isinstance(file_value, str) else None
        function = frame.get("func")
        self.state.current_function = function if isinstance(function, str) else None
        self.state.current_line = self._to_int(frame.get("line"))

    def _publish_event(self, event: DebugSessionEvent) -> None:
        # 未配置事件接收端时仍正常更新调试现场，不让可选协同依赖成为普通会话前提。
        publisher = self.event_publisher
        if publisher is None:
            return
        try:
            publisher.publish(event)
        # 发布失败不影响已处理的 MI 记录，消费层仍可查询当前现场。
        except Exception:
            # Debug event delivery is optional and must never break the MI reader.
            logger.exception("failed to publish GDB/MI debug event")

    # 重新运行时清空上一轮暂停信号，暂停、退出和失败都应唤醒等待者。
    def _set_state(
        self,
        state: DebugSessionState,
    ) -> None:
        # 先设置状态再唤醒等待者，让观察者能区分终止、故障和可继续的暂停。
        self.state.state = state

        # 恢复运行时撤销上一次暂停的线程归属，并将已知线程标为运行。
        # 等待信号同步复位，使下一次等待对应新的停止周期。
        if state == (
            DebugSessionState.RUNNING
        ):
            # 进入运行态后，上一轮的停止信号不能继续满足新的等待请求。
            # 已知线程的暂停标志同时撤销，直到下一条 MI 更新。
            self._stop_event.clear()
            self.state.stopped_thread_id = None
            self.state.breakpoint_hit_thread_id = None
            for thread in self.state.threads.values():
                thread.state = "running"

        # 停止信号表示无需继续等待；失败与退出同样唤醒调用方。
        # 最终分支应查看返回的状态，不能只以事件触发作为暂停证据。
        elif state in {
            DebugSessionState.STOPPED,
            DebugSessionState.EXITED,
            DebugSessionState.FAILED,
        }:
            self._stop_event.set()

    # 读循环终止后用同一错误结束就绪等待和全部命令等待。
    # 先清空索引，再通知等待者，避免结束回调仍看到已废弃的 token。
    def _fail_waiters(
        self,
        error: BaseException,
    ) -> None:
        # 握手未完成时使用读循环的真实异常结束等待，而不是等待固定启动超时。
        if (
            self._ready_future
            is not None
            and not self._ready_future.done()
        ):
            # 握手等待与命令等待分开失败。
            # 即使首次提示符尚未到来，启动者也无需一直等到自己的超时。
            self._ready_future.set_exception(
                error
            )

        # 先移除整张等待表，再逐个传播错误。
        # 后续处理迟到记录时就不会再找到属于故障会话的请求。
        pending = list(
            self._pending.values()
        )

        self._pending.clear()

        # 只结束尚未完成的请求，不替换已交给调用者的成功应答。
        for future in pending:
            if not future.done():
                future.set_exception(
                    error
                )

    def _require_started(
        self,
    ) -> None:
        # 未经启动的会话没有读取者，发送命令只会留下无法完成的等待。
        if not self._started:
            raise GdbMiError(
                "GDB/MI session "
                "has not been started"
            )

        # 关闭完成后禁止重新使用原会话发送命令，即使引用仍被外层持有。
        if self._closed:
            raise GdbMiError(
                "GDB/MI session is closed"
            )

    @staticmethod
    # MI 错误和不符合该命令预期的成功类别分别报告，避免误接收状态。
    def _require_success(
        command: MiCommand,
        record: MiRecord,
        *,
        allowed_classes: set[str],
    ) -> None:
        # 显式 MI 错误与不合约的应答分开处理。
        # 每个调用提供允许的结果类，避免对所有命令使用同一成功列表。
        if record.is_error:
            raise GdbMiCommandError(
                command,
                record,
            )

        # 即使不是显式 error，也必须是当前命令允许的结果记录才能算成功。
        if (
            record.kind
            != MiRecordKind.RESULT
            or record.message_class
            not in allowed_classes
        ):
            # 返回类别不满足当前操作约定时保留原记录供诊断。
            # 不能把任意带相同 token 的记录都解释成这个操作成功。
            raise GdbMiError(
                "unexpected GDB/MI "
                "response for "
                f"{command.text}: "
                f"{record.raw}"
            )

    @staticmethod
    # 按 MI 的断点对象生成快照，字段缺失时走协议错误而非猜测编号。
    def _breakpoint_from_record(
        location: str,
        record: MiRecord,
    ) -> DebugBreakpoint:
        # 只有包含断点对象和可引用编号的记录才能登记成功。
        # 断点响应缺失符号信息则不影响已有编号的身份。
        payload = (
            record.payload
            if isinstance(
                record.payload,
                dict,
            )
            else {}
        )

        data = payload.get(
            "bkpt"
        )

        # 成功类别不保证负载可用；缺少断点对象时不能登记无法操作的断点。
        if not isinstance(
            data,
            dict,
        ):
            raise GdbMiError(
                "GDB breakpoint response "
                "does not contain bkpt"
            )

        # 断点编号保留协议字符串，可表达带位置后缀的编号形式。
        # 本地不强制转换为数字，也不以源码行号代替。
        number = data.get(
            "number"
        )

        # 断点编号必须来自 GDB 文本字段，不能按列表位置或当前断点数量猜测。
        if not isinstance(
            number,
            str,
        ):
            raise GdbMiError(
                "GDB breakpoint response "
                "does not contain "
                "a breakpoint number"
            )

        # enabled 缺省时沿用启用；源信息从响应抽取，原请求位置单独保留。
        enabled = (
            data.get(
                "enabled",
                "y",
            )
            != "n"
        )

        # 位置标签与解析出的真实文件、行号和函数独立保留。
        # 缺少符号字段不影响按编号管理该断点。
        return DebugBreakpoint(
            number=number,
            location=location,
            enabled=enabled,
            file=(
                data.get("file")
                if isinstance(
                    data.get("file"),
                    str,
                )
                else None
            ),
            fullname=(
                data.get("fullname")
                if isinstance(
                    data.get("fullname"),
                    str,
                )
                else None
            ),
            line=GdbMiSession._to_int(
                data.get("line")
            ),
            function=(
                data.get("func")
                if isinstance(
                    data.get("func"),
                    str,
                )
                else None
            ),
        )

    @staticmethod
    def _to_int(
        value: Any,
    ) -> int | None:
        # 数字字段可以由 MI 文本或整数给出；不能转换时不强加默认行号或核心号。
        try:
            return int(value)

        except (
            TypeError,
            ValueError,
        ):
            return None
