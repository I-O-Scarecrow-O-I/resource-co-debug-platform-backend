from uuid import UUID

from app.core.errors import AppError
from app.modules.co_debug.dependency.dependency_analyzer import (
    DependencyAnalysisError,
    DependencyAnalyzer,
)
from app.modules.co_debug.dependency.dependency_detector import (
    DependencyDetector,
)
from app.modules.co_debug.dependency.dependency_repair import (
    DependencyRepair,
)
from app.modules.co_debug.dependency.makefile_parser import (
    MakefileParser,
)
from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakefileModel,
    MakeRule,
    MissingDependency,
    ProjectModel,
)
from app.modules.co_debug.dependency.project_parser import (
    ProjectParser,
)
from app.modules.co_debug.schemas.dependencies import (
    DeclaredDependencyDetail,
    DependencyAnalysisResponse,
    DependencyRepairResponse,
)
from app.platform.services.workspace_service import WorkspaceService


class DependencyAnalysisService:
    def __init__(
        self,
        workspace_service: WorkspaceService,
    ) -> None:
        self.workspace_service = workspace_service

        self.project_parser = ProjectParser()
        self.makefile_parser = MakefileParser()
        self.dependency_analyzer = DependencyAnalyzer()
        self.dependency_detector = DependencyDetector()

        # B5
        self.dependency_repair = DependencyRepair()

    def analyze(
        self,
        project_id: UUID,
    ) -> DependencyAnalysisResponse:
        """
        B1-B4：只分析，不修改工程。
        """

        try:
            result = self._analyze_project(project_id)
        except DependencyAnalysisError as exc:
            raise AppError(str(exc)) from exc

        if result is None:
            return DependencyAnalysisResponse(
                project_id=project_id,
                declared_dependencies=[],
                actual_dependencies=[],
                missing_dependencies=[],
                repair_supported=False,
                note=(
                    "No Makefile was found in the "
                    "project source directory."
                ),
            )

        (
            makefile_relative_path,
            makefile_model,
            actual_dependencies,
            missing_dependencies,
        ) = result

        return DependencyAnalysisResponse(
            project_id=project_id,
            declared_dependencies=(
                self._format_declared_dependencies(
                    makefile_model.rules
                )
            ),
            dependency_details=self._dependency_details(makefile_model),
            actual_dependencies=(
                self._format_actual_dependencies(
                    actual_dependencies
                )
            ),
            missing_dependencies=(
                self._format_missing_dependencies(
                    missing_dependencies
                )
            ),
            repair_supported=True,
            note=(
                f"Analyzed {makefile_relative_path}; "
                f"found {len(missing_dependencies)} "
                "missing dependencies."
            ),
        )

    def repair(
        self,
        project_id: UUID,
    ) -> DependencyRepairResponse:
        """
        B5：生成依赖修复预览。

        不修改 project/source 中的原 Makefile。
        """

        workspace = self.workspace_service.require_project(
            project_id
        )

        try:
            result = self._analyze_project(project_id)
        except DependencyAnalysisError as exc:
            raise AppError(str(exc)) from exc

        if result is None:
            raise ValueError(
                "No Makefile was found in the "
                "project source directory."
            )

        (
            makefile_relative_path,
            _,
            _,
            missing_dependencies,
        ) = result

        makefile_path = (
            workspace.source_path
            / makefile_relative_path
        )

        repaired = self.dependency_repair.generate(
            makefile_path=makefile_path,
            missing_dependencies=missing_dependencies,
        )

        return DependencyRepairResponse(
            project_id=project_id,
            original_makefile=makefile_relative_path,
            repaired_makefile=(
                self._repaired_makefile_path(
                    makefile_relative_path
                )
            ),
            applied_dependencies=(
                self._format_missing_dependencies(
                    repaired.applied_dependencies
                )
            ),
            repaired_content=repaired.content,
            note=(
                f"Generated repair preview with "
                f"{len(repaired.applied_dependencies)} "
                "dependency compensations. "
                "The original Makefile was not modified."
            ),
        )

    def _analyze_project(
        self,
        project_id: UUID,
    ):
        """
        B1-B4 的内部统一执行入口。

        analyze() 和 repair() 都复用这一套逻辑。
        """

        workspace = (
            self.workspace_service.require_project(
                project_id
            )
        )

        # B1
        project = self.project_parser.parse(
            workspace.source_path
        )

        if not project.makefiles:
            return None

        makefile_relative_path = (
            self._select_makefile(project)
        )

        makefile_path = (
            workspace.source_path
            / makefile_relative_path
        )

        # B2
        makefile_model = (
            self.makefile_parser.parse(
                makefile_path
            )
        )

        # B3
        actual_dependencies = (
            self.dependency_analyzer.analyze(
                project,
                makefile_model,
            )
        )

        # B4
        missing_dependencies = (
            self.dependency_detector.detect(
                makefile=makefile_model,
                actual_dependencies=(
                    actual_dependencies
                ),
            )
        )

        return (
            makefile_relative_path,
            makefile_model,
            actual_dependencies,
            missing_dependencies,
        )

    @staticmethod
    def _repaired_makefile_path(
        makefile_relative_path: str,
    ) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(
            makefile_relative_path
        )

        return (
            path.parent
            / "Makefile.repaired"
        ).as_posix()

    # ↓↓↓ 你原来已有的这些方法继续保留 ↓↓↓

    @staticmethod
    def _select_makefile(
        project: ProjectModel,
    ) -> str:
        if "Makefile" in project.makefiles:
            return "Makefile"

        return project.makefiles[0]

    @staticmethod
    def _format_declared_dependencies(
        rules: list[MakeRule],
    ) -> list[str]:
        result = []

        for rule in rules:
            for prerequisite in rule.normal_prerequisites:
                result.append(
                    f"{rule.target} -> {prerequisite}"
                )
            for prerequisite in rule.order_only_prerequisites:
                result.append(f"{rule.target} -[order-only]-> {prerequisite}")

        return result

    @classmethod
    def _dependency_details(
        cls,
        makefile: MakefileModel,
    ) -> list[DeclaredDependencyDetail]:
        details: list[DeclaredDependencyDetail] = []
        root = makefile.makefile_path.parent

        for rule in makefile.rules:
            target_kind, generated = cls._target_kind(rule, root)
            for dependency_type, prerequisites in (
                ("normal", rule.normal_prerequisites),
                ("order_only", rule.order_only_prerequisites),
            ):
                for dependency in prerequisites:
                    details.append(
                        DeclaredDependencyDetail(
                            target=rule.target,
                            dependency=dependency,
                            dependency_type=dependency_type,
                            target_kind=target_kind,
                            dependency_kind=cls._path_kind(dependency),
                            generated=generated,
                        )
                    )
        return details

    @classmethod
    def _target_kind(cls, rule: MakeRule, root) -> tuple[str, bool]:
        kind = cls._path_kind(rule.target)
        if kind == "header" and rule.recipes:
            return "generated_header", True
        if kind == "source" and rule.recipes:
            return "generated_source", True
        if kind in {"object", "library"}:
            return kind, False
        generated = (
            bool(rule.recipes)
            and "%" not in rule.target
            and not (root / rule.target).exists()
        )
        return ("generated_other" if generated else kind), generated

    @staticmethod
    def _path_kind(value: str) -> str:
        from pathlib import PurePosixPath

        suffix = PurePosixPath(value).suffix.lower()
        if suffix in {".h", ".hh", ".hpp"}:
            return "header"
        if suffix in {".c", ".cc", ".cpp", ".cxx"}:
            return "source"
        if suffix in {".o", ".obj", ".lo"}:
            return "object"
        if suffix in {".a", ".so", ".dylib"}:
            return "library"
        return "other"

    @staticmethod
    def _format_actual_dependencies(
        dependencies: list[
            ActualDependency
        ],
    ) -> list[str]:
        result = []

        for item in dependencies:
            for dependency in item.dependencies:
                result.append(
                    f"{item.target} -> {dependency}"
                )

        return result

    @staticmethod
    def _format_missing_dependencies(
        dependencies: list[
            MissingDependency
        ],
    ) -> list[str]:
        return [
            f"{item.target} -> {item.dependency}"
            for item in dependencies
        ]
