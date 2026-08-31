from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.platform.api.deps import get_workspace_service
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.projects import ProjectResponse
from app.platform.services.workspace_service import WorkspaceService

router = APIRouter()


@router.post("", response_model=ApiResponse[ProjectResponse])
async def upload_project(
    archive: Annotated[UploadFile, File(description="Zip archive containing source project")],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    name: Annotated[str | None, Form()] = None,
) -> ApiResponse[ProjectResponse]:
    workspace = await workspace_service.create_from_archive(archive=archive, display_name=name)
    return ApiResponse.ok(ProjectResponse.from_workspace(workspace))


@router.get("", response_model=ApiResponse[list[ProjectResponse]])
async def list_projects(
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[list[ProjectResponse]]:
    return ApiResponse.ok(
        [ProjectResponse.from_workspace(project) for project in workspace_service.list_projects()]
    )


@router.get("/{project_id}", response_model=ApiResponse[ProjectResponse])
async def get_project(
    project_id: UUID,
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[ProjectResponse]:
    project = workspace_service.require_project(project_id)
    return ApiResponse.ok(ProjectResponse.from_workspace(project))

