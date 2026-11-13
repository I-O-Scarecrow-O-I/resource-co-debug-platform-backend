from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

from app.modules.co_debug.debug.broker import (
    DebugCommandBroker,
    DebugCommandBrokerClosed,
    DebugCommandKind,
    DebugCommandRequest,
)
from app.modules.co_debug.debug.models import (
    DebugBreakpoint,
)
from app.modules.co_debug.debug.session import (
    GdbMiSession,
)


# 该异常描述活跃会话目录的查询失败。
# 持久化任务记录仍可能存在，并能用于查询已退出会话的任务终态。
class DebugSessionNotFound(
    KeyError
):
    pass


# 重复注册是任务 ID 与命令通道的所有权冲突。
# 调用方应保留现有通道，不能把它的排队请求转给另一个会话。
class DebugSessionAlreadyExists(
    RuntimeError
):
    pass


class GdbMiCommandDispatcher:
    """
    运行在 Managed Task EventLoop 中。

    它是 Broker 与 GdbMiSession 之间的业务适配层。
    """

    # 同一会话串行消费命令，使状态操作遵循队列接收顺序。
    async def run(
        self,
        *,
        broker: DebugCommandBroker,
        session: GdbMiSession,
    ) -> None:
        """
        持续消费 API 侧发来的调试命令。

        CLOSE命令处理后正常退出。
        Broker被关闭也正常退出。
        """

        # 同一会话的命令由这个消费者串行执行。
        # HTTP 侧可并发提交，不能直接跨线程操作 GDB 状态。
        while True:
            try:
                # 等待队列与执行请求是两个不同的异常边界。
                # 接收阶段的关闭表示不再有下一条命令；执行阶段的异常则要回给已领取的请求。
                request = (
                    await broker.receive()
                )

            # 通道关闭代表分发循环完成，不需要把终止标记发给 GDB。
            # Broker 关闭是退出信号，不要求额外投递一次 CLOSE。
            except DebugCommandBrokerClosed:
                return

            # 业务处理范围从成功取出请求开始。
            # 响应 Future 对应这一条请求；前一个请求的异常不会作为下一条的结果。
            try:
                # 分派返回值的第二项只控制消费循环，不能把业务结果里的状态当作关闭信号。
                # 例如 inferior 退出后，仍可能需要查询最终快照。
                result, should_close = (
                    await self._dispatch(
                        session,
                        request,
                    )
                )

            # 单条命令异常交还对应等待者，仍可继续服务后续队列请求。
            # 单次命令的错误返回给对应请求，不因业务拒绝结束整个消费者。
            except BaseException as exc:
                # 单次命令被 GDB 拒绝时，保留错误类型给发起方。
                # 消费循环继续工作，以便后续查看现场或关闭会话。
                broker.reject(
                    request,
                    exc,
                )

                continue

            # 先把本次结果交给 API 等待者，再按命令决定是否停止分发。
            # 先交付最后一次命令的结果再退出，便于关闭路径收到现场快照。
            broker.resolve(
                request,
                result,
            )

            # 仅成功分派并返回关闭标记时走这条正常退出路径。
            # 关闭操作抛异常时，由上面的错误回复或外层会话清理处理。
            if should_close:
                # 先回执再结束消费，关闭发起方仍能收到 transport 收尾后的最后状态。
                return

    # 将允许的操作翻译成会话方法，返回普通结果及是否结束分发的标记。
    async def _dispatch(
        self,
        session: GdbMiSession,
        request: DebugCommandRequest,
    ) -> tuple[Any, bool]:
        # 命令操作来自约定的枚举，参数来自 Broker 的请求封装。
        # 分派器不接受可直接执行的任意 MI 行。
        operation = (
            request.operation
        )

        arguments = (
            request.arguments
        )

        # 只接受内部命令枚举和已组装的参数；分派在会话的工作循环执行。
        if (
            operation
            == DebugCommandKind.SET_ARGUMENTS
        ):
            # 参数列表保留原顺序。
            # 由命令构造器负责 MI 引号，分发层不把列表拼成 shell 文本。
            await session.set_arguments(
                list(
                    arguments.get(
                        "arguments",
                        [],
                    )
                )
            )

            # 设置参数不构造新的进程或新任务，成功回执也没有需要跨线程共享的会话对象。
            return None, False

        # 新增断点的响应从会话对象提取，不直接返回内部可变状态。
        if (
            operation
            == DebugCommandKind.INSERT_BREAKPOINT
        ):
            # 插入成功时复制断点数据，调用方据实际 GDB 编号执行后续操作。
            breakpoint = (
                await session
                .insert_breakpoint(
                    arguments["location"],
                    temporary=bool(
                        arguments.get(
                            "temporary",
                            False,
                        )
                    ),
                    disabled=bool(
                        arguments.get(
                            "disabled",
                            False,
                        )
                    ),
                    condition=arguments.get(
                        "condition"
                    ),
                )
            )

            # 对外复制的是 GDB 返回的实际断点对象。
            # 调用方传入的位置可保留为标签，不能代替 GDB 分配的编号。
            return (
                self._breakpoint_snapshot(
                    breakpoint
                ),
                False,
            )

        # 删除根据已经存在的会话内编号定位。
        # 成功响应不带新的断点列表；需要完整列表时再读取状态。
        if (
            operation
            == DebugCommandKind.DELETE_BREAKPOINT
        ):
            # 只把当前会话的断点编号交给 GDB，删除后由会话对象更新其本地索引。
            # 这里不在管理器另存一份可失步的断点表。
            await session.delete_breakpoint(
                arguments[
                    "breakpoint_number"
                ]
            )

            return None, False

        # 启用和禁用共用断点编号，但不创建新断点。
        # 通过 session 更新本地映射，可供后续状态查询使用。
        if (
            operation
            == DebugCommandKind.ENABLE_BREAKPOINT
        ):
            # 启用使用原编号，不重新插入同位置的新断点。
            # 调用方之后读状态时仍可用此前的编号定位。
            await session.enable_breakpoint(
                arguments[
                    "breakpoint_number"
                ]
            )

            return None, False

        # 禁用仍保留断点位置和编号，因此后续启用无需再次解析源位置。
        if (
            operation
            == DebugCommandKind.DISABLE_BREAKPOINT
        ):
            # 禁用仅改变该断点的激活状态。
            # 已存的断点身份仍可用于启用或删除，不应从组规则中按删除解释。
            await session.disable_breakpoint(
                arguments[
                    "breakpoint_number"
                ]
            )

            return None, False

        # 运行类命令返回的是应答后的现场，目标未必已经到断点。
        # 读取任务单独处理稍后到达的停止通知。
        if (
            operation
            == DebugCommandKind.RUN
        ):
            await session.run()

            # 执行命令的回执在目标停下之前也可能到达。
            # 返回快照可以呈现仍在运行的会话，不以此阻塞等待现场。
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 继续命令沿用当前被调试进程。
        # 其返回快照与重新运行共用形状，但不承诺清除已有断点或重新装载程序。
        if (
            operation
            == DebugCommandKind.CONTINUE
        ):
            await (
                session
                .continue_execution()
            )

            # 继续的响应使用会话当时的状态，不复用继续前的暂停位置作为结果。
            # 需要新停止原因的调用方应再等待停止通知。
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 单步执行和步骤之后的暂停观察是两次独立操作。
        # 在返回的快照仍显示运行时，不能把旧栈帧当作新位置。
        if (
            operation
            == DebugCommandKind.NEXT
        ):
            await session.next()

            # 单步的语义由会话选择的 MI 操作决定。
            # 分派器只报告操作返回时所能观察到的状态，未补造源位置。
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 进入被调用函数的语义由 GDB 命令决定。
        # 调度层只交付该次应答后能观察到的状态。
        if (
            operation
            == DebugCommandKind.STEP
        ):
            await session.step()

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 中断命令可以先返回；需要暂停证据的调用方再等待停止事件。
        if (
            operation
            == DebugCommandKind.INTERRUPT
        ):
            await session.interrupt()

            # 中断后取状态快照并立即回执。
            # 它只报告当时可见信息，无法保证所有异步停止记录已消费。
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 评估请求只映射结果字段，外层无需知道 MI 的键值协议。
        # 表达式在 GDB 上下文求值，分派器仅封装结果。
        if (
            operation
            == DebugCommandKind.EVALUATE
        ):
            value = (
                await session
                .evaluate_expression(
                    arguments[
                        "expression"
                    ]
                )
            )

            # 求值数据在跨线程前包装成独立结果。
            # GDB 不返回文本值时，可保留 None，不能补成空字符串代表真实求值。
            return {
                "value": value,
            }, False

        # 栈查询在目标会话中读取当前上下文；结果与此前选择的线程相关。
        # 分发层不会为了查栈自行切换线程。
        if (
            operation
            == DebugCommandKind.STACK_FRAMES
        ):
            frames = (
                await session
                .list_stack_frames()
            )

            # 堆栈列表由会话查询获得，原始帧结构在此保持。
            # HTTP 层可按公开契约包装，不在管理器重新推导源码行。
            return {
                "frames": frames,
            }, False

        # 查询类请求返回当前会话快照，不迁移平台任务状态。
        # 局部变量和栈帧保留 MI 数据形状；线程结果另有稳定字段投影。
        if operation == DebugCommandKind.LOCAL_VARIABLES:
            variables = await session.list_local_variables()
            return {"variables": variables}, False

        # 线程列表先主动刷新再生成快照。
        # 这与只读缓存的 STATE 请求不同，可能收到 GDB 查询错误。
        if operation == DebugCommandKind.THREADS:
            await session.list_threads()
            # 先刷新完整线程缓存，再计算选中与命中标志。
            # 不能只读此前异步通知留下的部分线程表。
            return self._threads_snapshot(session), False

        # 切换线程之后返回现场快照，调用方不必自行移动当前线程标记。
        if operation == DebugCommandKind.SELECT_THREAD:
            await session.select_thread(arguments["thread_id"])
            # 线程选择可能令当前源码帧与最近断点现场不同。
            # 因此返回新的线程快照，并保留各角色的单独标记。
            return self._threads_snapshot(session), False

        # 读取状态使用会话内存快照，无需另外发送一条 MI 命令。
        # 状态查询只读取内存中的现场，不额外发出会改变调试上下文的命令。
        if (
            operation
            == DebugCommandKind.STATE
        ):
            # 状态查询与会话命令走同一消费循环。
            # 读的是该循环上的快照，API 线程不直接读取正在变化的可变状态。
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 暂停观察超时与 API 队列请求超时不同，由各自等待层独立执行。
        # 等待停止会占用当前会话的命令消费位置。
        # MI 读取任务仍独立运行，可以交付唤醒等待的事件。
        if (
            operation
            == DebugCommandKind.WAIT_FOR_STOP
        ):
            await session.wait_for_stop(
                timeout_seconds=(
                    arguments.get(
                        "stop_timeout_seconds"
                    )
                )
            )

            # 等待只负责观测停止状态，不发出中断。
            # 若返回的是退出或失败，调用方同样可从状态字段区分。
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        # 关闭等待会话回收传输，再交付最终快照并退出分发器。
        # 关闭分支通知消费者在回复之后正常退出，返回状态来自关闭后的会话。
        if (
            operation
            == DebugCommandKind.CLOSE
        ):
            # 只有会话完成关闭后才生成最终响应。
            # 这使等待者能观察结束状态，而不是得到一个仍可发送命令的会话句柄。
            await session.close()

            # 关闭传输层后才提取快照。
            # 此路径结束的是命令分派，不在管理器内写入平台任务的最终状态。
            return (
                self._state_snapshot(
                    session
                ),
                True,
            )

        # 未知操作不降级成任意原始 GDB 命令。
        raise ValueError(
            "unsupported debug command: "
            f"{operation}"
        )

    @staticmethod
    def _breakpoint_snapshot(
        breakpoint: DebugBreakpoint,
    ) -> dict[str, Any]:
        # 断点快照保留可能缺失的文件或行号；不在此用请求位置猜测符号解析结果。
        return {
            "number": breakpoint.number,
            "location": breakpoint.location,
            "enabled": breakpoint.enabled,
            "file": breakpoint.file,
            "fullname": breakpoint.fullname,
            "line": breakpoint.line,
            "function": breakpoint.function,
        }

    @classmethod
    # 状态快照转换为可跨线程传递的数据，避免 API 直接读写会话。
    def _state_snapshot(
        cls,
        session: GdbMiSession,
    ) -> dict[str, Any]:
        # 状态、现场、断点和四种输出一起构造为独立容器。
        # 不要把工作循环仍会修改的会话模型交给 HTTP 线程。
        # 选中、暂停和命中断点三个线程标记分别保留。
        state = session.state

        # 输出包含当前现场、断点缓存和各输出通道快照。
        # 这些是同一会话当前可见数据，不是持久任务记录的内容。
        return {
            "state": state.state.value,
            "stop_reason": (
                state.stop_reason
            ),
            "current_file": (
                state.current_file
            ),
            "current_line": (
                state.current_line
            ),
            "current_function": (
                state.current_function
            ),
            "current_thread_id": state.current_thread_id,
            "stopped_thread_id": state.stopped_thread_id,
            "breakpoint_hit_thread_id": state.breakpoint_hit_thread_id,
            "breakpoints": [
                cls._breakpoint_snapshot(
                    breakpoint
                )
                for breakpoint
                in state.breakpoints.values()
            ],
            "console_output": list(
                session.console_output
            ),
            "target_output": list(
                session.target_output
            ),
            "log_output": list(
                session.log_output
            ),
            "stderr_output": list(
                session.stderr_output
            ),
        }

    @staticmethod
    # 线程视图同时保留选择与暂停相关标识，调试前端可据此定位上下文。
    def _threads_snapshot(
        session: GdbMiSession,
    ) -> dict[str, Any]:
        # 标记比较使用 GDB 线程 ID；没有 Linux TID 的线程也可被选中。
        state = session.state
        # 线程表与三个线程 ID 使用同一次内存视图。
        # 选中线程、触发停止者和命中者可相同，也可在手工切换后不同。
        return {
            "threads": [
                {
                    "thread_id": thread.thread_id,
                    "linux_tid": thread.linux_tid,
                    "name": thread.name,
                    "state": thread.state,
                    "core": thread.core,
                    "current": thread.thread_id == state.current_thread_id,
                    "breakpoint_hit": (
                        thread.thread_id == state.breakpoint_hit_thread_id
                    ),
                }
                for thread in state.threads.values()
            ],
            "current_thread_id": state.current_thread_id,
            "stopped_thread_id": state.stopped_thread_id,
            "breakpoint_hit_thread_id": state.breakpoint_hit_thread_id,
        }


