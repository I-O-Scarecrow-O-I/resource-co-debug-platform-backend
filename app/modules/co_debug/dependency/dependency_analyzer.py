import os
import subprocess
from pathlib import Path, PurePosixPath

from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakefileModel,
    ProjectModel,
)


class DependencyAnalyzer:
    """
    B3 源码实际依赖分析器。

    使用 GCC -MM 获取项目级头文件依赖。

    依赖路径统一转换为相对于当前 Makefile
    所在目录的 POSIX 路径，从而能够与
    Makefile 中声明的 target/prerequisite
    使用相同的路径基准。
    """

    def analyze(
        self,
        project: ProjectModel,
        makefile: MakefileModel | None = None,
    ) -> list[ActualDependency]:

        analysis_root = self._analysis_root(
            project=project,
            makefile=makefile,
        )

        include_dirs = self._collect_include_dirs(
            project=project,
            analysis_root=analysis_root,
        )

        results: list[ActualDependency] = []

        for source_file in project.source_files:

            normalized_source = self._relative_to_analysis_root(
                project=project,
                analysis_root=analysis_root,
                path=source_file,
            )

            target = self._resolve_target(
                source_file=normalized_source,
                makefile=makefile,
            )

            dependency = self._analyze_source(
                root=analysis_root,
                source_file=normalized_source,
                target=target,
                include_dirs=include_dirs,
            )

            results.append(dependency)

        return results

    @staticmethod
    def _analysis_root(
        project: ProjectModel,
        makefile: MakefileModel | None,
    ) -> Path:
        if makefile is None:
            return project.source_root

        return makefile.makefile_path.resolve().parent

    def _collect_include_dirs(
        self,
        project: ProjectModel,
        analysis_root: Path,
    ) -> list[str]:

        include_dirs: set[str] = {"."}

        for header_file in project.header_files:

            normalized_header = (
                self._relative_to_analysis_root(
                    project=project,
                    analysis_root=analysis_root,
                    path=header_file,
                )
            )

            parent = PurePosixPath(
                normalized_header
            ).parent

            include_dirs.add(
                parent.as_posix()
            )

        return sorted(include_dirs)

    @staticmethod
    def _relative_to_analysis_root(
        project: ProjectModel,
        analysis_root: Path,
        path: str,
    ) -> str:

        absolute_path = (
            project.source_root
            / Path(path)
        ).resolve()

        relative_path = os.path.relpath(
            absolute_path,
            analysis_root,
        )

        return Path(
            relative_path
        ).as_posix()

    def _resolve_target(
        self,
        source_file: str,
        makefile: MakefileModel | None,
    ) -> str:
        if makefile is None:
            return None

        normalized_source = self._normalize_path(
            source_file
        )

        if makefile is not None:

            for rule in makefile.rules:

                prerequisites = {
                    self._normalize_path(
                        prerequisite
                    )
                    for prerequisite
                    in rule.prerequisites
                }

                if normalized_source in prerequisites:
                    return self._normalize_path(
                        rule.target
                    )

        return (
            PurePosixPath(
                normalized_source
            )
            .with_suffix(".o")
            .as_posix()
        )

    def _analyze_source(
        self,
        root: Path,
        source_file: str,
        target: str,
        include_dirs: list[str],
    ) -> ActualDependency:
        

        command = [
            "gcc",
            "-MM",
        ]
        if target is not None:
            command.extend(
                [
                    "-MT",
                    target,
                ]
            )
        command.append(
            source_file
        )

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

        parsed_target, dependencies = (
            self._parse_gcc_output(
                result.stdout
            )
        )

        return ActualDependency(
            target=parsed_target,
            source_file=source_file,
            dependencies=dependencies,
        )

    @staticmethod
    def _normalize_path(
        value: str,
    ) -> str:

        value = value.strip()

        if value.startswith("./"):
            value = value[2:]

        return PurePosixPath(
            value
        ).as_posix()

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