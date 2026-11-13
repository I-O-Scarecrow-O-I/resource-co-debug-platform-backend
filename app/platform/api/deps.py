import threading
import weakref
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import Settings
from app.core.config import get_settings as load_settings
from app.modules.co_debug.debug.events import DebugSessionEventBus
from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
)
from app.modules.co_debug.services.debug_comparison_summary_service import (
    DebugComparisonSummaryService,
)
from app.modules.co_debug.services.debug_coordination_service import (
    DebugCoordinationService,
)
from app.modules.co_debug.services.debug_coordination_store import (
    DebugCoordinationStore,
)
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.modules.co_debug.services.dependency_service import DependencyAnalysisService
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.repair_build_service import (
    DependencyRepairBuildService,
)
from app.modules.co_debug.services.schedule_comparison_service import ScheduleComparisonService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.modules.co_debug.services.task_service import CoDebugTaskService
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService

# 弱引用保留对曾经创建的任务服务的观察，不强制延长已清理实例的生命周期。
# 弱引用集合用于跟踪可能仍在执行的任务服务。
# 即使缓存已清，外部仍持有的实例也能参与资源关闭判断。
_task_services: weakref.WeakSet[TaskService] = weakref.WeakSet()
# 独立的表锁只保护实例集合；任务具体状态仍由各自服务维护。
_task_services_lock = threading.Lock()


# 依赖入口与应用装配复用同一个设置提供者，避免模块各自解释环境变量。
def get_settings() -> Settings:
    return load_settings()


# 任务日志和任务表使用同一数据库路径，各服务持有独立连接。
@lru_cache
def get_log_service() -> TaskLogService:
    # 日志和任务记录取同一个数据库配置，逻辑上仍由独立服务各自管理连接。
    # 替换某个依赖时要考虑仍运行的任务可能在写日志。
    settings = get_settings()
    # 日志的保留行数是单任务限制，由设置提供给日志服务执行。
    return TaskLogService(
        max_lines=settings.max_log_lines_per_task,
        database_path=settings.task_database_path,
    )


# 工作区索引随服务实例保留，不在每次请求重新扫描磁盘。
@lru_cache
# 恢复工程目录的成本由共享 WorkspaceService 承担，不在每个请求内扫描存储根。
def get_workspace_service() -> WorkspaceService:
    # 平台通过一个已配置的存储根加载工作区；模块适配层不创建自己的项目索引。
    return WorkspaceService(storage_root=get_settings().storage_root)


# 任务存储为生命周期、产物资格和占用记录提供同一个持久化来源。
@lru_cache
def get_task_store() -> TaskStore:
    # 缓存实例避免每个 API 调用都重复打开数据库或运行迁移。
    return TaskStore(database_path=get_settings().task_database_path)


# 运行器实例由多个任务模块复用；具体进程句柄随每次执行单独管理。
@lru_cache
def get_process_runner() -> ProcessRunner:
    # 各模块可以共享运行器对象，因为子进程和管道状态由单次执行调用拥有。
    return ProcessRunner()


# 规划器不需要数据库连接，可直接复用同一实例进行不同工作负载规划。
@lru_cache
def get_scheduler_service() -> SchedulerService:
    return SchedulerService()


# 调度执行复用平台运行器，取消、日志及进程树回收走同一实现。
@lru_cache
def get_schedule_execution_service() -> ScheduleExecutionService:
    # 策略执行和普通任务使用相同进程清理能力，避免在不同入口产生不同取消语义。
    return ScheduleExecutionService(process_runner=get_process_runner())


# 指标服务只计算口径和阈值，调用方提供历史或实际测量数据。
@lru_cache
def get_metric_service() -> AcceptanceMetricService:
    return AcceptanceMetricService()


