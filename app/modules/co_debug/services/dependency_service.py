from uuid import UUID

from app.modules.co_debug.dependency.dependency_analyzer import (
    DependencyAnalyzer,
)
from app.modules.co_debug.dependency.dependency_detector import (
    DependencyDetector,
)
from app.modules.co_debug.dependency.makefile_parser import (
    MakefileParser,
)
from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakeRule,
    MissingDependency,
    ProjectModel,
)
from app.modules.co_debug.dependency.project_parser import (
    ProjectParser,
)
from app.modules.co_debug.schemas.dependencies import (
    DependencyAnalysisResponse,
)
from app.platform.services.workspace_service import (
    WorkspaceService,
)


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

    def analyze(
        self,
        project_id: UUID,
    ) -> DependencyAnalysisResponse:

        # A模块负责根据 project_id 找到正式工作区
        workspace = self.workspace_service.require_project(
            project_id
        )

        # B1：工程解析
        project = self.project_parser.parse(
            workspace.source_path
        )

        if not project.makefiles:
            return DependencyAnalysisResponse(
                project_id=project_id,
                declared_dependencies=[],
                actual_dependencies=[],
                missing_dependencies=[],
                repair_supported=False,
                note="No Makefile was found in the project source directory.",
            )

        # 当前第一版选择主 Makefile
        makefile_relative_path = (
            self._select_makefile(project)
        )

        makefile_path = (
            workspace.source_path
            / makefile_relative_path
        )

        # B2：Makefile 声明依赖
        makefile_model = (
            self.makefile_parser.parse(
                makefile_path
            )
        )

        # B3：实际源码依赖
        actual_dependencies = (
            self.dependency_analyzer.analyze(
                project
            )
        )

        # B4：缺失依赖检测
        missing_dependencies = (
            self.dependency_detector.detect(
                makefile=makefile_model,
                actual_dependencies=actual_dependencies,
            )
        )

        return DependencyAnalysisResponse(
            project_id=project_id,
            declared_dependencies=self._format_declared_dependencies(
                makefile_model.rules
            ),
            actual_dependencies=self._format_actual_dependencies(
                actual_dependencies
            ),
            missing_dependencies=self._format_missing_dependencies(
                missing_dependencies
            ),
            repair_supported=True,
            note=(
                f"Analyzed {makefile_relative_path}; "
                f"found {len(missing_dependencies)} missing dependencies."
            ),
        )

    def _select_makefile(
        self,
        project: ProjectModel,
    ) -> str:
        """
        第一版优先使用工程根目录下的主 Makefile。
        """

        preferred_names = (
            "Makefile",
            "makefile",
            "GNUmakefile",
        )

        for name in preferred_names:
            if name in project.makefiles:
                return name

        return project.makefiles[0]

    def _format_declared_dependencies(
        self,
        rules: list[MakeRule],
    ) -> list[str]:

        items = {
            f"{rule.target} -> {dependency}"
            for rule in rules
            for dependency in rule.prerequisites
        }

        return sorted(items)

    def _format_actual_dependencies(
        self,
        dependencies: list[ActualDependency],
    ) -> list[str]:

        items = {
            f"{item.target} -> {dependency}"
            for item in dependencies
            for dependency in item.dependencies
        }

        return sorted(items)

    def _format_missing_dependencies(
        self,
        dependencies: list[MissingDependency],
    ) -> list[str]:

        items = {
            f"{item.target} -> {item.dependency}"
            for item in dependencies
        }

        return sorted(items)