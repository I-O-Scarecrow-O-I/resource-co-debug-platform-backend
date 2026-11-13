from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from uuid import UUID, uuid4

from app.core.errors import AppError
from app.core.time import utc_now
from app.modules.co_debug.coordination.models import (
    DebugCoordinationEventOutcome,
    DebugCoordinationEventRecord,
    DebugCoordinationGroup,
    DebugCoordinationGroupStatus,
    DebugCoordinationRule,
    DebugCoordinationRuleAction,
    DebugCoordinationRuleBinding,
    DebugGroupCommand,
    DebugGroupCommandOutcome,
    DebugLogicalPartition,
)
from app.modules.co_debug.debug.events import (
    DebugSessionEvent,
    DebugSessionEventBus,
    DebugSessionEventKind,
)
from app.modules.co_debug.debug.manager import DebugSessionManager
from app.modules.co_debug.schemas.coordination import (
    DebugCoordinationEventResponse,
    DebugCoordinationGroupRequest,
    DebugCoordinationGroupResponse,
    DebugCoordinationRuleRequest,
    DebugCoordinationRuleResponse,
    DebugGroupCommandResponse,
    DebugGroupMemberCommandResult,
    DebugLogicalPartitionResponse,
)
from app.modules.co_debug.schemas.debug import DebugSessionStateResponse
from app.modules.co_debug.services.debug_coordination_store import DebugCoordinationStore
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.services.task_store import TaskStore

logger = logging.getLogger(__name__)

# 这段预算只用于命令已被接受后的停止确认，不是成员会话的默认命令时限。
_INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS = 3.0