# 每次装配比较器时注入策略、执行和指标三方，测试可覆盖这些依赖。
def get_schedule_comparison_service(
    scheduler_service: Annotated[SchedulerService, Depends(get_scheduler_service)],
    execution_service: Annotated[
        ScheduleExecutionService,
        Depends(get_schedule_execution_service),
    ],
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ScheduleComparisonService:
    # 比较服务的配置不再查找全局对象；使用本次解析好的三项依赖。
    # 比较服务通过依赖注入组合规划、执行与指标计算。
    # 轮次的工作区并非由这些工厂提前共享创建。
    return ScheduleComparisonService(
        scheduler_service=scheduler_service,
        execution_service=execution_service,
        metric_service=metric_service,
    )


# 平台共享任务入口统一绑定持久化、工作区、日志和进程运行能力。
@lru_cache
def get_task_service() -> TaskService:
    # 工厂只建立服务及依赖关系；旧任务恢复在应用生命周期的 startup 内触发。
    task_service = TaskService(
        workspace_service=get_workspace_service(),
        task_store=get_task_store(),
        log_service=get_log_service(),
        process_runner=get_process_runner(),
        default_timeout_seconds=get_settings().default_task_timeout_seconds,
    )
    # 缓存被替换后旧服务仍可能有活跃后台任务，关闭前也要检查它。
    # 实例对外返回前先登记在资源守卫中。
    # 关闭连接时需要看仍存活的服务，而不是只检查缓存里的最新实例。
    with _task_services_lock:
        _task_services.add(task_service)
    # 追踪集合使用弱引用；模块或 worker 仍拥有引用时，清除缓存也不能让实例立即消失。
    return task_service


# 历史汇总读取通用任务服务，不重新执行调度策略。
def get_debug_comparison_summary_service(
    task_service: Annotated[TaskService, Depends(get_task_service)],
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> DebugComparisonSummaryService:
    # 历史比较只需要通用任务读取和指标计算，不要求存在活跃调试会话。
    return DebugComparisonSummaryService(
        task_service=task_service,
        metric_service=metric_service,
    )


# 协同调试任务适配器复用已有平台与调度依赖，不单独管理存储。
def get_co_debug_task_service(
    task_service: Annotated[TaskService, Depends(get_task_service)],
    scheduler_service: Annotated[SchedulerService, Depends(get_scheduler_service)],
    schedule_execution_service: Annotated[
        ScheduleExecutionService,
        Depends(get_schedule_execution_service),
    ],
    schedule_comparison_service: Annotated[
        ScheduleComparisonService,
        Depends(get_schedule_comparison_service),
    ],
) -> CoDebugTaskService:
    # 此处构造的是模块门面；实际调度命令由其注入的执行服务处理。
    return CoDebugTaskService(
        task_service=task_service,
        scheduler_service=scheduler_service,
        schedule_execution_service=schedule_execution_service,
        schedule_comparison_service=schedule_comparison_service,
    )


# 业务适配器持有任务服务引用，清除平台实例时同时失效相应适配器缓存。
def clear_task_service_cache() -> None:
    # 延迟导入业务依赖清理函数，平台组装模块时不建立反向的顶层导入链。
    # 导入放在重置入口内部，避免平台依赖与模块依赖在导入时相互构造。
    # 两种适配器可能各自缓存同一个平台任务服务的引用。
    from app.modules.code_generation.deps import clear_code_generation_task_service_cache
    from app.modules.vulnerability.deps import clear_vulnerability_task_service_cache

    # 先失效引用旧平台服务的模块缓存，再清掉平台入口；这一步本身不关闭执行器。
    clear_code_generation_task_service_cache()
    clear_vulnerability_task_service_cache()
    # 缓存失效不等同于异步停止。
    # 此前已经交出的服务实例仍需通过其生命周期流程结束运行。
    get_task_service.cache_clear()


# 主动关闭共享资源前要求所有仍存活的任务服务已退出其工作。
def _ensure_task_services_can_close(resource_name: str) -> None:
    # 在弱引用表的锁内检查仍活跃的服务；失去缓存引用不表示运行线程已退出。
    with _task_services_lock:
        # 在锁内取出当前仍不能释放资源的实例。
        # 守卫覆盖外部继续持有的对象，不依赖缓存容量作为活跃性指标。
        active_services = [
            task_service
            for task_service in _task_services
            if not task_service.can_close_resources()
        ]
    # 有任何服务仍可能使用共享资源时拒绝提前关闭，缓存仍可由正常关停路径撤销。
    if active_services:
        raise RuntimeError(f"task service must be shut down before clearing its {resource_name}")


# 正常生命周期可只清缓存；连接是否已无任务使用由关闭调用者协调。
def clear_log_service_cache(*, close: bool = True) -> None:
    # close=False 适用于生命周期已单独安排延迟关闭的情况，不提前断开活动连接。
    if close:
        _ensure_task_services_can_close("log service")
    # 先清依赖方再丢弃日志入口的缓存，避免新调用重用绑定旧日志服务的适配器。
    clear_task_service_cache()
    # 只关闭已有缓存对象，不为了清理而新建一个连接。
    if close and get_log_service.cache_info().currsize:
        get_log_service().close()
    # 移除缓存后后续装配可重新绑定资源，现有实例的生命周期不因此被回收。
    get_log_service.cache_clear()


# 任务存储被替换时同时重置依赖其任务及会话数据的协调组件。
def clear_task_store_cache(*, close: bool = True) -> None:
    if close:
        _ensure_task_services_can_close("task store")
    # 平台任务服务和调试协调共享任务存储。
    # 重置协调服务时先让它停止使用订阅和存储，再处理各自的缓存。
    clear_task_service_cache()
    # 先停止协调订阅，再关闭协调数据库，避免在关闭的连接上处理新事件。
    if get_debug_coordination_service.cache_info().currsize:
        get_debug_coordination_service().close()
    # 协调服务的缓存引用与协调存储的缓存引用分别撤销。
    # 关闭上层服务不表示下层已不需要显式收尾。
    get_debug_coordination_service.cache_clear()
    # 协调存储单独持有数据库连接；关闭协调订阅后才释放它。
    if get_debug_coordination_store.cache_info().currsize:
        get_debug_coordination_store().close()
    get_debug_coordination_store.cache_clear()
    # 调试门面同时持有持久任务索引，因此任务存储切换后必须丢弃旧门面。
    # 普通调试服务也持有任务存储，不能只清理协调服务而留着它。
    # 下一次查询应从依赖工厂取当前存储实例。
    get_debug_service.cache_clear()
    # 调试服务缓存依赖任务存储；旧实例失效后，新请求会重新绑定当前存储。
    if close and get_task_store.cache_info().currsize:
        get_task_store().close()
    # 如果关闭由生命周期延后执行，此处只撤销入口引用，不提前终止旧 worker 使用的连接。
    get_task_store.cache_clear()


# 依赖分析读取平台已有的项目工作区索引，不建立模块自己的上传目录。
@lru_cache
def get_dependency_service() -> DependencyAnalysisService:
    return DependencyAnalysisService(workspace_service=get_workspace_service())


# 活跃会话索引必须共享，否则 API 无法取得后台任务注册的通道。
@lru_cache
def get_debug_session_manager(
) -> DebugSessionManager:
    # 管理器状态只存在于本进程；重启后持久任务仍可查询，但无法恢复旧 GDB 命令通道。
    return DebugSessionManager()


# 单一事件总线连接 MI 会话发布端与协调订阅端。
@lru_cache
def get_debug_session_event_bus() -> DebugSessionEventBus:
    # 总线提供会话身份与协同事件连接，不接管进程或任务状态。
    return DebugSessionEventBus()


# 创建会话与查询会话必须使用相同的注册表；发布事件走同一个共享总线。
@lru_cache
def get_interactive_debug_service(
) -> InteractiveDebugService:
    # 交互执行、调试查询和协同规则需要看到同一份会话注册表。
    # 事件发布也指向协调服务订阅的同一个总线。
    return InteractiveDebugService(
        task_service=get_task_service(),
        session_manager=(
            get_debug_session_manager()
        ),
        event_publisher=get_debug_session_event_bus(),
    )


# API 调试服务同时依赖持久化任务与活跃会话索引，便于处理已结束会话。
@lru_cache
def get_debug_service(
) -> DebugSessionService:
    # 同一个门面同时支持已登记的实时会话与持久任务历史；两类数据不会互相替代。
    # 只把任务存储与会话管理器接到查询层，不跨线程暴露具体 GDB 会话。
    # 需要创建或执行交互进程时再委托 InteractiveDebugService。
    return DebugSessionService(
        task_store=get_task_store(),
        session_manager=(
            get_debug_session_manager()
        ),
        interactive_service=(
            get_interactive_debug_service()
        ),
    )


# 协调状态保存在平台数据库路径，连接由协调存储自己管理。
@lru_cache
def get_debug_coordination_store() -> DebugCoordinationStore:
    # 协调表使用自己的连接；数据库文件共享不意味着可以依赖另一连接的事务。
    return DebugCoordinationStore(database_path=get_settings().task_database_path)


# 组命令和断点动作使用同一个会话索引，不为协作组另建调试进程。
@lru_cache
def get_debug_coordination_service() -> DebugCoordinationService:
    # 组状态、平台任务记录、实时通道和断点事件由不同组件提供，在此统一注入。
    # 协调服务在已存组规则与实时会话之间衔接。
    # 它从共享事件总线消费断点通知，而不是为每个 HTTP 请求创建独立观察者。
    return DebugCoordinationService(
        store=get_debug_coordination_store(),
        task_store=get_task_store(),
        session_manager=get_debug_session_manager(),
        debug_service=get_debug_service(),
        event_bus=get_debug_session_event_bus(),
    )

# 修复构建只需已有的任务执行能力，路由每次装配无需持久模块状态。
def get_dependency_repair_build_service(
) -> DependencyRepairBuildService:
    return DependencyRepairBuildService(
        task_service=get_task_service(),
    )
