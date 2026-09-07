from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.platform.domain.enums import CodeGenerationOperation


class CodeGenerationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_llm_calls: int | None = Field(default=None, ge=1, le=100)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100)
    max_input_tokens: int | None = Field(default=None, ge=1, le=120_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=24_000)
    max_seconds: int | None = Field(default=None, ge=1, le=300)


class CodeGenerationTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    operation: CodeGenerationOperation
    instruction: str = Field(min_length=1, max_length=10_000)
    target_files: list[str] = Field(min_length=1, max_length=50)
    budget: CodeGenerationBudget = Field(default_factory=CodeGenerationBudget)
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)

    @field_validator("target_files")
    @classmethod
    def require_relative_target_files(cls, value: list[str]) -> list[str]:
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

class NaturalCCRunRequest(BaseModel):
    goal: str = Field(min_length=1)
    target_files: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class NaturalCCHealthStatus(BaseModel):
    status: Literal["available", "unavailable"]
    detail: str


class CodeGenerationCapabilities(BaseModel):
    provider: str
    operations: list[str]
    execution_route_available: bool
    detail: str
