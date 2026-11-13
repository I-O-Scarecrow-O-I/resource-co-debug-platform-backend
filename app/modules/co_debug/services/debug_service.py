from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import AppError
from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
)
from app.modules.co_debug.debug.session import GdbMiCommandError, GdbMiError
from app.modules.co_debug.schemas.debug import (
    DebugSessionResponse,
    DebugSessionStateResponse,
)
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.platform.domain.enums import (
    TaskStatus,
    TaskType,
)
from app.platform.domain.task import (
    TaskRecord,
)
from app.platform.schemas.tasks import (
    DebugTaskRequest,
)
from app.platform.services.task_store import (
    TaskStore,
)


class DebugSessionService:
    """
    B7/B8 对 REST API 暴露的统一业务门面。

    Route 不直接访问：

        DebugSessionManager
        DebugCommandBroker
        GdbMiSession

    所有调试操作统一经过本 Service。
    """

    def __init__(
        self,
        task_store: TaskStore,
        session_manager: (
            DebugSessionManager
            | None
        ) = None,
        interactive_service: (
            InteractiveDebugService
            | None
        ) = None,
    ) -> None:
        # 任务存储支持活动和历史查询；交互服务与注册表可以独立缺省。
        self.task_store = task_store

        # 注册表只回答当前进程是否有命令通道；任务历史不会因此变成可操作的会话。
        self.session_manager = (
            session_manager
        )

        self.interactive_service = (
            interactive_service
        )

    # 创建交互会话由任务执行服务负责；该接口不直接启动 GDB。
    async def create(
        self,
        request: DebugTaskRequest,
    ) -> TaskRecord:
        # 没有交互服务时不能启动会话，但已有任务记录仍可供描述接口查询。
        if (
            self.interactive_service
            is None
        ):
            raise AppError(
                "interactive debug service "
                "is unavailable"
            )

        # 这里返回创建后的任务记录。
        # 就绪信号要等后台完成握手与注册，不能从创建响应推断。
        return await (
            self.interactive_service
            .create_task(request)
        )

    # 会话摘要同时读取平台任务和活跃注册表，可区分尚未就绪与已经可控制。
    def describe(
        self,
        task_id: UUID,
    ) -> DebugSessionResponse:
        # 描述既支持活动会话也支持历史任务，命令列表只表达模块能力。
        self._require_debug_task(task_id)

        active = (
            self.session_manager
            is not None
            and self.session_manager
            .contains(task_id)
        )

        # 协议名称和命令清单是接口说明。
        # 当前任务是否还能接收命令以注册表和实际投递为准。
        return DebugSessionResponse(
            task_id=task_id,
            protocol="GDB/MI",
            supported_commands=[
                "set_arguments",
                "insert_breakpoint",
                "delete_breakpoint",
                "run",
                "continue",
                "next",
                "step",
                "interrupt",
                "evaluate",
                "stack_frames",
                "local_variables",
                "threads",
                "select_thread",
                "wait_for_stop",
                "close",
            ],
            note=(
                "Interactive GDB/MI "
                "session is active."
                if active
                else (
                    "The DEBUG task exists "
                    "but has no active "
                    "GDB/MI session."
                )
            ),
        )

    # 有活跃通道时向 GDB 所在线程请求快照；否则回退到任务记录。
    async def state(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        # 任务记录和 MI 快照分别从两个来源读取；它们不是同一事务内的原子快照。
        task = self._require_debug_task(
            task_id
        )

        manager = self.session_manager

        # 实时快照由会话工作循环提供，不能仅从平台任务状态推断。
        if (
            manager is not None
            and manager.contains(
                task_id
            )
        ):
            # 从 contains 到实际调用之间会话可能退出；这条路径不保证随后仍能取得实时状态。
            snapshot = (
                await manager.state(
                    task_id
                )
            )

            return (
                self._state_response(
                    task,
                    snapshot,
                    active=True,
                )
            )

        # 终态优先使用执行结果保存的协议状态，缺失时按平台状态派生。
        final_state = None

        # Broker 注销后优先用任务结果中保存的 MI 终态。
        if isinstance(
            task.result,
            dict,
        ):
            value = task.result.get(
                "final_state"
            )

            # 只有文本型 final_state 才用于恢复历史视图；不把任意结果对象格式化成状态名。
            if isinstance(
                value,
                str,
            ):
                final_state = value

        # 无终态证据时按平台状态降级；仍未注册的待启动任务可以显示为不活动的 STARTING。
        if final_state is None:
            # 任务还活跃但没有通道时显示启动中。
            # 它不构成 GDB 已打开或允许下发命令的证据。
            if task.status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
            }:
                final_state = "STARTING"

            elif (
                task.status
                == TaskStatus.FAILED
            ):
                final_state = "FAILED"

            else:
                final_state = "EXITED"

        # 不活动响应只交付已知的终态和平台状态。
        # 不从历史日志重建线程、断点或当前栈帧。
        return DebugSessionStateResponse(
            task_id=task.id,
            task_status=task.status,
            active=False,
            state=final_state,
        )

    # 运行参数更新只发给当前活跃会话，不修改项目持久化设置。
    # 参数更新要求活动会话；平台任务的原始命令记录不通过这个接口重写。
    async def set_arguments(
        self,
        task_id: UUID,
        arguments: list[str],
    ) -> None:
        manager = self._require_active(
            task_id
        )

        # 修改的是下一次启动目标所用的 GDB 参数；任务记录不会因此换成一条新命令。
        await manager.set_arguments(
            task_id,
            arguments,
        )

    # 断点通过调试会话创建，返回上游编号用于后续操作或规则关联。
    # 断点创建先检查活动性，随后由工作循环生成并返回协议编号。
    async def insert_breakpoint(
        self,
        task_id: UUID,
        *,
        location: str,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
    ) -> dict[str, Any]:
        manager = self._require_active(
            task_id
        )

        # 位置和条件只传给已选会话，实际编号与成功状态依赖该会话的回复。
        return await (
            manager.insert_breakpoint(
                task_id,
                location,
                temporary=temporary,
                disabled=disabled,
                condition=condition,
            )
        )

    # 只对指定会话下发删除；本地断点表在协议确认后由会话维护。
    async def delete_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
    ) -> None:
        manager = self._require_active(
            task_id
        )

        # 删除按 GDB 编号而不是源文件定位，避免同位置多个断点时误删。
        await manager.delete_breakpoint(
            task_id,
            breakpoint_number,
        )

    # 运行类方法统一走状态命令入口，避免每种操作分别拼装平台与 MI 响应。
    async def run(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        # 命令返回的状态描述会话当前观察值，不能作为目标已完成运行的凭据。
        return await self._state_command(
            task_id,
            "run",
        )

    # 继续控制已有 inferior，API 不会为此重新复制工作区。
    async def continue_execution(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        return await self._state_command(
            task_id,
            "continue_execution",
        )

    # next 和 step 保留各自协议动作；错误仍经过统一状态命令适配。
    async def next(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        # 步过请求沿用通用状态响应，后续若仍运行，应等待停止再检查现场。
        return await self._state_command(
            task_id,
            "next",
        )

    async def step(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        # 步入的语义由 GDB 负责；此层不会通过修改断点模拟一次步入。
        return await self._state_command(
            task_id,
            "step",
        )

    # 发出中断后返回当时的现场；需要确认停止的调用方走 wait_for_stop。
    async def interrupt(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        # 主动请求中断与实际暂停是两个步骤；本接口交付第一步之后的快照。
        return await self._state_command(
            task_id,
            "interrupt",
        )

    # 分别控制队列请求的预算与实际暂停观察，不把等待超时解释成停止。
    async def wait_for_stop(
        self,
        task_id: UUID,
        *,
        timeout_seconds: (
            float
            | None
        ) = None,
    ) -> DebugSessionStateResponse:
        # 等待操作既要有效任务，又要活动 broker，历史结果不能代替现场等待。
        task = self._require_debug_task(
            task_id
        )

        manager = self._require_active(
            task_id
        )

        # 外层期限为排队和交付多留一点余量；未显式设期限时不在这两层添加超时。
        snapshot = (
            await manager.wait_for_stop(
                task_id,
                stop_timeout_seconds=(
                    timeout_seconds
                ),
                request_timeout_seconds=(
                    None
                    if timeout_seconds is None
                    else timeout_seconds + 1
                ),
            )
        )

        # 返回的 active 反映本次调用走实时会话路径；任务状态仍取提交命令之前的记录。
        return self._state_response(
            task,
            snapshot,
            active=True,
        )

    # 表达式结果保留 GDB 的文本表示，以当前线程和栈帧为上下文。
    # 表达式在当前 GDB 上下文求值，HTTP 进程不自行解释表达式。
    async def evaluate(
        self,
        task_id: UUID,
        expression: str,
    ) -> dict[str, Any]:
        manager = self._require_active(
            task_id
        )

        # 求值结果保持 GDB 文本表示；不按 Python 数字或对象重新解释。
        return await manager.evaluate(
            task_id,
            expression,
        )

    # 栈帧通过活动会话获取；任务已完成时不从历史结果捏造帧。
    # 栈帧读取要求活动现场，历史任务不提供这种查询。
    async def stack_frames(
        self,
        task_id: UUID,
    ) -> dict[str, Any]:
        manager = self._require_active(
            task_id
        )

        # 读取栈帧不改变选中线程；使用调用时 GDB 已选定的检查上下文。
        return await (
            manager.stack_frames(
                task_id
            )
        )

    # 局部变量与栈帧同属实时检查接口；没有活跃进程时不构造替代结果。
    async def local_variables(
        self,
        task_id: UUID,
    ) -> dict[str, Any]:
        manager = self._require_active(task_id)
        return await manager.local_variables(task_id)

    # 线程数据由 GDB 刷新，前端身份使用 GDB 编号而非系统线程号。
    async def threads(
        self,
        task_id: UUID,
    ) -> dict[str, Any]:
        manager = self._require_active(task_id)
        return await manager.threads(task_id)

    # 选择线程失败转换为接口错误，不能把未知线程作为当前线程返回。
    # 线程选择以 GDB ID 为依据；刷新后的线程结果用于更新客户端选中标记。
    async def select_thread(
        self,
        task_id: UUID,
        thread_id: str,
    ) -> dict[str, Any]:
        # 线程检查在活跃会话内执行；关闭和线程退出都可能在校验之后发生。
        manager = self._require_active(task_id)
        try:
            return await manager.select_thread(task_id, thread_id)
        # 选择失败转为应用错误，并保留底层异常链以便诊断。
        except GdbMiError as exc:
            raise AppError(f"Failed to select debug thread: {exc}") from exc

    # 主动关闭后返回最后快照，平台任务终态由后台执行者落库。
    async def close(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        task = self._require_debug_task(
            task_id
        )

        manager = self._require_active(
            task_id
        )

        # 关闭返回最后一份现场并标为不活动。
        # 平台终态可能稍后落盘，返回的任务状态来自下发前。
        snapshot = (
            await manager.close_session(
                task_id
            )
        )

        # 主动关闭的响应不等待平台重新读取持久化终态，因此仍可能带此前的 RUNNING。
        return self._state_response(
            task,
            snapshot,
            active=False,
        )

    # 多种执行命令共享任务类型、活动状态校验和错误转换。
    async def _state_command(
        self,
        task_id: UUID,
        operation: str,
    ) -> DebugSessionStateResponse:
        # 先保存平台侧的任务快照，随后让命令通道提供协议侧状态。
        task = self._require_debug_task(
            task_id
        )

        manager = self._require_active(
            task_id
        )

        # 操作名来自服务内部的固定分派，协议拒绝会附带对应操作名。
        method = getattr(
            manager,
            operation,
        )

        # 命令结果来自工作循环的快照，与读取平台任务的时刻可能不同。
        try:
            snapshot = await method(
                task_id
            )
        # 把 MI 拒绝关联到具体操作，便于客户端区分运行状态与命令本身失败。
        except GdbMiCommandError as exc:
            command_name = operation.replace("_", " ")
            raise AppError(
                f"Failed to {command_name} debug target: {exc}"
            ) from exc

        # 只在调用正常返回后构造活动响应；协议拒绝不会被包装成成功快照。
        return self._state_response(
            task,
            snapshot,
            active=True,
        )

    # 必须是调试任务才能解析会话状态，通用任务 ID 本身不足以授权此入口。
    # 先核验任务类型，避免非 DEBUG 任务进入会话注册表查询。
    def _require_debug_task(
        self,
        task_id: UUID,
    ) -> TaskRecord:
        # 找不到任务时沿用存储层的缺失错误；类型不符另给出可操作的诊断。
        task = self.task_store.require(
            task_id
        )

        if (
            task.task_type
            != TaskType.DEBUG
        ):
            raise AppError(
                "task is not a DEBUG task"
            )

        return task

    # 这里只检查 DEBUG 任务类型和活跃通道；是否为 RUNNING 由协同层另行判断。
    def _require_active(
        self,
        task_id: UUID,
    ) -> DebugSessionManager:
        self._require_debug_task(
            task_id
        )

        # 实时可操作性以本进程目录中存在通道为准，不能仅看持久化任务的状态字段。
        # 会话启动和退出的瞬间两者可能暂时不一致。
        manager = (
            self.session_manager
        )

        # 活动检查只验证此刻有已注册通道；进程之后退出仍可能导致实际投递失败。
        if (
            manager is None
            or not manager.contains(
                task_id
            )
        ):
            raise AppError(
                "debug session is not active"
            )

        return manager

    @staticmethod
    # 合并平台任务字段和协议快照，保留状态来源各自的语义。
    def _state_response(
        task: TaskRecord,
        snapshot: dict[str, Any],
        *,
        active: bool,
    ) -> DebugSessionStateResponse:
        # 合并平台生命周期和一次 MI 命令得到的快照；缺省的输出与断点按空列表返回。
        return DebugSessionStateResponse(
            task_id=task.id,
            task_status=task.status,
            active=active,
            state=snapshot["state"],
            stop_reason=(
                snapshot.get(
                    "stop_reason"
                )
            ),
            current_file=(
                snapshot.get(
                    "current_file"
                )
            ),
            current_line=(
                snapshot.get(
                    "current_line"
                )
            ),
            current_function=(
                snapshot.get(
                    "current_function"
                )
            ),
            current_thread_id=snapshot.get("current_thread_id"),
            stopped_thread_id=snapshot.get("stopped_thread_id"),
            breakpoint_hit_thread_id=snapshot.get("breakpoint_hit_thread_id"),
            breakpoints=list(
                snapshot.get(
                    "breakpoints",
                    [],
                )
            ),
            console_output=list(
                snapshot.get(
                    "console_output",
                    [],
                )
            ),
            target_output=list(
                snapshot.get(
                    "target_output",
                    [],
                )
            ),
            log_output=list(
                snapshot.get(
                    "log_output",
                    [],
                )
            ),
            stderr_output=list(
                snapshot.get(
                    "stderr_output",
                    [],
                )
            ),
        )
