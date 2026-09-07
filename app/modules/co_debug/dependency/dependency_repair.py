from pathlib import Path

from app.modules.co_debug.dependency.models import (
    MissingDependency,
    RepairedBuildConfig,
)


class DependencyRepair:
    """
    B5：Makefile 缺失依赖补偿。

    支持两种使用方式：

    1. generate()
       只生成修复后的 Makefile 内容，不修改磁盘文件。
       适合依赖修复预览。

    2. write()
       将修复后的内容写入指定文件。
       适合后续在 BUILD task workspace 中生成
       Makefile.repaired。
    """

    REPAIR_COMMENT = "# Auto-generated dependency compensation"

    def generate(
        self,
        makefile_path: str | Path,
        missing_dependencies: list[MissingDependency],
    ) -> RepairedBuildConfig:
        """
        生成修复后的 Makefile 内容，但不写入磁盘。
        """

        original_makefile = self._validate_makefile(
            makefile_path
        )

        original_content = original_makefile.read_text(
            encoding="utf-8"
        )

        grouped_dependencies = self._group_dependencies(
            missing_dependencies
        )

        repaired_content = self._append_compensation_rules(
            original_content,
            grouped_dependencies,
        )

        return RepairedBuildConfig(
            original_makefile=original_makefile,
            repaired_makefile=None,
            content=repaired_content,
            applied_dependencies=missing_dependencies.copy(),
        )

    def write(
        self,
        makefile_path: str | Path,
        missing_dependencies: list[MissingDependency],
        output_path: str | Path | None = None,
    ) -> RepairedBuildConfig:
        """
        生成修复后的 Makefile，并写入磁盘。

        默认输出到原 Makefile 同目录下：

            Makefile.repaired

        原始 Makefile 不会被修改。
        """

        result = self.generate(
            makefile_path=makefile_path,
            missing_dependencies=missing_dependencies,
        )

        original_makefile = result.original_makefile

        if output_path is None:
            repaired_makefile = (
                original_makefile.parent / "Makefile.repaired"
            )
        else:
            repaired_makefile = Path(output_path).resolve()

        if repaired_makefile == original_makefile:
            raise ValueError(
                "repaired Makefile must not overwrite "
                "the original Makefile"
            )

        repaired_makefile.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        repaired_makefile.write_text(
            result.content,
            encoding="utf-8",
        )

        result.repaired_makefile = repaired_makefile

        return result

    @staticmethod
    def _validate_makefile(
        makefile_path: str | Path,
    ) -> Path:
        path = Path(makefile_path).resolve()

        if not path.exists():
            raise FileNotFoundError(
                f"Makefile does not exist: {path}"
            )

        if not path.is_file():
            raise ValueError(
                f"Makefile path is not a file: {path}"
            )

        return path

    @staticmethod
    def _group_dependencies(
        missing_dependencies: list[MissingDependency],
    ) -> dict[str, set[str]]:
        """
        将多个 MissingDependency 按 target 分组。

        例如：

            main.o -> include/add.h
            main.o -> include/config.h

        转换成：

            {
                "main.o": {
                    "include/add.h",
                    "include/config.h",
                }
            }
        """

        grouped: dict[str, set[str]] = {}

        for item in missing_dependencies:
            grouped.setdefault(
                item.target,
                set(),
            ).add(
                item.dependency
            )

        return grouped

    def _append_compensation_rules(
        self,
        original_content: str,
        grouped_dependencies: dict[str, set[str]],
    ) -> str:
        """
        在原 Makefile 文本末尾追加依赖补偿规则。
        """

        if not grouped_dependencies:
            return original_content

        content = original_content

        # 保证追加规则从新的一行开始。
        if content and not content.endswith("\n"):
            content += "\n"

        content += "\n"
        content += f"{self.REPAIR_COMMENT}\n"

        for target in sorted(grouped_dependencies):
            dependencies = " ".join(
                sorted(grouped_dependencies[target])
            )

            content += (
                f"{target}: {dependencies}\n"
            )

        return content