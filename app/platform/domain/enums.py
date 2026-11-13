from enum import StrEnum


class ProjectStatus(StrEnum):
    # 这些值是稳定持久化与 API 标识，改变拼写会影响已有工程记录。
    UPLOADED = "UPLOADED"
    # 只有达到 READY 的清单在重启时作为可用工程装载。
    READY = "READY"
    INVALID = "INVALID"
    ARCHIVED = "ARCHIVED"


# 模块名称是注册和 HTTP 契约使用的稳定字符串，任务来源不能从显示名推断。
class BackendModuleName(StrEnum):
    CODE_GENERATION = "code_generation"
    CO_DEBUG = "co_debug"
    VULNERABILITY = "vulnerability"
    RISK_WARNING = "risk_warning"


# 任务类型用于生命周期之外的业务分派，例如选择可用于调试的成功 BUILD。
# 相同模块下的补全、修复和重构可拥有不同的任务类型。
class TaskType(StrEnum):
    CODE_GENERATION = "CODE_GENERATION"
    CODE_REPAIR = "CODE_REPAIR"
    CODE_REFACTOR = "CODE_REFACTOR"
    # 构建与调试分开建任务，使构建产物可独立作为调试来源。
    BUILD = "BUILD"
    DEBUG = "DEBUG"
    # 实验运行单个策略；比较任务保留一对策略结果与汇总指标。
    SCHEDULE_EXPERIMENT = "SCHEDULE_EXPERIMENT"
    SCHEDULE_COMPARISON = "SCHEDULE_COMPARISON"
    DEPENDENCY_ANALYSIS = "DEPENDENCY_ANALYSIS"
    # 频发缺陷和高危检查共用检测集成，但前端按任务类型展示。
    VULNERABILITY_SCAN = "VULNERABILITY_SCAN"
    RISK_CHECK = "RISK_CHECK"


# 平台状态描述整个托管任务，而非某个外部子进程状态。
# 运行中的取消请求与已经收尾的 CANCELLED 是不同状态。
class TaskStatus(StrEnum):
    # 排队尚未认领，不要求已有工作区或进程。
    PENDING = "PENDING"
    # 运行状态期间可能已收到取消，真正终态由后台收尾提交。
    RUNNING = "RUNNING"
    # 终态表示不再接受新执行；产物是否公开另由发布标记判定。
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# 对外使用小写操作名，平台在创建任务时映射到对应任务类型。
class CodeGenerationOperation(StrEnum):
    COMPLETION = "completion"
    REPAIR = "repair"
    REFACTOR = "refactor"


# 策略标识随计划与比较结果保存，防止仅从时间快慢猜测实际使用了哪套分配。
class SchedulerStrategy(StrEnum):
    FIFO_BASELINE = "FIFO_BASELINE"
    RESOURCE_AWARE = "RESOURCE_AWARE"

