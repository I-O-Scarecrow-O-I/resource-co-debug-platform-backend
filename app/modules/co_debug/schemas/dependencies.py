from uuid import UUID

from pydantic import BaseModel


class DependencyAnalysisResponse(BaseModel):
    project_id: UUID
    declared_dependencies: list[str]
    actual_dependencies: list[str]
    missing_dependencies: list[str]
    repair_supported: bool
    note: str

class DependencyRepairResponse(BaseModel):
    project_id: UUID

    # 原 Makefile 在项目中的相对路径
    original_makefile: str

    # 将来真正构建时使用的修复文件名
    repaired_makefile: str

    # 实际补偿了哪些依赖
    applied_dependencies: list[str]

    # 修复后的完整 Makefile，用于前端预览
    repaired_content: str

    note: str