class DebugCoordinationService:
    def __init__(
        self,
        *,
        store: DebugCoordinationStore,
        task_store: TaskStore,
        session_manager: DebugSessionManager,
        debug_service: DebugSessionService,
        event_bus: DebugSessionEventBus | None = None,
    ) -> None:
        # 组与规则是可持久化的，活跃调试器与事件循环只存在于当前服务进程。
        # 重启后仍能查看历史组，但不会仅靠记录重建它的成员进程。
        self.store = store
        self.task_store = task_store
        self.session_manager = session_manager
        self.debug_service = debug_service
        self.event_bus = event_bus
        # 组操作锁用于排列同一组的控制命令、成员变更和规则执行。
        # 事件订阅另受线程锁保护，因为关闭可能来自不同于订阅者的线程。
        self._operation_locks: dict[UUID, asyncio.Lock] = {}
        # 线程锁只协调订阅生命周期；它不会阻塞同组异步操作直到 GDB 返回。
        self._event_lock = threading.RLock()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._event_subscription_id: UUID | None = None
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # 每个逻辑分区绑定同一项目中仍可控制的独立调试会话。
    # 创建协作组不会启动新的 GDB 进程。
    async def create(
        self,
        request: DebugCoordinationGroupRequest,
    ) -> DebugCoordinationGroupResponse:
        # 订阅建立后组才可能接收新断点事件；创建前不会向现有成员发送命令。
        self._ensure_event_subscription()
        # 创建组只关联已有会话，不启动 GDB 或补建任务。
        # 平台任务状态与进程内会话注册都必须有效，才能成为成员。
        for member in request.members:
            # 项目归属是跨会话协作的边界，不能把另一个工程的 DEBUG 任务混入当前组。
            task = self.task_store.require(member.session_task_id)
            if task.project_id != request.project_id:
                raise AppError("all debug sessions must belong to the same project")
            if task.task_type != TaskType.DEBUG:
                raise AppError("coordination members must reference DEBUG tasks")
            # 任务 RUNNING 与会话已注册同时成立才可加入。
            # 仍在握手或已经注销的工作器都不能靠任务记录充当活动会话。
            if task.status != TaskStatus.RUNNING or not self.session_manager.contains(task.id):
                raise AppError("coordination members must reference active DEBUG sessions")

        # 组和初始成员共享同一个创建时刻，成员有独立的稳定 ID 可供规则引用。
        created_at = utc_now()
        group = DebugCoordinationGroup(
            id=uuid4(),
            project_id=request.project_id,
            name=request.name,
            created_at=created_at,
        )
        # 逻辑分区使用独立身份，即使它指向的任务日后退出，也能保留关联历史。
        members = [
            DebugLogicalPartition(
                id=uuid4(),
                group_id=group.id,
                name=member.partition_name,
                session_task_id=member.session_task_id,
                joined_at=created_at,
            )
            for member in request.members
        ]
        # 服务先校验运行现场，存储负责把组和成员作为一个整体登记。
        # 保存成功后再按实际状态组装对外视图。
        self.store.create(group, members)
        return await self._response(group)

    # 项目过滤只缩小持久组集合；每组的实时活动状态仍需独立读取。
    async def list(
        self,
        project_id: UUID | None = None,
    ) -> list[DebugCoordinationGroupResponse]:
        self._ensure_event_subscription()
        # 每个组的活动状态由当前成员快照派生，列表不只读取组表中的持久字段。
        groups = self.store.list(project_id)
        # 列表组装可以并发等待各组的会话；当前状态不保证是所有成员同一瞬间的快照。
        return await asyncio.gather(*(self._response(group) for group in groups))

    async def get(self, group_id: UUID) -> DebugCoordinationGroupResponse:
        self._ensure_event_subscription()
        return await self._response(self.store.require(group_id))

    # 规则绑定时核对组、双方会话和源断点；事件到达后还会检查当时状态。
    async def create_rule(
        self,
        group_id: UUID,
        request: DebugCoordinationRuleRequest,
    ) -> DebugCoordinationRuleResponse:
        self._ensure_event_subscription()
        # 同组变更在锁内执行，规则创建与成员移除不会交错。
        # 来源和目标会话的活动性仍取自运行时快照。
        async with self._lock_for(group_id):
            group = self.store.require(group_id)
            # 关闭组仍可以查看历史，但不允许登记新的联动动作。
            if group.closed_at is not None:
                raise AppError("debug coordination group is closed")
            # 规则不能把源会话中断动作发回同一个逻辑分区，避免自联动。
            if request.source_partition_id == request.target_partition_id:
                raise AppError("source and target partitions must be different")

            # 成员查询带组 ID，两个端点都须属于当前组且仍关联。
            source = self.store.require_member(group_id, request.source_partition_id)
            target = self.store.require_member(group_id, request.target_partition_id)
            # 即使分区 ID 仍可查询，解除关联后也没有资格作为新规则的来源或目标。
            if not source.associated or not target.associated:
                raise AppError("coordination rule members must still be associated")

            # 并发查询两个会话当前状态，只用于判断能否登记规则。
            # 这一快照不会当作将来每次触发时的状态。
            source_state, target_state = await asyncio.gather(
                self.debug_service.state(source.session_task_id),
                self.debug_service.state(target.session_task_id),
            )
            # 发现任一会话不活跃时整条规则不创建；不会建立半有效的目标关联。
            if not self._is_active_state(source_state) or not self._is_active_state(target_state):
                raise AppError("coordination rule members must reference active DEBUG sessions")
            # 匹配依据是来源会话中的断点号。
            # 相同编号可在不同会话独立出现，因此不能仅凭编号定位全局来源。
            breakpoint_numbers = {
                str(item.get("number"))
                for item in source_state.breakpoints
                if isinstance(item, dict) and item.get("number") is not None
            }
            # 源断点通过即时快照验证存在性。
            # 规则存储编号，不为目标会话另外安装断点。
            if request.source_breakpoint_number not in breakpoint_numbers:
                raise AppError("source breakpoint does not exist in the active debug session")

            # 固定的动作是中断目标会话；来源条件绑定实际存在的断点编号。
            rule = DebugCoordinationRule(
                id=uuid4(),
                group_id=group_id,
                source_partition_id=source.id,
                source_breakpoint_number=request.source_breakpoint_number,
                target_partition_id=target.id,
                action=DebugCoordinationRuleAction.INTERRUPT,
                created_at=utc_now(),
            )
            # 规则由数据库保存，返回的是登记事实；它没有触发中断，直到真正收到命中事件。
            self.store.create_rule(rule)
            return self._rule_response(rule)

    # 规则列表返回现有有效定义；不会按实时断点表重写已存规则。
    async def list_rules(self, group_id: UUID) -> list[DebugCoordinationRuleResponse]:
        self._ensure_event_subscription()
        return [self._rule_response(rule) for rule in self.store.list_rules(group_id)]

    # 规则删除只影响匹配资格；已经记录的事件仍能引用原规则身份。
    async def delete_rule(self, group_id: UUID, rule_id: UUID) -> None:
        # 与同组执行使用同一把锁，确保删除操作不会和正在执行的组操作交错。
        async with self._lock_for(group_id):
            self.store.delete_rule(group_id, rule_id)

    async def list_events(
        self,
        group_id: UUID,
        *,
        limit: int = 100,
    ) -> list[DebugCoordinationEventResponse]:
        # 历史事件列表来自持久记录，可用于观察发送和确认结果，无需活动会话仍存在。
        self._ensure_event_subscription()
        # 读取历史不会等待目标停止或重新下发未确认的中断。
        return [
            self._event_response(event) for event in self.store.list_events(group_id, limit=limit)
        ]

    # 移除逻辑成员只改变关联关系，底层调试会话由专门的关闭命令处理。
    async def remove_member(
        self,
        group_id: UUID,
        partition_id: UUID,
    ) -> DebugCoordinationGroupResponse:
        # 移除成员只撤销关联；其单独调试会话仍可由原入口继续操作。
        async with self._lock_for(group_id):
            # 完成解除关联后重新组装组响应，便于客户端立即看到成员已移除。
            self.store.remove_member(group_id, partition_id)
            return await self.get(group_id)

    # 关闭组停止协调，不等价于主动关闭该组的全部调试进程。
    # 只关闭组关联，不终止成员任务；随后展示关闭状态与成员历史。
    async def close_group(self, group_id: UUID) -> DebugCoordinationGroupResponse:
        """Close only the association. Existing GDB sessions remain untouched."""
        async with self._lock_for(group_id):
            # 组关闭只改变关系与关闭时间；它不依赖任何成员调试器仍可访问。
            group = self.store.close_group(group_id)
            return await self._response(group)

    # 组 RUN 使用成员当前状态判定能否首次启动，每个成员保留独立结果。
    async def run(self, group_id: UUID) -> DebugGroupCommandResponse:
        return await self._command(group_id, DebugGroupCommand.RUN)

    async def continue_execution(self, group_id: UUID) -> DebugGroupCommandResponse:
        return await self._command(group_id, DebugGroupCommand.CONTINUE)

    async def interrupt(self, group_id: UUID) -> DebugGroupCommandResponse:
        return await self._command(group_id, DebugGroupCommand.INTERRUPT)

    # 关闭会话只收束成员的运行资源，组及成员的历史关联仍可查询。
    async def close_sessions(self, group_id: UUID) -> DebugGroupCommandResponse:
        """Close member sessions without closing or deleting the group association."""
        return await self._command(group_id, DebugGroupCommand.CLOSE_SESSIONS)

    # 组锁串行化成员变更和事件动作；同一组命令内部并发访问不同会话。
    async def _command(
        self,
        group_id: UUID,
        command: DebugGroupCommand,
    ) -> DebugGroupCommandResponse:
        # 持锁直到本次成员结果齐备，防止同组变更改变命令广播的对象集合。
        # 成员间并发下发，不要求任何会话跟随另一个会话回滚。
        async with self._lock_for(group_id):
            # 组 ID 先解析到真实记录；关闭状态在读取广播成员之前就拒绝命令。
            group = self.store.require(group_id)
            if group.closed_at is not None:
                raise AppError("debug coordination group is closed")
            # 广播前选出仍关联成员；历史成员不会重新接收命令。
            # 关闭剩余会话是清理操作，即使组已不足两个成员仍可进行。
            members = self.store.list_members(group_id, include_removed=False)
            # 普通协同命令要求至少两个成员；降级组仍可关闭剩余会话。
            if len(members) < 2 and command != DebugGroupCommand.CLOSE_SESSIONS:
                raise AppError("debug coordination group requires at least two associated members")
            # 组命令保留每个成员的独立结果。
            # 跳过和失败都使 all_succeeded 为假，调用方可据成员状态决定后续操作。
            results = await asyncio.gather(
                *(self._command_member(member, command) for member in members)
            )
            # 只有非空成员结果全部成功时才给出整体成功，跳过也会保留。
            return DebugGroupCommandResponse(
                group_id=group_id,
                command=command,
                all_succeeded=bool(results)
                and all(result.outcome == DebugGroupCommandOutcome.SUCCEEDED for result in results),
                results=results,
            )

    # 逐成员读取状态并执行，单个调试器异常以局部结果交付。
    # 单成员结果是组响应的最小单位；一个调试器不可用不丢弃其他成员的回执。
    async def _command_member(
        self,
        member: DebugLogicalPartition,
        command: DebugGroupCommand,
    ) -> DebugGroupMemberCommandResult:
        # 查询失败和状态不适用分别返回 FAILED 与 SKIPPED。
        # 前者没有可靠现场，后者保留已知状态供调用方检查。
        try:
            before = await self.debug_service.state(member.session_task_id)
        # 无法读取成员当前状态时无法做命令前置判断；记录错误且不向该成员发命令。
        except Exception as exc:
            return self._command_result(
                member,
                DebugGroupCommandOutcome.FAILED,
                error=self._error_message(exc),
            )

        # 状态不满足命令条件时记录跳过；不用向 GDB 发送注定不适用的请求。
        # 预检没有通过时不发命令，返回已有状态说明为何跳过。
        skip_reason = self._skip_reason(before, command)
        # 跳过保留本次看到的状态，可让前端区分已经运行、尚未就绪或已经离线。
        if skip_reason is not None:
            return self._command_result(
                member,
                DebugGroupCommandOutcome.SKIPPED,
                state=before,
                error=skip_reason,
            )

        # 状态检查与实际调试命令之间可能有外部操作改变现场，实际错误仍需单成员捕获。
        try:
            if command == DebugGroupCommand.RUN:
                # 每条分支调用成员独立的会话门面，不经共享 shell 或替换成员的调试上下文。
                state = await self.debug_service.run(member.session_task_id)
            elif command == DebugGroupCommand.CONTINUE:
                state = await self.debug_service.continue_execution(member.session_task_id)
            elif command == DebugGroupCommand.INTERRUPT:
                state = await self.debug_service.interrupt(member.session_task_id)
            else:
                state = await self.debug_service.close(member.session_task_id)
        # 命令异常仍附上执行前快照，说明最后一次可确认的状态。
        # 单成员命令失败时附带下发前的快照。
        # 它只是诊断上下文，不保证命令在报错前完全没有影响。
        except Exception as exc:
            return self._command_result(
                member,
                DebugGroupCommandOutcome.FAILED,
                state=before,
                error=self._error_message(exc),
            )

        # 成功表示调用已返回一份现场，不替代专门的异步暂停确认。
        return self._command_result(
            member,
            DebugGroupCommandOutcome.SUCCEEDED,
            state=state,
        )

    @staticmethod
    # 平台任务需要仍在运行，同时调试会话满足具体命令的状态前置条件。
    def _skip_reason(
        state: DebugSessionStateResponse,
        command: DebugGroupCommand,
    ) -> str | None:
        # 先确认运行对象仍活跃，再判断具体命令所需的 MI 状态。
        # 即使持久任务显示 RUNNING，失去会话通道也不能下发组命令。
        if not state.active:
            return "debug session is not active"
        if state.task_status != TaskStatus.RUNNING:
            return "debug task is not running"
        # 平台任务处于运行只说明会话还由任务托管。
        # 控制操作还需检查 MI 状态，区分首次运行、继续及中断的前置条件。
        normalized = state.state.upper()
        required = {
            DebugGroupCommand.RUN: "READY",
            DebugGroupCommand.CONTINUE: "STOPPED",
            DebugGroupCommand.INTERRUPT: "RUNNING",
        }.get(command)
        # 关闭命令没有特定 MI 运行态要求；活动性和平台任务状态的前置检查仍然适用。
        if required is not None and normalized != required:
            return f"debug session must be {required}"
        return None

    @staticmethod
    # 统一保留分区和任务标识，以便界面把结果归回正确成员。
    def _command_result(
        member: DebugLogicalPartition,
        outcome: DebugGroupCommandOutcome,
        *,
        state: DebugSessionStateResponse | None = None,
        error: str | None = None,
    ) -> DebugGroupMemberCommandResult:
        # 稳定分区身份和运行任务 ID 同时返回，客户端不必用成员列表位置关联响应。
        return DebugGroupMemberCommandResult(
            partition_id=member.id,
            partition_name=member.name,
            session_task_id=member.session_task_id,
            outcome=outcome,
            state=state,
            error=error,
        )

    # 组展示状态根据当前关联及会话可用性推导，和成员自身状态区分。
    async def _response(
        self,
        group: DebugCoordinationGroup,
    ) -> DebugCoordinationGroupResponse:
        # 已移除成员仍能出现在响应中供回顾，但不参与活动成员计数。
        members = self.store.list_members(group.id)
        member_responses = await asyncio.gather(
            *(self._member_response(member) for member in members)
        )
        # 组状态只聚合仍关联的成员；历史会话离线不会把仍健康的当前组降级。
        associated_members = [member for member in member_responses if member.associated]
        active_count = sum(
            member.session is not None
            and member.session.active
            and member.session.task_status == TaskStatus.RUNNING
            for member in associated_members
        )
        # 关闭是显式持久状态，优先于现场推导。
        # 未关闭的组若有成员失联或人数不足，保留组身份并报告降级。
        if group.closed_at is not None:
            status = DebugCoordinationGroupStatus.CLOSED
        elif (
            len(associated_members) >= 2
            and active_count == len(associated_members)
            and all(member.error is None for member in associated_members)
        ):
            status = DebugCoordinationGroupStatus.ACTIVE
        else:
            status = DebugCoordinationGroupStatus.DEGRADED
        # 关闭是持久事实，活动人数则是此次查询观察值。
        # 保留每个成员详情以解释降级来源。
        return DebugCoordinationGroupResponse(
            group_id=group.id,
            project_id=group.project_id,
            name=group.name,
            status=status,
            created_at=group.created_at,
            closed_at=group.closed_at,
            active_member_count=active_count,
            members=member_responses,
        )

    # 成员读取错误只写到该成员的结果，组状态可据此降级而继续返回。
    async def _member_response(
        self,
        member: DebugLogicalPartition,
    ) -> DebugLogicalPartitionResponse:
        # 成员查询按关联状态决定是否访问 GDB；历史成员只返回关联时间和身份。
        state = None
        error = None
        # 仅对仍关联的成员读取现场；成员退出或查询失败不妨碍返回整组历史。
        if member.associated:
            try:
                state = await self.debug_service.state(member.session_task_id)
            # 单个成员查询出错不能破坏其他成员的证据，错误跟随该成员返回。
            except Exception as exc:
                error = self._error_message(exc)
        return DebugLogicalPartitionResponse(
            partition_id=member.id,
            partition_name=member.name,
            session_task_id=member.session_task_id,
            associated=member.associated,
            joined_at=member.joined_at,
            removed_at=member.removed_at,
            session=state,
            error=error,
        )

    # 用组标识作为锁粒度，独立调试组不必互相等待。
    # 不同组的操作可以并行，同组的组操作与断点联动按同一把锁串行。
    def _lock_for(self, group_id: UUID) -> asyncio.Lock:
        return self._operation_locks.setdefault(group_id, asyncio.Lock())

    # 订阅绑定协调服务所用的事件循环；循环变化时替换原订阅。
    def _ensure_event_subscription(self) -> None:
        event_bus = self.event_bus
        if event_bus is None:
            return
        # 总线回调在订阅者自己的事件循环运行，使规则任务能使用该循环上的组锁。
        loop = asyncio.get_running_loop()
        # 一个服务实例只保留当前事件循环的订阅。
        # 循环切换时先移除旧订阅，避免同一命中事件被重复调度。
        with self._event_lock:
            if self._closed:
                raise RuntimeError("debug coordination service is closed")
            # 同一循环内重复调用不会重复注册，否则一次命中可能重复入队。
            if self._event_subscription_id is not None and self._event_loop is loop:
                return
            # 循环更换时旧订阅必须移除；已关闭服务拒绝再建订阅。
            if self._event_subscription_id is not None:
                event_bus.unsubscribe(self._event_subscription_id)
            self._event_loop = loop
            self._event_subscription_id = event_bus.subscribe(loop, self._schedule_event)

    # 事件回调只登记短异步任务，避免阻塞 GDB 的记录读取路径。
    def _schedule_event(self, event: DebugSessionEvent) -> None:
        if self._closed:
            return
        # 把规则处理放到独立协程，MI 事件投递无需等待目标会话暂停。
        # 记录在途任务以便应用关闭时发出取消。
        task = asyncio.create_task(self._handle_session_event(event))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_task_done)

    # 主动取消无需当作错误；其他后台异常要被消费并记录。
    # 完成回调撤销在途登记，后续关闭只处理尚未收束的任务。
    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        # 先移除已经完成的任务；即使读取异常并写日志，它也不再计入关闭时需要取消的集合。
        self._event_tasks.discard(task)
        if task.cancelled():
            return
        # 完成回调主动提取异常，使后台失败进入日志。
        # 取消属于生命周期控制，不作为规则执行错误记录。
        error = task.exception()
        if error is not None:
            logger.error(
                "debug coordination event processing failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    # 仅断点命中触发规则查找，按会话和断点编号关联。
    async def _handle_session_event(self, event: DebugSessionEvent) -> None:
        # 规则触发依据是协议确认的断点命中，不是普通暂停或控制命令成功。
        if event.kind != DebugSessionEventKind.BREAKPOINT_HIT:
            return
        # 一次命中可以匹配多个目标绑定；每条规则分别保留认领和结果。
        bindings = self.store.matching_rules(
            event.session_task_id,
            event.breakpoint_number,
        )
        # 规则可以并发排队；共享一个组 ID 的动作仍会在获取组锁时串行。
        await asyncio.gather(*(self._handle_rule_event(event, binding) for binding in bindings))

    # 先构造可持久化的 PROCESSING 记录，获得执行资格后才调用目标会话。
    async def _handle_rule_event(
        self,
        source_event: DebugSessionEvent,
        binding: DebugCoordinationRuleBinding,
    ) -> None:
        # 先存处理中的事件，以便在下发外部动作前取得该规则对本次命中的唯一认领。
        # 认领失败则不再对目标发送命令。
        initial = DebugCoordinationEventRecord(
            id=uuid4(),
            source_event_id=source_event.event_id,
            group_id=binding.rule.group_id,
            rule_id=binding.rule.id,
            source_partition_id=binding.source.id,
            source_session_task_id=binding.source.session_task_id,
            target_partition_id=binding.target.id,
            target_session_task_id=binding.target.session_task_id,
            breakpoint_number=source_event.breakpoint_number,
            occurred_at=source_event.occurred_at,
            processed_at=utc_now(),
            outcome=DebugCoordinationEventOutcome.PROCESSING,
            command_sent=False,
            target_paused=False,
            message="processing breakpoint coordination rule",
        )
        # 领取失败表示该来源事件与规则已处理或规则不再有效，不重复中断。
        # 无法取得认领时结束本条规则，不根据内存状态再试一次外部动作。
        if not self.store.claim_event(initial):
            return

        # 在组锁内执行并保存结果，避免与其他协调动作交错更新同组会话。
        # 认领和取得组锁之间，组与成员可能已发生变化。
        # 执行路径重新读取组和成员；无论执行或跳过都尽量写回明确结果。
        async with self._lock_for(binding.rule.group_id):
            try:
                final = await self._execute_rule_event(initial, binding)
            # 业务异常转换成此次事件的失败终态；这不意味着目标会话一定没有发生变化。
            except Exception as exc:
                final = self._finish_event(
                    initial,
                    DebugCoordinationEventOutcome.FAILED,
                    f"coordination rule failed: {self._error_message(exc)}",
                )
            # 处理结论写回先前认领的事件，保持同一个历史身份。
            self.store.finalize_event(final)

    # 真正执行前再次取得组和成员状态；等待组锁期间可能已经移除关联。
    async def _execute_rule_event(
        self,
        event: DebugCoordinationEventRecord,
        binding: DebugCoordinationRuleBinding,
    ) -> DebugCoordinationEventRecord:
        # 在组锁内重新读取源和目标；之前查询的绑定只用于定位它们。
        group = self.store.require(binding.rule.group_id)
        # 即使规则匹配时源分区有效，也要确认其仍关联，防止在等待组锁期间被移除。
        source = self.store.require_member(group.id, binding.source.id)
        target = self.store.require_member(group.id, binding.target.id)
        # 显式关闭优先于现场状态，即使目标仍运行也不再执行本组联动。
        if group.closed_at is not None:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "group is closed",
            )
        # 来源成员移除后，已经在途的事件不再有资格触发规则。
        if not source.associated:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "source partition is no longer associated",
            )
        # 目标已解除关联时不向它下发中断，即使其独立会话还活跃。
        if not target.associated:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "target partition is no longer associated",
            )

        # 读取实时目标状态，不能用创建规则时的会话状态决定是否中断。
        # 通过成员所指任务读取目标现场，不复用规则创建时的会话快照。
        try:
            target_state = await self.debug_service.state(target.session_task_id)
        # 无法读取目标状态不能当成未运行；记录为规则失败以便区分。
        except Exception as exc:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.FAILED,
                f"failed to read target state: {self._error_message(exc)}",
            )

        # 任务或运行对象失效时按跳过处理，这不是中断协议命令失败。
        if not self._is_active_state(target_state):
            # 不活动时没有可靠的运行对象，不把未送达命令的情况报成中断失败。
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "target debug session is not active",
            )
        # 已经暂停的目标无需重复发送命令；结果记录当前暂停事实。
        # 目标原本就停下时只记录现场，不把已有暂停归因于本次规则。
        if target_state.state.upper() == "STOPPED":
            # 目标已经暂停也是可观察状态，但 command_sent 保持默认值，以区分这次触发是否采取动作。
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "target debug session is already stopped",
                target_paused=True,
            )
        # 未在 RUNNING 的其他状态没有中断前提；例如就绪会话尚未运行目标。
        if target_state.state.upper() != "RUNNING":
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                f"target debug session is {target_state.state}",
            )

        # 只有活动且正在运行的会话才能接收中断；发送失败与确认失败分别记录。
        # 只有确认目标正在运行后才尝试下发；命令返回仍不能直接证明它已暂停。
        try:
            interrupted = await self.debug_service.interrupt(target.session_task_id)
        # 发送时发生协议或会话错误，结果不会标记 command_sent。
        except Exception as exc:
            # 发送调用异常和后续确认异常分开记录，后者至少已有命令应答证据。
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.FAILED,
                f"failed to send interrupt: {self._error_message(exc)}",
            )

        # 命令返回时若已处于暂停状态，可以直接确认，无需额外等待。
        # 命令应答时若已看到异步 STOPPED，可直接确认，不再额外等待。
        # 只有返回时仍活动且状态已经暂停才走快速确认；状态缺失时另行等待。
        if interrupted.active and interrupted.state.upper() == "STOPPED":
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED,
                "interrupt command succeeded and target is stopped",
                command_sent=True,
                target_paused=True,
            )

        # 否则在有限时间内观察暂停，命令送达不等于暂停已经发生。
        # 若命令返回时尚未停下，短时间等待异步停止信号。
        # 等待超时保留已送达证据，不升级为暂停已确认。
        try:
            # 确认走会话服务的状态等待，不重复发送中断，也不把超时当作已暂停。
            stopped = await self.debug_service.wait_for_stop(
                target.session_task_id,
                timeout_seconds=_INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS,
            )
        # 确认阶段失败后仍保留命令已发送；界面不能把此类事件当成发送失败。
        except Exception as exc:
            # 等待失败仍保留已接受的动作。
            # 调用方可以从原因判断是否需要刷新目标状态。
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.INTERRUPT_SENT,
                "interrupt command succeeded but stop was not confirmed: "
                f"{self._error_message(exc)}",
                command_sent=True,
            )

        # 等待可能得到退出等终态；只有仍活动且 STOPPED 才算确认。
        # 等待也可能因退出或失败结束，只有活动且 STOPPED 才计为确认。
        # 记录保留送达和停下两个维度，便于区分故障环节。
        paused = stopped.active and stopped.state.upper() == "STOPPED"
        # 若等待结束的原因是目标退出，仍仅能证明中断已发出，不能写成确认暂停。
        return self._finish_event(
            event,
            (
                DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED
                if paused
                else DebugCoordinationEventOutcome.INTERRUPT_SENT
            ),
            (
                "interrupt command succeeded and target is stopped"
                if paused
                else f"interrupt command succeeded but target reached {stopped.state}"
            ),
            command_sent=True,
            target_paused=paused,
        )

    @staticmethod
    # 事件记录不可变，用替换生成最终版本，同时保留源事件标识。
    def _finish_event(
        event: DebugCoordinationEventRecord,
        outcome: DebugCoordinationEventOutcome,
        message: str,
        *,
        command_sent: bool = False,
        target_paused: bool = False,
    ) -> DebugCoordinationEventRecord:
        # 最终记录保留触发身份和发生时间，替换处理时间及独立结果标记。
        return replace(
            event,
            processed_at=utc_now(),
            outcome=outcome,
            command_sent=command_sent,
            target_paused=target_paused,
            message=message,
        )

    @staticmethod
    # 活动资格同时包含已注册会话和平台任务 RUNNING，不能只看 MI 的字符串。
    def _is_active_state(state: DebugSessionStateResponse) -> bool:
        return state.active and state.task_status == TaskStatus.RUNNING

    @staticmethod
    def _rule_response(rule: DebugCoordinationRule) -> DebugCoordinationRuleResponse:
        # 对外投影使用稳定规则 ID 与分区 ID；后续操作不依赖 UI 当前排序。
        return DebugCoordinationRuleResponse(
            rule_id=rule.id,
            group_id=rule.group_id,
            source_partition_id=rule.source_partition_id,
            source_breakpoint_number=rule.source_breakpoint_number,
            target_partition_id=rule.target_partition_id,
            action=rule.action,
            created_at=rule.created_at,
        )

    @staticmethod
    def _event_response(event: DebugCoordinationEventRecord) -> DebugCoordinationEventResponse:
        # 事件响应分别保留命中时间、处理时间、命令发送和目标暂停，方便定位联动延迟。
        return DebugCoordinationEventResponse(
            event_id=event.id,
            source_event_id=event.source_event_id,
            group_id=event.group_id,
            rule_id=event.rule_id,
            source_partition_id=event.source_partition_id,
            source_session_task_id=event.source_session_task_id,
            target_partition_id=event.target_partition_id,
            target_session_task_id=event.target_session_task_id,
            breakpoint_number=event.breakpoint_number,
            occurred_at=event.occurred_at,
            processed_at=event.processed_at,
            outcome=event.outcome,
            command_sent=event.command_sent,
            target_paused=event.target_paused,
            message=event.message,
        )

    # 先取消订阅，再向原事件循环投递未完成任务的取消。
    # 不在关闭线程直接操作另一个循环中的协程。
    def close(self) -> None:
        # 先禁止接收新任务并退订，再对已捕获的在途协程发出取消。
        # 关闭方法同步返回，不承担等待协程完成的职责。
        with self._event_lock:
            # 先标记关闭，使已调度但尚未调用的事件回调看到关闭后立即返回。
            self._closed = True
            if self.event_bus is not None and self._event_subscription_id is not None:
                self.event_bus.unsubscribe(self._event_subscription_id)
            self._event_subscription_id = None
            loop = self._event_loop
            # 拿到在途任务快照后才放开事件锁；实际取消回到任务所属循环。
            tasks = tuple(self._event_tasks)
            self._event_loop = None
        # 仅在拿到原循环和在途任务时安排取消；已关闭循环无需另外创建执行线程。
        if loop is not None and tasks:

            def cancel_tasks() -> None:
                for task in tasks:
                    task.cancel()

            # 关闭可能由同步依赖清理触发，此时调用线程没有运行中的事件循环。
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            # 只能从任务所属循环安全地发出取消。
            # 关闭发生在其他线程时把操作投递回原循环；已关闭循环可能无法接收。
            if running_loop is loop:
                cancel_tasks()
            else:
                try:
                    loop.call_soon_threadsafe(cancel_tasks)
                except RuntimeError:
                    pass

    @staticmethod
    # 应用错误有专用消息字段；其他异常保留其可读表示供协调事件定位。
    def _error_message(error: Exception) -> str:
        return error.message if isinstance(error, AppError) else str(error)