# 管理器只索引任务对应的 broker；会话和进程由后台任务拥有。
class DebugSessionManager:
    """
    B8 多调试会话注册表。

    每个 task_id 对应一个独立 Broker。

    Manager自身不保存GdbMiSession对象，
    因为GdbMiSession属于Managed Task EventLoop，
    不能暴露给FastAPI线程。
    """

    # 注册表的锁跨 API 和任务线程使用。
    # 索引只含通道对象，因此持有锁不会取得访问调试进程或现场的权限。
    def __init__(
        self,
    ) -> None:
        self._lock = (
            threading.RLock()
        )

        # 注册表的共享数据只有任务到 Broker 的映射。
        # 读取方通过通道请求快照，不需要把调试循环对象移交给 HTTP。
        self._brokers: dict[
            UUID,
            DebugCommandBroker,
        ] = {}

    # 在锁内拒绝重复注册，避免同一任务 ID 指向两个命令通道。
    def register(
        self,
        task_id: UUID,
        broker: DebugCommandBroker,
    ) -> None:
        # 拒绝用新通道覆盖同一任务的旧通道，避免排队请求失去路由。
        with self._lock:
            # 重复注册是会话身份冲突，必须保留已存在的通道。
            # 静默替换会把旧请求和已有调试进程变成无法定位的对象。
            if (
                task_id
                in self._brokers
            ):
                raise (
                    DebugSessionAlreadyExists(
                        "debug session "
                        "already exists: "
                        f"{task_id}"
                    )
                )

            # 只有注册通过的任务才可通过管理器接收命令。
            # 注册本身不代表被调试程序已经运行。
            self._brokers[
                task_id
            ] = broker

    # 先从活跃表移除再关闭通道，新请求不会取得正在退役的入口。
    def unregister(
        self,
        task_id: UUID,
        *,
        error: (
            BaseException
            | None
        ) = None,
    ) -> None:
        # 注销即使找不到原通道也结束；调用方可以在显式关闭与兜底回收两条路径调用。
        with self._lock:
            # 先撤销可发现性，再关闭实际通道。
            # 已拿到引用的调用可能遇到关闭异常，新调用则找不到该活动会话。
            broker = (
                self._brokers.pop(
                    task_id,
                    None,
                )
            )

        # 先撤下目录入口，再在锁外关闭通道并唤醒等待者。
        # 客户端和任务清理可以重复调用注销。
        if broker is not None:
            # 实际关闭发生在注册表锁之外。
            # 通道可独立通知等待者，不需要持有所有会话共享的锁。
            broker.close(
                error
            )

    # 取得的是已登记通道；查询完成后仍可能由执行者关闭。
    def require(
        self,
        task_id: UUID,
    ) -> DebugCommandBroker:
        # 取得通道以后会话仍可能退出，真正投递时仍需检查通道关闭状态。
        with self._lock:
            broker = (
                self._brokers.get(
                    task_id
                )
            )

        # 不对未知任务创建空通道，避免调用者在无人消费的队列上等到超时。
        if broker is None:
            raise DebugSessionNotFound(
                "debug session "
                "does not exist: "
                f"{task_id}"
            )

        # 返回 Broker 引用时不持有注册表锁。
        # 取到引用不证明会话在提交命令时仍打开，提交阶段要接受关闭竞争。
        return broker

    # contains 只回答这一刻是否登记。
    # 它不是租约，之后真正投递仍可能收到会话关闭。
    def contains(
        self,
        task_id: UUID,
    ) -> bool:
        with self._lock:
            # 包含性只用于判断当下是否有注册项。
            # 这不是对下一次命令成功的预留，也不读取 inferior 的运行状态。
            return (
                task_id
                in self._brokers
            )

    # 返回任务标识快照，调用者不会遍历正在被其他线程修改的字典。
    def list_session_ids(
        self,
    ) -> list[UUID]:
        # 返回 ID 列表副本，调用方遍历时不依赖仍在变化的注册表。
        with self._lock:
            # 返回列表副本供调用方迭代，迭代期间不持有注册表锁。
            # 会话可能随后注销，逐条查询仍需处理不存在。
            return list(
                self._brokers.keys()
            )

    # 状态查询也走命令队列，与此前提交的状态更新遵循相同分发顺序。
    async def state(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.STATE,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    # 默认时限用于等待完整请求，排队较久时可能尚未送到 GDB。
    async def set_arguments(
        self,
        task_id: UUID,
        arguments: list[str],
        *,
        timeout_seconds: float = 10,
    ) -> None:
        # 把 argv 当作结构化参数传递，命令构造器仍负责每一项的引用边界。
        # 管理器无需解析 shell 文本。
        await self._request(
            task_id,
            DebugCommandKind.SET_ARGUMENTS,
            timeout_seconds=(
                timeout_seconds
            ),
            arguments=arguments,
        )

    # 请求保留临时、禁用和条件参数三种独立含义，管理器不根据其中一项推断其他项。
    async def insert_breakpoint(
        self,
        task_id: UUID,
        location: str,
        *,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        # 位置和选项通过不同参数跨线程传递。
        # 本层不自行解释条件表达式或调整断点所在帧。
        return await self._request(
            task_id,
            DebugCommandKind.INSERT_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            location=location,
            temporary=temporary,
            disabled=disabled,
            condition=condition,
        )

    # 删除只按编号路由到指定任务，其他任务的同编号断点不会受到影响。
    async def delete_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        # 断点编号只在指定任务的通道内有意义。
        # 另一个 GDB 会话中相同的编号不能作为同一个断点使用。
        await self._request(
            task_id,
            DebugCommandKind.DELETE_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            breakpoint_number=(
                breakpoint_number
            ),
        )

    async def enable_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        await self._request(
            task_id,
            DebugCommandKind.ENABLE_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            breakpoint_number=(
                breakpoint_number
            ),
        )

    async def disable_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        await self._request(
            task_id,
            DebugCommandKind.DISABLE_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            breakpoint_number=(
                breakpoint_number
            ),
        )

    async def run(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        # 命令启动指定会话已配置的 inferior。
        # API 等待预算是这条控制命令的等待时间，不是被调试程序的运行时限。
        return await self._request(
            task_id,
            DebugCommandKind.RUN,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def continue_execution(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.CONTINUE,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def next(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.NEXT,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def step(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.STEP,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def interrupt(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        # 提交中断使用普通请求预算。
        # 若需等待目标实际停下，应把停止等待作为另一条命令并单独给预算。
        return await self._request(
            task_id,
            DebugCommandKind.INTERRUPT,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    # 表达式原文交给选定会话，解释与错误来源为 GDB。
    # 管理器不在 API 线程解释或重写表达式。
    async def evaluate(
        self,
        task_id: UUID,
        expression: str,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.EVALUATE,
            timeout_seconds=(
                timeout_seconds
            ),
            expression=expression,
        )

    async def stack_frames(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.STACK_FRAMES,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    # 局部变量查询依赖选定会话的当前帧；管理器不为缺少现场生成空值占位。
    async def local_variables(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.LOCAL_VARIABLES,
            timeout_seconds=timeout_seconds,
        )

    async def threads(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        # 线程查询不从平台进程清单取值。
        # 实际 GDB 编号在会话内读取，才可用于后续选择。
        return await self._request(
            task_id,
            DebugCommandKind.THREADS,
            timeout_seconds=timeout_seconds,
        )

    # 接受的是 GDB 的线程 ID；操作系统线程号只用于诊断或协同映射。
    async def select_thread(
        self,
        task_id: UUID,
        thread_id: str,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.SELECT_THREAD,
            timeout_seconds=timeout_seconds,
            thread_id=thread_id,
        )

    # 允许分别设置 GDB 状态等待与调用方命令预算，避免互相重置。
    # 停止期限约束 MI 现场等待，请求期限还覆盖排队与交付。
    # 调用方可把外层期限设置得略长，以保留实际停止失败信息。
    async def wait_for_stop(
        self,
        task_id: UUID,
        *,
        stop_timeout_seconds: (
            float
            | None
        ) = None,
        request_timeout_seconds: (
            float
            | None
        ) = None,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.WAIT_FOR_STOP,
            timeout_seconds=(
                request_timeout_seconds
            ),
            stop_timeout_seconds=(
                stop_timeout_seconds
            ),
        )

    # 等关闭命令交付最终结果后移除注册，其他收尾路径仍可幂等注销。
    async def close_session(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        # 等 CLOSE 的回复后再注销，避免在命令被消费前就关闭通道。
        result = await self._request(
            task_id,
            DebugCommandKind.CLOSE,
            timeout_seconds=(
                timeout_seconds
            ),
        )

        # 成功关闭后可以撤销目录项。
        # 超时路径不直接撤销，以免隐瞒仍在执行的关闭请求；后台任务仍负责收尾。
        self.unregister(
            task_id
        )

        # 只有收到关闭结果才走此处的正常注销。
        # 若请求失败，上游 worker 的清理仍负责处理可能已经关闭的通道。
        return result

    # 统一定位 broker，只有暂停等待操作会附加它自己的内部时限。
    async def _request(
        self,
        task_id: UUID,
        operation: DebugCommandKind,
        *,
        timeout_seconds: (
            float
            | None
        ),
        stop_timeout_seconds: (
            float
            | None
        ) = None,
        **arguments: Any,
    ) -> Any:
        # 每次命令重新定位活跃会话。
        # 长期缓存通道会跨过注销边界，并可能使失败原因变得难以识别。
        broker = self.require(
            task_id
        )

        # 停止期限只发给等待操作；通道级请求期限对各类命令独立适用。
        if (
            operation
            == DebugCommandKind.WAIT_FOR_STOP
        ):
            # 仅等待停止的操作接受内部停止参数。
            # 其他请求即使允许长时间等待，也不会把这一参数解释为 MI 命令选项。
            arguments[
                "stop_timeout_seconds"
            ] = stop_timeout_seconds

        # 请求完成的是这一条操作的应答；状态快照不会随着之后的 MI 事件自动变化。
        return await broker.request(
            operation,
            timeout_seconds=(
                timeout_seconds
            ),
            **arguments,
        )
