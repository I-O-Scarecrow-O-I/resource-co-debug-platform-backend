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
        # 平台任务服务由调用方注入，依赖组件不自行接管进程或任务存储。
        self.task_service = task_service

        self.project_parser = ProjectParser()
        # 解析器、分析器和修复器分别持有不同阶段的职责。
        # 准备阶段的中间模型留在本次调用内，平台只消费准备结果。
        self.makefile_parser = MakefileParser()
        self.dependency_analyzer = DependencyAnalyzer()
        self.dependency_detector = DependencyDetector()
        self.dependency_repair = DependencyRepair()

    # 提交 BUILD 任务；提交时不修改工程源树，也不执行 Makefile。
    async def create_task(
        self,
        request: DependencyRepairBuildRequest,
    ) -> TaskRecord:

        # 平台调用准备器时已经创建任务副本。
        # 耗时依赖探测不在 HTTP 提交路径发生。
        async def prepare(
            context: TaskPreparationContext,
        ) -> PreparedProcess:
            """
            A创建Workspace以后调用。
            """

            # 在扫描副本前检查取消，避免已经终止的任务继续生成修复文件。
            context.raise_if_cancelled()

            # 这条日志记录的是准备流程开始，不是子进程已启动。
            # 在这里失败时应从解析或扫描诊断定位，而非找 Make 的退出码。
            context.log(
                "dependency repair build preparation started"
            )

            # 阶段进度表示分析已经开始，尚未执行修复构建命令。
            context.report_progress(
                15,
                "analyzing project dependencies",
            )

            # 当前B1-B5都是同步代码。
            # 放入线程，避免阻塞TaskService事件循环。
            # 线程操作只接收本次副本和目标。
            # 协程取消不等于同步线程立即停止，返回后仍需检查取消。
            prepared = await asyncio.to_thread(
                self._prepare_workspace,
                context.workspace,
                request.target,
            )

            # 线程完成准备不等于还允许构建；再次检查取消再交还命令。
            context.raise_if_cancelled()

            # 进入下一阶段前修复文件已经准备好，但不能把此事件当作 Make 成功。
            context.report_progress(
                45,
                "dependency repair completed",
            )

            # 该日志可用于把随后 Make 自身的失败与之前的依赖分析失败区分开。
            context.log(
                "Makefile.repaired generated"
            )

            # 只有检查完取消才交付 PreparedProcess，平台不消费同步线程内部的临时模型。
            return prepared
        # 交给平台记录进程状态及退出码，仅成功后开放产物下载。
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
        # 在副本中重新扫描而不使用前端传来的依赖列表，确保分析对象与编译输入一致。
        project = self.project_parser.parse(
            workspace
        )

        # 没有可解析的 Makefile 时不创建猜测规则，准备阶段直接失败。
        if not project.makefiles:
            raise AppError(
                "No Makefile was found in the project."
            )

        # 扫描结果可能含多个入口，只选一个 Makefile 推导图并生成旁路文件。
        makefile_relative_path = (
            self._select_makefile(project.makefiles)
        )

        # 扫描器提供相对路径，后续解析和修复均以当前任务副本为输入。
        makefile_path = (
            workspace / makefile_relative_path
        )

        # B2
        # 从选定的入口抽取声明规则，后续检测不混入其他 Makefile 的目标。
        # 解析前仍保留原 Makefile 路径。
        # 后面的分析使用该规则文件所在目录解释相对输入。
        makefile_model = (
            self.makefile_parser.parse(
                makefile_path
            )
        )

        # B3
        # 用项目文件和构建规则一起推导实际依赖，保留编译目标关联。
        # 实际边由编译器和配方证据共同给出，不由声明图反推缺失项。
        actual_dependencies = (
            self.dependency_analyzer.analyze(
                project,
                makefile_model,
            )
        )

        # B4
        # 检测仅比较声明与实际依赖；写入由下一个步骤统一进行。
        # 只把没有声明的正常依赖交给修复器，已存在的目标和配方不重写。
        missing_dependencies = (
            self.dependency_detector.detect(
                makefile=makefile_model,
                actual_dependencies=(
                    actual_dependencies
                ),
            )
        )

        # B5
        # 把补偿规则写到旁路文件，保留副本里的原 Makefile 供对照。
        # 即使没有缺失依赖，修复器仍可产生旁路文件供统一构建流程使用。
        repaired = (
            self.dependency_repair.write(
                makefile_path=makefile_path,
                missing_dependencies=(
                    missing_dependencies
                ),
            )
        )

        # 没有可执行的修复文件时不退回原 Makefile，否则无法确认构建使用了补偿规则。
        # 写入结果没有路径就没有可调用入口，此时不退回原始构建以制造成功。
        if repaired.repaired_makefile is None:
            raise AppError(
                "Failed to generate repaired Makefile."
            )

        # Makefile如果在子目录，
        # 让A把进程cwd切换到Makefile所在目录。
        # 配方中的相对输入以 Makefile 父目录为基准，副本根可能更高一层。
        work_dir_path = (
            Path(makefile_relative_path).parent
        )

        # 工作目录返回相对于任务副本的路径；具体绝对路径在平台执行阶段确定。
        work_dir = (
            "."
            if str(work_dir_path) == "."
            else work_dir_path.as_posix()
        )

        # B6只负责产生命令，不执行。
        # 向平台交付修复文件名而非工作区绝对路径，让运行器用已校验的 cwd 定位。
        command = [
            "make",
            "-f",
            repaired.repaired_makefile.name,
        ]

        # 未指定目标时沿用 Makefile 默认目标；指定值作为一个 argv 参数交给平台。
        if target:
            command.append(target)

        # 准备结果不包含退出状态，产物是否公开取决于后续实际命令成功。
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

        # 根级精确名的优先级独立于排序。
        # 没有这个候选时沿用扫描清单首项，并不尝试运行全部构建入口。
        if "Makefile" in makefiles:
            return "Makefile"

        return makefiles[0]