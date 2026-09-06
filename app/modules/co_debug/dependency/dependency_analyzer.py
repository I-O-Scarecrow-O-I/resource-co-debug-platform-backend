from pathlib import Path
import subprocess

from app.modules.co_debug.dependency.models import (
    ActualDependency,
    ProjectModel,
)


class DependencyAnalyzer:
    """
    B3 源码实际依赖分析器。

    当前使用 GCC -MM 获取项目级头文件依赖。
    """

    def analyze(
        self,
        project: ProjectModel,
    ) -> list[ActualDependency]:

        root = project.source_root

        include_dirs = self._collect_include_dirs(
            project
        )

        results: list[ActualDependency] = []

        for source_file in project.source_files:

            dependency = self._analyze_source(
                root=root,
                source_file=source_file,
                include_dirs=include_dirs,
            )

            results.append(dependency)

        return results

    def _collect_include_dirs(
        self,
        project: ProjectModel,
    ) -> list[str]:

        include_dirs: set[str] = set()

        for header_file in project.header_files:

            parent = Path(header_file).parent

            if str(parent) != ".":
                include_dirs.add(
                    parent.as_posix()
                )

        include_dirs.add(".")

        return sorted(include_dirs)

    def _analyze_source(
        self,
        root: Path,
        source_file: str,
        include_dirs: list[str],
    ) -> ActualDependency:

        command = [
            "gcc",
            "-MM",
            source_file,
        ]

        for include_dir in include_dirs:
            command.extend(
                ["-I", include_dir]
            )

        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"依赖分析失败: {source_file}\n"
                f"{result.stderr}"
            )

        target, dependencies = (
            self._parse_gcc_output(
                result.stdout
            )
        )

        return ActualDependency(
            target=target,
            source_file=source_file,
            dependencies=dependencies,
        )

    def _parse_gcc_output(
        self,
        output: str,
    ) -> tuple[str, list[str]]:

        output = output.replace(
            "\\\n",
            " ",
        ).strip()

        if ":" not in output:
            raise ValueError(
                f"无法解析 GCC 依赖输出: {output}"
            )

        target_part, dependency_part = output.split(
            ":",
            1,
        )

        target = target_part.strip()

        dependencies = (
            dependency_part.split()
        )

        return target, dependencies