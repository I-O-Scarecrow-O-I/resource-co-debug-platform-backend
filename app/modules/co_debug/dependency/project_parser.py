from pathlib import Path

from app.modules.co_debug.dependency.models import ProjectModel

# 这里只枚举依赖分析关心的 C 系列源与头文件；其他文件仍可作为项目证据保留。
SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
}

HEADER_EXTENSIONS = {
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
}

# 名字集合仅控制是否收录构建入口；入口偏好由使用此清单的服务决定。
MAKEFILE_NAMES = {
    "Makefile",
    "makefile",
    "GNUmakefile",
}

# 被忽略的目录按相对路径分段判断，名字碰巧包含同一片段的普通文件不会命中。
IGNORE_DIRECTORIES = {
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
}


class ProjectParser:
    """
    B1 工程解析器。

    扫描 A 模块提供的源码工作目录，
    建立 B 模块内部工程视图。
    """

    # 只建立依赖分析需要的文件视图，输入目录归属已由平台确定。
    def parse(
        self,
        source_root: str | Path,
    ) -> ProjectModel:

        root = Path(source_root).resolve()

        # 区分目录不存在和路径类型错误，让调用者明确定位工程输入。
        if not root.exists():
            raise FileNotFoundError(
                f"工程源码目录不存在: {root}"
            )

        # 非目录不进入遍历；项目扫描需要一个可计算相对路径的共同根。
        if not root.is_dir():
            raise NotADirectoryError(
                f"工程源码路径不是目录: {root}"
            )

        # 模型记录绝对源码根用于解析文件，其内部文件标识统一使用相对路径。
        model = ProjectModel(
            source_root=root
        )

        # 扫描普通文件并排除工具目录内的文件，目录项本身不进入工程模型。
        for file_path in root.rglob("*"):

            # 目录本身不入清单；构建依赖目标基于后续识别的文件及 Makefile 规则。
            if not file_path.is_file():
                continue

            # 忽略检查发生在遍历之后，只决定结果是否收录，不表示剪枝整个目录树。
            if self._should_ignore(
                file_path,
                root,
            ):
                continue

            # 以统一的工程相对路径表示文件，避免在 Make 规则之间比较绝对路径。
            relative_path = (
                file_path
                .relative_to(root)
                .as_posix()
            )

            # 分类不改变路径位置；同名文件在不同子目录仍保持独立标识。
            self._classify_file(
                file_path=file_path,
                relative_path=relative_path,
                model=model,
            )

        self._sort_files(model)

        return model

    # 忽略规则检查路径中任一组件，工具目录下的嵌套文件也不参与分析。
    def _should_ignore(
        self,
        file_path: Path,
        root: Path,
    ) -> bool:

        relative_parts = (
            file_path
            .relative_to(root)
            .parts
        )

        return any(
            part in IGNORE_DIRECTORIES
            for part in relative_parts
        )

    # 优先按准确文件名识别构建入口，再按扩展名归类源、头和构建产物。
    def _classify_file(
        self,
        file_path: Path,
        relative_path: str,
        model: ProjectModel,
    ) -> None:

        # Makefile 按固定文件名识别，其他类型按归一化后缀判断。
        file_name = file_path.name
        suffix = file_path.suffix.lower()

        if file_name in MAKEFILE_NAMES:
            model.makefiles.append(relative_path)

        # 源、头、对象和库分开登记，让后续阶段按证据类型筛选。
        elif suffix in SOURCE_EXTENSIONS:
            model.source_files.append(
                relative_path
            )

        elif suffix in HEADER_EXTENSIONS:
            model.header_files.append(
                relative_path
            )

        # 现有构建产物仅作为目录清单证据，列出它们不代表它们由本次任务产生。
        elif suffix == ".o":
            model.object_files.append(
                relative_path
            )

        elif suffix == ".a":
            model.static_libraries.append(
                relative_path
            )

        # 这是按扩展名分类的文件视图，不检查二进制格式、架构或运行兼容性。
        elif suffix == ".so":
            model.shared_libraries.append(
                relative_path
            )

        # 未知类型仍保留在其他文件列表，不被当作可供编译器分析的源文件。
        else:
            model.other_files.append(
                relative_path
            )

    # 文件分类按路径稳定排序，不依赖文件系统遍历返回的顺序。
    def _sort_files(
        self,
        model: ProjectModel,
    ) -> None:

        # 各分类独立排序，文件系统枚举顺序不会影响后续首选入口及图输出。
        model.source_files.sort()
        model.header_files.sort()
        model.makefiles.sort()
        model.object_files.sort()
        model.static_libraries.sort()
        model.shared_libraries.sort()
        model.other_files.sort()