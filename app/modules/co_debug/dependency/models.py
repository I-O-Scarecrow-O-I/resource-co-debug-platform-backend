from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ProjectModel:
    """
    B 模块内部工程视图。

    source_root 指向 A 模块 ProjectWorkspace.source_path。
    文件列表统一保存为相对于 source_root 的 POSIX 路径。
    """

    source_root: Path

    source_files: list[str] = field(default_factory=list)
    header_files: list[str] = field(default_factory=list)
    makefiles: list[str] = field(default_factory=list)

    object_files: list[str] = field(default_factory=list)
    static_libraries: list[str] = field(default_factory=list)
    shared_libraries: list[str] = field(default_factory=list)

    other_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MakeRule:
    target: str
    prerequisites: list[str] = field(default_factory=list)
    line_number: int = 0


@dataclass(slots=True)
class MakefileModel:
    makefile_path: Path
    rules: list[MakeRule] = field(default_factory=list)


@dataclass(slots=True)
class ActualDependency:
    target: str
    source_file: str
    dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MissingDependency:
    target: str
    dependency: str
    source_file: str

@dataclass(slots=True)
class RepairedBuildConfig:
    original_makefile: Path

    # 预览模式下为 None；
    # 真正写入文件后保存 Makefile.repaired 的路径。
    repaired_makefile: Path | None = None

    # 修复后的完整 Makefile 文本。
    content: str = ""

    # 本次真正补偿了哪些依赖。
    applied_dependencies: list[MissingDependency] = field(
        default_factory=list
    )