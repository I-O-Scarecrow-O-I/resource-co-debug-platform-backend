from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.platform.api.deps import get_workspace_service
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.projects import ProjectResponse
from app.platform.services.workspace_service import WorkspaceService

router = APIRouter()


# 上传体积限制可能在进入端点前触发；把相同的错误响应形状写入 OpenAPI。
# 接口级 413 说明的是上传请求过大。
# ZIP 展开后超出条目或内容限额则由工作区导入层单独判断。
PROJECT_UPLOAD_RESPONSES = {
    413: {
        "model": ApiResponse[None],
        "description": "Project upload exceeds the configured size limit.",
    }
}


# 归档与可选名称是 multipart 的不同部分；存储不由客户端提供路径。
@router.post(
    "",
    response_model=ApiResponse[ProjectResponse],
    responses=PROJECT_UPLOAD_RESPONSES,
)
async def upload_project(
    archive: Annotated[UploadFile, File(description="Zip archive containing source project")],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    name: Annotated[str | None, Form()] = None,
) -> ApiResponse[ProjectResponse]:
    # 归档与显示名由工作区服务处理，接口仅返回已完成导入的项目。
    # multipart 的展示名称和归档内容分开交给工作区服务。
    # 名称不决定项目目录的位置或后续任务使用的项目身份。
    workspace = await workspace_service.create_from_archive(archive=archive, display_name=name)
    # 导入完成后再序列化快照；中途的解包路径不是可访问的项目资源。
    return ApiResponse.ok(ProjectResponse.from_workspace(workspace))


# 列表反映当前服务已经认可的工程。
# 一个目录即使仍在磁盘上，只要未通过导入或恢复就不会作为正常项目列出。
@router.get("", response_model=ApiResponse[list[ProjectResponse]])
# 列表返回服务当前登记的项目；本端点不会重新扫描或导入磁盘目录。
async def list_projects(
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[list[ProjectResponse]]:
    # 读取项目快照，不向调用者暴露存储中的工作区对象。
    return ApiResponse.ok(
        [ProjectResponse.from_workspace(project) for project in workspace_service.list_projects()]
    )


# 路径段按 UUID 解析，先区分格式错误，再由服务识别合法但不存在的项目。
@router.get("/{project_id}", response_model=ApiResponse[ProjectResponse])
async def get_project(
    project_id: UUID,
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[ProjectResponse]:
    # 缺失工程使用统一业务错误处理，不返回一个空的项目对象。
    # 路径参数使用工程 UUID 而非上传归档名。
    # 同名导入仍是两个可独立查询、运行任务的项目。
    project = workspace_service.require_project(project_id)
    return ApiResponse.ok(ProjectResponse.from_workspace(project))

