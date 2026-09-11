import asyncio
from pathlib import Path

from app.core.errors import AppError
from app.modules.co_debug.dependency.dependency_analyzer import (
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
from app.modules.co_debug.dependency.project_parser import (
    ProjectParser,
)
from app.modules.co_debug.schemas.dependencies import (
    DependencyRepairBuildRequest,
)
from app.platform.domain.enums import (
    BackendModuleName,
    TaskType,
)
from app.platform.domain.task import TaskRecord
from app.platform.services.task_execution import (
    PreparedProcess,
    TaskPreparationContext,
)
from app.platform.services.task_service import (
    TaskService,
)


class DependencyRepairBuildService:
    """
    B6：依赖补偿后的自动重新编译。

    B模块负责：
        B1-B5
        生成最终 make 命令

    A模块负责：
        Task
        Workspace
        ProcessRunner
        Log
        Timeout
        Cancellation
        Artifact
    """

    def __init__(
        self,
        task_service: TaskService,
    ) -> None:
        self.task_service = task_service

        self.project_parser = ProjectParser()
        self.makefile_parser = MakefileParser()
        self.dependency_analyzer = DependencyAnalyzer()
        self.dependency_detector = DependencyDetector()
        self.dependency_repair = DependencyRepair()

    async def create_task(
        self,
        request: DependencyRepairBuildRequest,
    ) -> TaskRecord:

        async def prepare(
            context: TaskPreparationContext,
        ) -> PreparedProcess:
            """
            A创建Workspace以后调用。
            """

            context.raise_if_cancelled()

            context.log(
                "dependency repair build preparation started"
            )

            context.report_progress(
                15,
                "analyzing project dependencies",
            )

            # 当前B1-B5都是同步代码。
            # 放入线程，避免阻塞TaskService事件循环。
            prepared = await asyncio.to_thread(
                self._prepare_workspace,
                context.workspace,
                request.target,
            )

            context.raise_if_cancelled()

            context.report_progress(
                45,
                "dependency repair completed",
            )

            context.log(
                "Makefile.repaired generated"
            )

            return prepared
        return await self.task_service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=request.project_id,
            task_type=TaskType.BUILD,
            prepare=prepare,
            timeout_seconds=request.timeout_seconds,
            metadata={
                 "operation": "dependency_repair_build",
                     },
            artifacts_on_success=True,
            )

    def _prepare_workspace(
        self,
        workspace: Path,
        target: str | None,
    ) -> PreparedProcess:
        """
        在A提供的独立Task Workspace中完成B1-B5，
        并生成B6最终构建命令。
        """

        # B1
        project = self.project_parser.parse(
            workspace
        )

        if not project.makefiles:
            raise AppError(
                "No Makefile was found in the project."
            )

        makefile_relative_path = (
            self._select_makefile(project.makefiles)
        )

        makefile_path = (
            workspace / makefile_relative_path
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
                project
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

        # B5
        repaired = (
            self.dependency_repair.write(
                makefile_path=makefile_path,
                missing_dependencies=(
                    missing_dependencies
                ),
            )
        )

        if repaired.repaired_makefile is None:
            raise AppError(
                "Failed to generate repaired Makefile."
            )

        # Makefile如果在子目录，
        # 让A把进程cwd切换到Makefile所在目录。
        work_dir_path = (
            Path(makefile_relative_path).parent
        )

        work_dir = (
            "."
            if str(work_dir_path) == "."
            else work_dir_path.as_posix()
        )

        # B6只负责产生命令，不执行。
        command = [
            "make",
            "-f",
            repaired.repaired_makefile.name,
        ]

        if target:
            command.append(target)

        return PreparedProcess(
            command=command,
            work_dir=work_dir,
        )

    @staticmethod
    def _select_makefile(
        makefiles: list[str],
    ) -> str:
        """
        优先选择项目根目录Makefile；
        没有时选择扫描到的第一个。
        """

        if "Makefile" in makefiles:
            return "Makefile"

        return makefiles[0]