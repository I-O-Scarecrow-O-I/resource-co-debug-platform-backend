from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


# 结构化边标明类型与出处，兼容文本边仍可由同一个声明模型投影。
class DeclaredDependencyDetail(BaseModel):
    # 每条详情对应一条声明边，不包含根据源码推断的边。
    # 普通依赖与顺序依赖的语义必须在序列化时保留。
    target: str
    dependency: str
    # 仅排序的前置项不会因文件变化触发重建，客户端需要单独展示。
    dependency_type: Literal["normal", "order_only"]
    # 文件类型用于分类展示，generated 表示目标规则的推断标记。
    target_kind: str
    dependency_kind: str
    # 生成标记描述目标是否由配方产生，不是文件是否已经存在的断言。
    # 生成标志是从目标与配方形状推断的候选，不表示已经产出文件。
    generated: bool
    # 此类型只描述声明来源；编译器与配方观察到的实际证据在另一列表。
    source: Literal["makefile"] = "makefile"


# 三组边分别表示声明、实际观察与差异，不能用同一个计数互相替代。
class DependencyAnalysisResponse(BaseModel):
    project_id: UUID
    # 兼容字段保留字符串边列表，详情为可按类别筛选的结构化数据。
    declared_dependencies: list[str]
    dependency_details: list[DeclaredDependencyDetail] = Field(default_factory=list)
    # 实际边和缺边分别提供；缺边是与声明图比较得到的子集证据。
    actual_dependencies: list[str]
    missing_dependencies: list[str]
    # 不支持修复可能只是缺少构建入口，不能从空缺失列表推断分析成功。
    # repair_supported 用于区分可生成修复预览与未找到 Makefile 的情况。
    repair_supported: bool
    note: str


# 预览是完整文本与计划路径，没有声明任何文件已写入。
class DependencyRepairResponse(BaseModel):
    # 预览包含原文件位置及生成后的内容，不代表已修改项目或完成构建。
    project_id: UUID

    # 原 Makefile 在项目中的相对路径
    # 相对位置保留子目录，使预览可正确定位所选构建入口。
    original_makefile: str

    # 将来真正构建时使用的修复文件名
    # 预览返回建议文件名，不承诺磁盘已有该文件或可以下载。
    repaired_makefile: str

    # 实际补偿了哪些依赖
    # 补偿边来源是当前分析，不能当作编译器已验证修复有效的结果。
    applied_dependencies: list[str]

    # 修复后的完整 Makefile，用于前端预览
    # 完整文本用于比较原配方与追加规则；本接口不执行其中的命令。
    repaired_content: str

    note: str


# 实际修复构建以工程为输入，选填目标由平台作为 Make 参数传递。
class DependencyRepairBuildRequest(BaseModel):
    # 修复构建请求只指定项目及可选目标；实际命令与修复文件由服务生成。
    project_id: UUID
    # 未选目标时保留 Makefile 的默认目标语义。
    target: str | None = None
    # 缺省超时使用任务服务设置；该请求不自行执行工具或发布产物。
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
    )
