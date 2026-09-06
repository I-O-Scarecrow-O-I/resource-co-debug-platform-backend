from pathlib import PurePosixPath

from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakefileModel,
    MissingDependency,
)


class DependencyDetector:
    """
    B4 Makefile 缺失依赖检测器。
    """

    def detect(
        self,
        makefile: MakefileModel,
        actual_dependencies: list[ActualDependency],
    ) -> list[MissingDependency]:

        declared_map = self._build_declared_map(
            makefile
        )

        missing_dependencies: list[
            MissingDependency
        ] = []

        for actual in actual_dependencies:

            target = self._normalize_path(
                actual.target
            )

            if target not in declared_map:
                continue

            declared = declared_map[target]

            for dependency in actual.dependencies:

                normalized_dependency = (
                    self._normalize_path(
                        dependency
                    )
                )

                if normalized_dependency not in declared:

                    missing_dependencies.append(
                        MissingDependency(
                            target=target,
                            dependency=normalized_dependency,
                            source_file=actual.source_file,
                        )
                    )

        return missing_dependencies

    def _build_declared_map(
        self,
        makefile: MakefileModel,
    ) -> dict[str, set[str]]:

        declared_map: dict[
            str,
            set[str],
        ] = {}

        for rule in makefile.rules:

            target = self._normalize_path(
                rule.target
            )

            dependencies = {
                self._normalize_path(item)
                for item in rule.prerequisites
            }

            declared_map.setdefault(
                target,
                set(),
            )

            declared_map[target].update(
                dependencies
            )

        return declared_map

    def _normalize_path(
        self,
        value: str,
    ) -> str:

        value = value.strip()

        if value.startswith("./"):
            value = value[2:]

        return PurePosixPath(
            value
        ).as_posix()