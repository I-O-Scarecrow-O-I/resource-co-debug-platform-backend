from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.platform.domain.enums import CodeGenerationOperation


# 各项上游预算可以缺省；显式值分别限制模型调用、工具调用、令牌和时间。
class CodeGenerationBudget(BaseModel):
    # 拒绝未知预算键，以免调用方把未生效的字段误当作资源限制。
    model_config = ConfigDict(extra="forbid")

    # 调用次数与令牌额度分别设置。
    # 某一项仍有余量不表示远端还能越过其他已经耗尽的预算。
    max_llm_calls: int | None = Field(default=None, ge=1, le=100)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100)
    max_input_tokens: int | None = Field(default=None, ge=1, le=120_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=24_000)
    # 这是 Agent 运行预算字段。
    # 它与平台任务的墙钟超时分别提交，后者也覆盖适配器准备和收尾。
    max_seconds: int | None = Field(default=None, ge=1, le=300)


# 公开任务请求只接受已声明字段，运行模型和权限不由任意附加字段控制。
class CodeGenerationTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 项目身份用于定位由平台管理的源树，公开请求不接收任意 workspace 路径。
    project_id: UUID
    operation: CodeGenerationOperation
    # 提交必须给出目标及指令；长度限制只约束输入形状，不证明文件已经存在。
    instruction: str = Field(min_length=1, max_length=10_000)
    # 清单保存客户端要编辑的相对文件名；存在性检查需要任务副本就绪后才能做。
    # 目标数限制独立于指令字数和生成预算。
    target_files: list[str] = Field(min_length=1, max_length=50)
    # 未给预算时生成独立的空预算对象；任务服务会用实际任务期限补齐时间边界。
    budget: CodeGenerationBudget = Field(default_factory=CodeGenerationBudget)
    # 可省略平台超时并采用服务的默认值；显式值必须在公开任务预算范围内。
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)

    # 语法校验同时考虑当前平台与 Windows 路径，副本归属仍要在任务运行时解析。
    @field_validator("target_files")
    @classmethod
    def require_relative_target_files(cls, value: list[str]) -> list[str]:
        # 父目录跳转在两种路径解析器下都拒绝，避免 Unix 接受 Windows 路径片段作为普通文件名。
        for target_file in value:
            path = Path(target_file)
            windows_path = PureWindowsPath(target_file)
            if (
                not target_file
                or path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or any(part == ".." for part in path.parts)
                or any(part == ".." for part in windows_path.parts)
            ):
                raise ValueError("target_files must contain relative paths inside the project")
        return value

# 上游 Agent 运行请求与公开任务模型分开，可由平台附加其协议字段。
# 线程和能力字段属于上游运行契约。
# 它们与平台任务身份分离，不从任意 HTTP 扩展字段透传。
class NaturalCCRunRequest(BaseModel):
    # goal 由适配器把操作约束和指令组合；公开任务请求不直接控制线程和能力对象。
    goal: str = Field(min_length=1)
    target_files: list[str] = Field(default_factory=list)
    # 适配器组装的预算只作为上游协议数据传递。
    # 与公开任务模型分开后，客户端无需直接理解 Agent 内部请求结构。
    budget: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


# 健康对象表示 NaturalCC 应用可达性，不作为模型完成能力的证明。
class NaturalCCHealthStatus(BaseModel):
    # 不可用原因面向界面展示，不携带原始网络或模型请求细节。
    status: Literal["available", "unavailable"]
    detail: str


# 能力描述用于前端展示当前接入操作，和具体运行实例状态分离。
class CodeGenerationCapabilities(BaseModel):
    provider: str
    operations: list[str]
    execution_route_available: bool
    detail: str
