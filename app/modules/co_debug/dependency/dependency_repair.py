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

    # 先生成修复文本与原文件关联，本方法不向磁盘写入结果。
    def generate(
        self,
        makefile_path: str | Path,
        missing_dependencies: list[MissingDependency],
    ) -> RepairedBuildConfig:
        """
        生成修复后的 Makefile 内容，但不写入磁盘。
        """

        # 解析原文件真实路径后读取 UTF-8 文本；预览阶段不会写入文件。
        original_makefile = self._validate_makefile(
            makefile_path
        )

        # 修复文件必须保留可用的配方文本；读取失败不会用容错内容继续生成。
        original_content = original_makefile.read_text(
            encoding="utf-8"
        )

        # 同一目标可能经不同分析路径报告重复头文件，先归并成集合。
        grouped_dependencies = self._group_dependencies(
            missing_dependencies
        )

        # 补偿规则追加在现有文件尾部，不需要重写已有目标及其配方。
        repaired_content = self._append_compensation_rules(
            original_content,
            grouped_dependencies,
        )

        # 生成结果保留调用方给出的缺边列表副本；写入位置在预览时为空。
        # 返回列表只复制容器；缺失依赖记录的值未在生成补偿文本时修改。
        return RepairedBuildConfig(
            original_makefile=original_makefile,
            repaired_makefile=None,
            content=repaired_content,
            applied_dependencies=missing_dependencies.copy(),
        )

    # 写入只生成旁路 Makefile，执行与产物发布仍由平台管理。
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

        # 磁盘写入复用与预览相同的生成逻辑，区别只在生成后提交目标文件。
        result = self.generate(
            makefile_path=makefile_path,
            missing_dependencies=missing_dependencies,
        )

        original_makefile = result.original_makefile

        # 缺省输出与原 Makefile 同目录，保留配方的工作目录语义。
        if output_path is None:
            repaired_makefile = (
                original_makefile.parent / "Makefile.repaired"
            )
        else:
            # 显式输出先解析绝对位置再比较，避免把路径别名误认为不同文件。
            repaired_makefile = Path(output_path).resolve()

        # 按解析后的路径拒绝覆盖原输入，即使调用方用不同拼写指定。
        if repaired_makefile == original_makefile:
            raise ValueError(
                "repaired Makefile must not overwrite "
                "the original Makefile"
            )

        # 调用方负责提供归属正确的输出目录；这里创建父目录并写入生成内容。
        repaired_makefile.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # 只有成功写入后才设置修复路径，调用者不会启动不存在的构建入口。
        repaired_makefile.write_text(
            result.content,
            encoding="utf-8",
        )

        # 只有写入成功后才给结果附加修复文件路径，失败由上层按构建任务处理。
        result.repaired_makefile = repaired_makefile

        return result

    @staticmethod
    # 输入须能解析为存在的普通文件，避免目录等对象进入文本处理。
    def _validate_makefile(
        makefile_path: str | Path,
    ) -> Path:
        # 检查的是解析后的路径，让原文件与输出路径可在同一坐标系比较。
        path = Path(makefile_path).resolve()

        # 区分缺少路径与目标不是普通文件，调用方可据异常定位输入问题。
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

        # 按目标聚合并去重，避免同一缺边在补偿规则中反复出现。
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

        # 无缺失边时沿用原文本，不额外生成空的补偿规则。
        if not grouped_dependencies:
            return original_content

        # 需要增加规则时保留原文本作为前缀，不重排原文件中的目标。
        content = original_content

        # 保证追加规则从新的一行开始。
        # 文本没有结尾换行时先补上，防止生成标记被接到原文件最后一条配方中。
        if content and not content.endswith("\n"):
            content += "\n"

        content += "\n"
        # 此处的标记属于输出 Makefile，不是对构建结果成功的声明。
        content += f"{self.REPAIR_COMMENT}\n"

        # 目标和依赖均排序以便复查；追加规则只声明前置项，原配方不被替换。
        for target in sorted(grouped_dependencies):
            dependencies = " ".join(
                sorted(grouped_dependencies[target])
            )

            # 新规则只声明前置条件，不附配方；执行方法仍由原规则决定。
            content += (
                f"{target}: {dependencies}\n"
            )

        return content