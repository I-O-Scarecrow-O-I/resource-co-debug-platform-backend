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


# 在平台选定工程源树上组织依赖分析与预览；真正修复构建由后台任务使用副本。
class DependencyAnalysisService:
    def __init__(
        self,
        workspace_service: WorkspaceService,
    ) -> None:
        # 分析入口从平台取得工程视图；不会为每次预览创建一个可供执行的任务副本。
        self.workspace_service = workspace_service

        # 各阶段的模型只在一次分析请求内传递。
        # 服务不缓存图结果，重新预览会重新读取输入。
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

        # 分析器的可预期故障转换为 API 错误，而不是返回零缺失依赖。
        # 分析异常转为应用错误；找不到 Makefile 是可展示的不支持状态，不能误认为没有遗漏。
        # 编译器给出的分析失败转换为平台可展示错误，不会当作没有缺失项返回。
        try:
            result = self._analyze_project(project_id)
        except DependencyAnalysisError as exc:
            raise AppError(str(exc)) from exc

        # 没有入口文件时明确关闭修复能力，空列表不代表工程没有缺陷。
        # 缺少构建入口时分析仍能返回有意义的工程状态。
        # repair_supported 为假，空图不能解释为验证通过。
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

        # 声明详情、实际依赖和缺失依赖分别形成响应，便于客户端区分证据来源。
        # 四项结果共享同一个选定入口，展示声明、实际与缺边时不会跨 Makefile 混用。
        (
            makefile_relative_path,
            makefile_model,
            actual_dependencies,
            missing_dependencies,
        ) = result

        # 同时保留旧字符串边和结构化声明细节，调用者可选择更合适的展示方式。
        # 同时提供面向旧客户端的边文本和类型化边详情，二者来自同一声明模型。
        # 类型化结果保留普通和顺序边的区别，并附加目标与依赖分类。
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

    # 修复预览只生成补偿后文本。
    # 需要实际生成文件并编译时另走隔离的修复构建任务。
    def repair(
        self,
        project_id: UUID,
    ) -> DependencyRepairResponse:
        """
        B5：生成依赖修复预览。

        不修改 project/source 中的原 Makefile。
        """

        # 预览读取项目源树，返回的是修复文本与建议路径，不生成磁盘产物。
        # 预览从当前项目源重新分析，不复用客户端传入的缺边列表。
        # 原路径只用于读取所选 Makefile；本入口没有向工程源码写回的步骤。
        workspace = self.workspace_service.require_project(
            project_id
        )

        # 先用与分析相同的证据得到缺边，再把结果交给修复器。
        try:
            result = self._analyze_project(project_id)
        except DependencyAnalysisError as exc:
            raise AppError(str(exc)) from exc

        # 无 Makefile 时不能提供预览，即使工程源文件本身存在。
        # 缺少入口时没有可生成的补偿文本，与分析接口可返回空结构的语义不同。
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

        # 读取原文件生成修复内容；响应里的修复文件名是建议路径而非写入凭证。
        # 这里拼回的是读取用的工程路径；响应中的文件标识仍相对工程。
        makefile_path = (
            workspace.source_path
            / makefile_relative_path
        )

        # 调用无副作用的生成方法，和后台构建所用的写入入口区分。
        # generate 不创建文件，后续返回的修复名称仅是建议位置。
        repaired = self.dependency_repair.generate(
            makefile_path=makefile_path,
            missing_dependencies=missing_dependencies,
        )

        # 把预览文本与实际应用的缺边一起交付，客户端无需根据字符串猜测补偿条数。
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

    # 分析与修复预览共享同一文件选择、边提取及检测顺序。
    def _analyze_project(
        self,
        project_id: UUID,
    ):
        """
        B1-B4 的内部统一执行入口。

        analyze() 和 repair() 都复用这一套逻辑。
        """

        # 工程存在性由平台先验证，解析器仅负责该工程目录内的分类。
        workspace = (
            self.workspace_service.require_project(
                project_id
            )
        )

        # B1
        # 项目清单只告诉服务有哪些可能入口；解析与编译器探测都选定同一入口后进行。
        project = self.project_parser.parse(
            workspace.source_path
        )

        # 用缺省结果表示缺少构建入口，调用者按各自契约解释。
        # 缺少 Makefile 时提前结束，不把普通源码目录当作已分析构建。
        # 此 None 表示没有 Makefile，和成功解析到零条缺边的结果保持区别。
        if not project.makefiles:
            return None

        # 按稳定规则选单个 Makefile，避免把多入口项目的目标集合错误混合。
        # 只选择一个 Makefile 作为分析入口；声明边和源码证据以同一入口为基准。
        # 根级 Makefile 具有固定优先级；否则采用已排序清单的第一项。
        makefile_relative_path = (
            self._select_makefile(project)
        )

        makefile_path = (
            workspace.source_path
            / makefile_relative_path
        )

        # B2
        # 声明模型保留目标、前置项类型和配方来源，供实际证据匹配。
        makefile_model = (
            self.makefile_parser.parse(
                makefile_path
            )
        )

        # B3
        # 先从配方和编译器输出推断实际文件边，再与声明图比较。
        # 检测按目标核对普通前置项，不能把经过别的目标可达当作已声明直接边。
        # 实际关系由预处理和有限配方分析得出，不是把声明边改名后再返回。
        actual_dependencies = (
            self.dependency_analyzer.analyze(
                project,
                makefile_model,
            )
        )

        # B4
        # 只把可确认的直接缺边交给补偿阶段，不尝试重写整个依赖拓扑。
        missing_dependencies = (
            self.dependency_detector.detect(
                makefile=makefile_model,
                actual_dependencies=(
                    actual_dependencies
                ),
            )
        )

        # 返回工程相对入口路径供 API 展示；绝对路径只保留在内部模型用于解析。
        return (
            makefile_relative_path,
            makefile_model,
            actual_dependencies,
            missing_dependencies,
        )

    @staticmethod
    # 返回旁路文件的工程相对路径；本方法只计算展示名称。
    # 修复文件预览放在原 Makefile 同级，保留嵌套项目布局。
    def _repaired_makefile_path(
        makefile_relative_path: str,
    ) -> str:
        from pathlib import PurePosixPath

        # 响应路径使用 POSIX 分隔符，即使服务在 Windows 上运行也保持一致。
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
        # 优先使用项目根的标准文件名；没有时按扫描器返回顺序选第一个。
        # 优先匹配根级精确名称，不自动把每个子项目 Makefile 合并成一个图。
        if "Makefile" in project.makefiles:
            return "Makefile"

        return project.makefiles[0]

    @staticmethod
    # 兼容视图显式标出 order-only，避免把不同重建语义的边混在一起。
    def _format_declared_dependencies(
        rules: list[MakeRule],
    ) -> list[str]:
        result = []

        # 普通依赖与仅顺序依赖在兼容字符串中仍用不同标记，避免合并其重建语义。
        # 展示按规则保留正常与顺序边，后者即使不触发重建也不会丢失。
        for rule in rules:
            for prerequisite in rule.normal_prerequisites:
                result.append(
                    f"{rule.target} -> {prerequisite}"
                )
            for prerequisite in rule.order_only_prerequisites:
                result.append(f"{rule.target} -[order-only]-> {prerequisite}")

        return result

    @classmethod
    # 结构化视图区分边类型、端点类型和目标是否由构建生成。
    def _dependency_details(
        cls,
        makefile: MakefileModel,
    ) -> list[DeclaredDependencyDetail]:
        # 结构化详情按规则和依赖逐边产生，生成性标记取自规则目标。
        details: list[DeclaredDependencyDetail] = []
        # 类型推断以本 Makefile 父目录作为相对坐标，不能使用项目根替代子项目位置。
        root = makefile.makefile_path.parent

        # 每条声明边共享其目标分类；一个目标可对应多条不同类型的前置边。
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
    # 源与头文件有配方时直接识别为生成项；普通对象和库保留原分类。
    def _target_kind(cls, rule: MakeRule, root) -> tuple[str, bool]:
        # 带配方的源码和头文件显式标为生成项；对象与库保持普通构建产物分类。
        # 文件类别只根据后缀推断；配方是否存在再用于补充生成标记。
        kind = cls._path_kind(rule.target)
        # 头文件或源文件带配方时视为生成项，即使当前副本里已经存在。
        if kind == "header" and rule.recipes:
            return "generated_header", True
        if kind == "source" and rule.recipes:
            return "generated_source", True
        # 对象和库保留构建产物类别，不走通用生成目标的启发式。
        if kind in {"object", "library"}:
            return kind, False
        # 其余类别只在目标不存在且有具体配方时判断为生成项，模式目标不参与。
        # 其他目标只有带配方、不是模式且路径不存在时才标为生成。
        # 判断根目录是 Makefile 所在目录。
        # 其他类型只有具备配方、不是模式且当前缺失时才标成生成候选。
        generated = (
            bool(rule.recipes)
            and "%" not in rule.target
            and not (root / rule.target).exists()
        )
        return ("generated_other" if generated else kind), generated

    @staticmethod
    # 后缀只用于结果展示分类，不用于决定如何执行文件。
    def _path_kind(value: str) -> str:
        from pathlib import PurePosixPath

        # 文件后缀用于响应展示，不会改变检测器比较边的方式。
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
    # 实际边投影成展示文本，不改变分析器保留的源码来源信息。
    def _format_actual_dependencies(
        dependencies: list[
            ActualDependency
        ],
    ) -> list[str]:
        result = []

        # 输出文本逐条展开实际前置项；来源信息仍保留在内部证据模型。
        for item in dependencies:
            for dependency in item.dependencies:
                result.append(
                    f"{item.target} -> {dependency}"
                )

        return result

    @staticmethod
    # 缺失边沿用同一箭头表达，供分析和补偿预览对照。
    def _format_missing_dependencies(
        dependencies: list[
            MissingDependency
        ],
    ) -> list[str]:
        # 缺边文本与其他边使用同一目标方向，内容不表示编译已成功。
        return [
            f"{item.target} -> {item.dependency}"
            for item in dependencies
        ]
