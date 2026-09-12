from pathlib import Path

from app.modules.co_debug.dependency.models import ProjectModel


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

MAKEFILE_NAMES = {
    "Makefile",
    "makefile",
    "GNUmakefile",
}

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

    def parse(
        self,
        source_root: str | Path,
    ) -> ProjectModel:

        root = Path(source_root).resolve()

        if not root.exists():
            raise FileNotFoundError(
                f"工程源码目录不存在: {root}"
            )

        if not root.is_dir():
            raise NotADirectoryError(
                f"工程源码路径不是目录: {root}"
            )

        model = ProjectModel(
            source_root=root
        )

        for file_path in root.rglob("*"):

            if not file_path.is_file():
                continue

            if self._should_ignore(
                file_path,
                root,
            ):
                continue

            relative_path = (
                file_path
                .relative_to(root)
                .as_posix()
            )

            self._classify_file(
                file_path=file_path,
                relative_path=relative_path,
                model=model,
            )

        self._sort_files(model)

        return model

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

    def _classify_file(
        self,
        file_path: Path,
        relative_path: str,
        model: ProjectModel,
    ) -> None:

        file_name = file_path.name
        suffix = file_path.suffix.lower()

        if file_name in MAKEFILE_NAMES:
            model.makefiles.append(relative_path)

        elif suffix in SOURCE_EXTENSIONS:
            model.source_files.append(
                relative_path
            )

        elif suffix in HEADER_EXTENSIONS:
            model.header_files.append(
                relative_path
            )

        elif suffix == ".o":
            model.object_files.append(
                relative_path
            )

        elif suffix == ".a":
            model.static_libraries.append(
                relative_path
            )

        elif suffix == ".so":
            model.shared_libraries.append(
                relative_path
            )

        else:
            model.other_files.append(
                relative_path
            )

    def _sort_files(
        self,
        model: ProjectModel,
    ) -> None:

        model.source_files.sort()
        model.header_files.sort()
        model.makefiles.sort()
        model.object_files.sort()
        model.static_libraries.sort()
        model.shared_libraries.sort()
        model.other_files.sort()