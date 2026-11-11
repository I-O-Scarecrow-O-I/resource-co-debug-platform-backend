from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# 一项作业描述一次可重复的批处理调试，不直接接收任意 GDB 命令列表。
class DebugBatchJob(BaseModel):
    # 拒绝未知字段，避免清单拼写错误被当成默认配置。
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    executable_path: str = Field(min_length=1)
    # 断点支持函数名或文件:行号；完整路径仍由任务入口解析。
    breakpoint: str = Field(
        pattern=r"^(?:[A-Za-z_][A-Za-z0-9_:]*|[A-Za-z0-9_./-]+:[1-9][0-9]*)$"
    )
    args: list[str] = Field(default_factory=list)
    # 预估值用于生成 FIFO 计划，优化轮改用基线实测时长。
    estimated_ms: int = Field(default=1000, ge=1)


class DebugBatchWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # 同组作业共享工作目录；至少两个任务才有组内耗时差可比。
    work_dir: str = "."
    jobs: list[DebugBatchJob] = Field(min_length=2)


class DebugWorkloadManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 版本号固定当前解释方式；少于三组可用于联调，不等于正式指标达标。
    version: Literal[1]
    workloads: list[DebugBatchWorkload] = Field(min_length=1, max_length=3)


class DebugComparisonRequest(BaseModel):
    # 工程始终提供清单；若指定成功的构建任务，运行产物从构建快照取。
    project_id: UUID
    build_task_id: UUID | None = None
    manifest_path: str = "debug-workloads.json"
    # 省略核心列表时由调度服务选择当前进程可用的核心。
    core_ids: list[int] | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
