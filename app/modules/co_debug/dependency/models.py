from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ProjectModel:
    """
    B 模块内部工程视图。

    source_root 指向 A 模块 ProjectWorkspace.source_path。
    文件列表统一保存为相对于 source_root 的 POSIX 路径。
    """

    # 根目录由平台工作区提供；模型只保存分类后的相对路径，不拥有目录生命周期。
    source_root: Path

    # 源码和头文件用于编译器依赖探测，Makefile 文件名用于入口选择。
    source_files: list[str] = field(default_factory=list)
    header_files: list[str] = field(default_factory=list)
    makefiles: list[str] = field(default_factory=list)

    # 构建产物和未知文件也被索引，供图展示和后续入口筛选。
    object_files: list[str] = field(default_factory=list)
    static_libraries: list[str] = field(default_factory=list)
    shared_libraries: list[str] = field(default_factory=list)

    other_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
# 一条规则对应一个目标，多目标声明在解析时已拆开。
class MakeRule:
    target: str
    # 正常边影响重建，order-only 只表达顺序；检测时不可合并使用。
    normal_prerequisites: list[str] = field(default_factory=list)
    order_only_prerequisites: list[str] = field(default_factory=list)
    recipes: list[str] = field(default_factory=list)
    # 行号对应逻辑规则的首个物理行，便于把配方证据指回构建文件。
    line_number: int = 0

    @property
    # 兼容视图返回新列表，调用方不能用它原地改变任何边组。
    def prerequisites(self) -> list[str]:
        """Compatibility view for code that needs every prerequisite."""
        return [*self.normal_prerequisites, *self.order_only_prerequisites]

    @prerequisites.setter
    # 旧接口整体赋值时按正常边解释并清空仅排序的集合。
    def prerequisites(self, values: list[str]) -> None:
        self.normal_prerequisites = values
        self.order_only_prerequisites = []

@dataclass(slots=True)
# 构建文件路径决定工作目录，变量表保留分析所需的已知赋值。
class MakefileModel:
    makefile_path: Path
    rules: list[MakeRule] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
# 实际边可以来自编译器或 Makefile 配方，source_file 保留两类出处。
class ActualDependency:
    target: str
    source_file: str
    dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
# 一条缺失记录只描述一个目标与前置项之间的边；修复器之后再归组。
class MissingDependency:
    target: str
    dependency: str
    source_file: str

@dataclass(slots=True)
# 预览文本与实际产物路径分开，调用者可区分计算结果和成功写入。
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
