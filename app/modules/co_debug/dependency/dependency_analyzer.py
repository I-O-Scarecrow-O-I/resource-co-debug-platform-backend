import shlex
import subprocess
from pathlib import Path

from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakefileModel,
    MakeRule,
    ProjectModel,
)
from app.modules.co_debug.dependency.pattern_rule_resolver import PatternRuleResolver

# 源文件后缀集合用于从 recipe 参数提取编译输入。
# 这只是静态分类，不能证明这个参数代表已经存在的文件。
_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".c++",
}

_OBJECT_SUFFIXES = {
    ".o",
    ".obj",
    ".lo",
}

_LIBRARY_SUFFIXES = {
    ".a",
    ".so",
    ".dylib",
}


class DependencyAnalysisError(RuntimeError):
    """A user-correctable failure while deriving project dependencies."""


class DependencyAnalyzer:
    """
    B3 实际依赖分析器。

    两类证据：
    1. gcc/g++ -MM:
       source -> headers

    2. Makefile recipe:
       source -> object
       object/library -> executable
    """

    # 模式规则仅在寻找依赖应归属的产物时参与。
    # 不让模式解析器替换编译器实际提取的头文件证据。
    def __init__(self) -> None:
        self.pattern_rules = PatternRuleResolver()

    # 将配方提供的目标关系与编译器观察到的源码依赖合并。
    # 所有最终边使用构建入口下相对路径的表达方式。
    def analyze(
        self,
        project: ProjectModel,
        makefile: MakefileModel | None = None,
    ) -> list[ActualDependency]:
        # 按目标建立单一结果项，使配方识别与预处理头文件可以追加同一个节点。
        # 字典插入顺序用于输出稳定性，不表示构建拓扑。
        merged: dict[str, ActualDependency] = {}

        # 先从配方提取编译、归档和链接关系，使后续头文件能挂到真实目标。
        if makefile is not None:
            for dependency in self._analyze_makefile_recipes(
                makefile
            ):
                self._merge_dependency(
                    merged,
                    dependency,
                )

        # 选中子目录 Makefile 时，将其所在目录作为所有编译器调用的工作目录。
        project_root = project.source_root
        analysis_root = (
            makefile.makefile_path.parent
            if makefile is not None
            else project_root
        )
        # 工程范围的头文件目录以被分析 Makefile 的相对坐标传入编译器。
        # 把绝对工程路径混进这组参数会破坏后续目标匹配的坐标约定。
        include_dirs = self._collect_include_dirs(
            project,
            analysis_root,
        )
        # 收集已声明的生成目标，以便为尚未存在的头文件消除路径歧义。
        generated_targets = {
            self._normalize_path(rule.target)
            for rule in makefile.rules
        } if makefile is not None else set()

        # 源码清单来自工程根；当构建入口更深时，只分析可表示在该入口下的文件。
        for source_file in project.source_files:
            source_path = project_root / source_file

            try:
                analyzed_source = source_path.relative_to(
                    analysis_root
                ).as_posix()
            except ValueError:
                # 选中的 Makefile 只负责其所在工程子树。
                continue

            # 全局 include 目录与当前源文件的配方参数结合，按首次出现顺序去重。
            source_include_dirs = list(include_dirs)

            # 当前源的配方 include 参数附在工程级搜索路径之后。
            # 这样可以保留配方声明的信息，又不污染其他源码的分析调用。
            if makefile is not None:
                source_include_dirs.extend(
                    self._recipe_include_dirs_for_source(
                        makefile,
                        analyzed_source,
                    )
                )

            # 只请求编译器生成依赖文本，不通过此步骤执行 Makefile 配方。
            dependency = self._analyze_source(
                root=analysis_root,
                source_file=analyzed_source,
                include_dirs=self._unique(source_include_dirs),
                generated_targets=generated_targets,
            )

            # 如果 recipe 已经告诉我们
            # "build/main.o <- src/main.cpp"，
            # 就把 -MM 找到的 header 也挂到该真实 object target。
            # 这里查的是已归并的 recipe 证据。
            # 能找到上游命令产物时，用该产物接收编译器的依赖，而不是发布两个同源对象节点。
            recipe_target = self._find_recipe_target_for_source(
                merged,
                analyzed_source,
                makefile,
            )

            # 编译器默认对象名仅作回退；配方或模式规则能定位真实目标时以其为准。
            if recipe_target is not None:
                # 重挂载只改变目标键，源文件和头文件集合仍来自本次扫描。
                # 合并时先前 recipe 里识别出的其他输入也会保留。
                dependency = ActualDependency(
                    target=recipe_target,
                    source_file=analyzed_source,
                    dependencies=dependency.dependencies,
                )

            # 归并输出覆盖直接编译和链接边，也保留生成头的声明目标。
            # 未识别的配方不会因此被执行或视为已验证。
            self._merge_dependency(
                merged,
                dependency,
            )

        # 列表保留首次发现目标的顺序。
        # 调用者不可把这个发现顺序解释成依赖图的拓扑构建顺序。
        return list(merged.values())

    # 配方作为字符串分析，从已识别的命令形式抽取关系，不启动 shell。
    def _analyze_makefile_recipes(
        self,
        makefile: MakefileModel,
    ) -> list[ActualDependency]:
        results: list[ActualDependency] = []

        # 按声明顺序解析配方，自动变量必须用当前规则上下文替换。
        # 同一个文本在不同目标下可能引用不同输入或输出。
        for rule in makefile.rules:
            for recipe in rule.recipes:
                # 按所属规则展开自动变量，再判断命令类型。
                # 同一 recipe 片段放到不同规则里可能指向不同目标和先决项。
                command = self._expand_automatic_variables(
                    recipe,
                    rule,
                )

                # 只对可识别的命令片段收集边；普通目录创建等命令不产生数据依赖。
                for command_part in self._split_shell_commands(command):
                    dependency = self._analyze_command(
                        command_part,
                        rule,
                    )

                    # 不认识的片段没有边可记录，但不会中止同一规则中的其他片段。
                    # 这使目录准备与编译连在同一行时仍可保留后者的证据。
                    if dependency is not None:
                        results.append(dependency)

        return results

    # 只处理可识别的工具名；其他命令不产生推断边。
    def _analyze_command(
        self,
        command: str,
        rule: MakeRule,
    ) -> ActualDependency | None:
        # 解析失败或非支持的工具名只跳过当前配方片段，源码扫描仍可继续。
        tokens = self._command_tokens(command)
        if not tokens:
            return None

        # 工具名取路径末段，显式指定工具路径不改变识别规则。
        # 不会因为配方出现某个路径就从该路径启动程序。
        executable = Path(tokens[0]).name

        if executable in {
            "gcc",
            "g++",
            "clang",
            "clang++",
            "cc",
            "c++",
        }:
            return self._analyze_compiler_command(
                tokens,
                rule,
            )

        # 归档工具提供静态库成员边。
        # 单独处理它可避免将库自身当成编译源码或链接输入。
        if executable == "ar":
            return self._analyze_ar_command(
                tokens,
                rule,
            )

        # 直接链接器的输入分类与通过编译器驱动链接时不同。
        # 此分支不会从源码后缀推断编译步骤。
        if executable in {"ld", "gold", "ld.lld"}:
            return self._analyze_linker_command(
                tokens,
                rule,
            )

        # 未建模的工具不报告依赖边。
        # 空结果不能证明该命令没有输入或副作用。
        return None

    # 编译和链接命令的输入类型不同，不能用链接库识别规则推断编译源。
    def _analyze_compiler_command(
        self,
        tokens: list[str],
        rule: MakeRule,
    ) -> ActualDependency | None:
        # 显式输出名可能与声明目标不同，实际依赖按命令真正写出的名称归组。
        output = self._output_argument(tokens)

        # 没有显式输出参数时以声明目标为准，确保检测结果可回溯。
        if output is None:
            output = rule.target

        # 带编译选项的配方只关联源码输入，头文件来自后续预处理分析。
        # 显式编译分支建立产物到源文件的边，头文件仍由另一个编译器扫描阶段提供。
        # 一个命令若带多个源参数，这里记录它们的集合，并用首项标识来源。
        if "-c" in tokens:
            # 这一层的候选来自分词后的后缀特征，并不掌握编译器全部选项的取值规则。
            sources = [
                token
                for token in tokens[1:]
                if self._has_suffix(
                    token,
                    _SOURCE_SUFFIXES,
                )
            ]

            # 没有可识别的源输入时不猜测配方的编译边。
            # 编译器依赖分析仍以扫描得到的源文件为输入。
            if not sources:
                return None

            # 对编译配方保留首个源码作为出处，全部识别到的源码仍可成为前置项。
            # 去重只去掉重复参数，不把识别到的其他源输入丢掉。
            return ActualDependency(
                target=output,
                source_file=sources[0],
                dependencies=self._unique(sources),
            )

        # 编译器作为 linker 使用
        # 非单纯编译的命令按链接输入识别，忽略不属于文件输入的标志。
        # 驱动器作为链接器时，直接编入的源文件也属于输入。
        # 输出项本身排除，避免加入目标对自身的边。
        inputs = [
            token
            for token in tokens[1:]
            if (
                self._has_suffix(token, _OBJECT_SUFFIXES)
                or self._has_suffix(token, _LIBRARY_SUFFIXES)
                or self._has_suffix(token, _SOURCE_SUFFIXES)
            )
            and token != output
        ]

        # 只有明确输入才生成链接记录，不能由输出名反推出隐式库依赖。
        if not inputs:
            return None

        # 链接输入的来源标记是配方行，不是一个可供头文件扫描的源码路径。
        # 使用 Makefile 行号表示链接证据来源，避免把多个输入中的某个文件误当成唯一源。
        return ActualDependency(
            target=output,
            source_file=f"Makefile:{rule.line_number}",
            dependencies=self._unique(inputs),
        )

    # 直接调用链接器时仍绑定输出目标，捕获可识别的对象和库文件依赖。
    def _analyze_linker_command(
        self,
        tokens: list[str],
        rule: MakeRule,
    ) -> ActualDependency | None:
        # 未找到输出选项时回落到规则的目标命名。
        # 这属于解析器的静态归属约定，不是运行产物存在性检查。
        output = self._output_argument(tokens) or rule.target

        # 直接调用链接器只看显式对象与库文件。
        # 以库名选项表达的系统库不在这张文件依赖图中。
        inputs = [
            token
            for token in tokens[1:]
            if (
                self._has_suffix(token, _OBJECT_SUFFIXES)
                or self._has_suffix(token, _LIBRARY_SUFFIXES)
            )
            and token != output
        ]

        if not inputs:
            return None

        # 链接边按当前命令明确列出的文件构成。
        # 通过搜索路径间接找到的库不会由此凭空展开。
        return ActualDependency(
            target=output,
            source_file=f"Makefile:{rule.line_number}",
            dependencies=self._unique(inputs),
        )

    # 归档命令定位首个静态库，再从其后解析对象成员。
    def _analyze_ar_command(
        self,
        tokens: list[str],
        rule: MakeRule,
    ) -> ActualDependency | None:
        archive_index = None

        # 静态库目标从配方参数中定位，选项不纳入归档成员。
        for index, token in enumerate(tokens[1:], start=1):
            if token.endswith(".a"):
                archive_index = index
                break

        # 找不到显式静态库就没有可归属的归档边；不能用任意目标声明补齐证据。
        if archive_index is None:
            return None

        # 静态库参数被定位后，只有它之后的对象文件作为成员输入。
        # 前置标志和模式字符串不会被记录为成员。
        output = tokens[archive_index]

        # 候选成员从归档名之后收集，只保留可识别的对象文件。
        inputs = [
            token
            for token in tokens[archive_index + 1 :]
            if self._has_suffix(token, _OBJECT_SUFFIXES)
        ]

        if not inputs:
            return None

        return ActualDependency(
            target=output,
            source_file=f"Makefile:{rule.line_number}",
            dependencies=self._unique(inputs),
        )

    # 优先查已提取配方的实际目标；缺少显式关联时尝试模式规则。
    def _find_recipe_target_for_source(
        self,
        merged: dict[str, ActualDependency],
        source_file: str,
        makefile: MakefileModel | None,
    ) -> str | None:
        # 先规范化参与比较的源路径再搜索目标。
        # 这样 recipe 以相对前缀书写时不会仅因文本差异失去关联。
        normalized_source = self._normalize_path(source_file)

        # 先查已存在的实际依赖边，找到源码出现的第一个目标。
        # 这使非默认对象目录能接收其头文件关系。
        for target, dependency in merged.items():
            normalized_dependencies = {
                self._normalize_path(item)
                for item in dependency.dependencies
            }

            # 按发现顺序取第一个包含该源的目标。
            # 这里不推断同一源在多种配置下生成的所有变体。
            if normalized_source in normalized_dependencies:
                return target

        # 无显式配方匹配时再尝试模式目标；不能把所有文件都假定产出同名对象。
        if makefile is not None:
            return self.pattern_rules.target_for_prerequisite(
                normalized_source,
                makefile.rules,
            )

        # 缺少显式证据和可用模式时让调用者保留编译器自己的目标名。
        # 没有命中并不是找到了实际对象目录。
        return None

    # 只收集分析根下的头文件父目录，避免把别处工程的目录混入 include 搜索。
    def _collect_include_dirs(
        self,
        project: ProjectModel,
        analysis_root: Path,
    ) -> list[str]:
        # 默认 include 搜索目录含分析根本身，其他目录统一转换为相对路径。
        include_dirs: set[str] = {"."}

        for header_file in project.header_files:
            header_path = project.source_root / header_file

            try:
                parent = header_path.parent.relative_to(
                    analysis_root
                )
            # 选定 Makefile 外的头目录不参与此轮预处理，不能用相邻项目掩盖缺失依赖。
            except ValueError:
                continue

            # 搜索根已用点目录登记；只有其下其他父目录才增加新的候选。
            if str(parent) != ".":
                include_dirs.add(
                    parent.as_posix()
                )

        # 公共 include 搜索目录按稳定顺序传递。
        # 发现头文件的遍历顺序不应令同一目录集合产生不同的命令参数顺序。
        return sorted(include_dirs)

    # 按源文件类型选择编译器，参数使用列表交给进程而非拼接 shell 命令。
    def _analyze_source(
        self,
        root: Path,
        source_file: str,
        include_dirs: list[str],
        generated_targets: set[str],
    ) -> ActualDependency:
        # 预处理依赖调用使用平台约定的 gcc 或 g++。
        # 从配方提取边不表示将使用配方中的编译器二进制。
        compiler = self._compiler_for_source(
            source_file
        )
        # 给以选项或响应文件标记开头的路径加相对前缀，保留文件参数语义。
        source_argument = (
            f"./{source_file}"
            if source_file.startswith(("-", "@"))
            else source_file
        )

        # 依赖模式只输出工程头文件；生成头尚不存在时先保留其名称。
        # 依赖模式只输出依赖声明，不生成对象文件。
        # 允许暂时缺失的生成头进入后续规则匹配，避免把所有缺失都当成编译失败。
        command = [
            compiler,
            "-MM",
            "-MG",
            source_argument,
        ]

        # include 目录以独立 argv 参数传入，带空格的目录不需要拼接成 shell 文本。
        for include_dir in include_dirs:
            command.extend(
                ["-I", include_dir]
            )

        # 调用仅采集编译器依赖输出，标准错误单独保存用于解释失败。
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )

        # 编译器出错时不能返回空依赖作为成功结果，交由准备阶段报告失败。
        # 错误带当前源码和编译器诊断，调用方可以区分项目输入问题与正常的无缺边。
        if result.returncode != 0:
            raise DependencyAnalysisError(
                f"依赖分析失败: {source_file}\n"
                f"{result.stderr}"
            )

        # 只有成功退出才解析依赖声明，错误输出不能成为虚假的图节点。
        # 提取依据是本次编译器的标准输出，错误输出已由前一分支处理。
        # 不从错误信息中的文件名推断一份看似有效的依赖结果。
        target, dependencies = (
            self._parse_compiler_dependency_output(
                result.stdout
            )
        )

        # 对预处理器报告的每个文件核对存在性或生成目标归属。
        dependencies = [
            self._resolve_generated_dependency(
                root=root,
                dependency=item,
                include_dirs=include_dirs,
                generated_targets=generated_targets,
            )
            for item in dependencies
        ]

        # 头文件路径完成存在性或生成目标解析后再返回。
        # 无法归属的缺失依赖不会混入看似完整的图。
        return ActualDependency(
            target=target,
            source_file=source_file,
            dependencies=dependencies,
        )

    # 源码对应的配方可能有专用 include 路径，不能仅依赖工程级目录。
    def _recipe_include_dirs_for_source(
        self,
        makefile: MakefileModel,
        source_file: str,
    ) -> list[str]:
        # 专用 include 仅适用于配方真正提到当前源码的情况，其他配方忽略。
        normalized_source = self._normalize_path(source_file)
        include_dirs: list[str] = []

        # 只借用与当前源明确关联的 recipe 搜索目录。
        # 遍历全表是为了定位来源，不能把不相关编译规则的路径全部混入。
        for rule in makefile.rules:
            for recipe in rule.recipes:
                command = self._expand_automatic_variables(recipe, rule)

                for command_part in self._split_shell_commands(command):
                    tokens = self._command_tokens(command_part)

                    # 编译器集合与配方依赖识别保持同类，不能把归档器参数解释成 include 路径。
                    if not tokens or Path(tokens[0]).name not in {
                        "gcc", "g++", "clang", "clang++", "cc", "c++",
                    }:
                        continue

                    # 这里也使用后缀与规范化路径配对。
                    # 不会从变量值、响应文件或外部脚本里递归追踪源码参数。
                    sources = {
                        self._normalize_path(token)
                        for token in tokens[1:]
                        if self._has_suffix(token, _SOURCE_SUFFIXES)
                    }

                    # 归一化后的路径用于精确匹配，不按同名文件跨目录借用 include 参数。
                    if normalized_source not in sources:
                        continue

                    # 这里只使用配方的搜索目录证据，不继承宏、工作目录变换或任意环境设置。
                    # 预处理结果可能因此与完整构建存在差异。
                    include_dirs.extend(self._include_dirs_from_tokens(tokens))

        # 返回次序沿用识别到的参数先后。
        # 去重保留首次出现位置，避免改动编译器查找顺序。
        return self._unique(include_dirs)

    @staticmethod
    def _command_tokens(command: str) -> list[str]:
        # 去掉 Make 配方控制前缀后再按词分析；解析失败不产生关系。
        command = command.strip()

        # 这些前缀属于 Make 的命令控制，不是可执行文件名的一部分。
        while command and command[0] in "@-+":
            command = command[1:].lstrip()

        # 使用有限的分词做依赖推导，失败时返回空结果，不交给 shell 试运行。
        try:
            tokens = shlex.split(command)
        # 不能可靠分词的片段没有可提取证据。
        # 不尝试对不成对引号或转义执行猜测性修复。
        except ValueError:
            return []

        # 跳过命令前的简单环境赋值，仅将后续词作为工具名和参数处理。
        # 仅跳过符合简单名字形状的赋值，避免把正常路径中的等号误作环境声明。
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            # 赋值前缀只为定位真正的命令而跳过。
            # 分析并不将这些变量写回本进程，也不执行这个 recipe。
            left, _, _ = tokens[0].partition("=")
            if not left.replace("_", "").isalnum():
                break
            tokens = tokens[1:]

        # 识别常见编译缓存包装器，分析真正接收参数的工具。
        if tokens and Path(tokens[0]).name in {"ccache", "sccache"}:
            tokens = tokens[1:]

        return tokens

    @staticmethod
    # 处理分开的目录参数和紧贴选项的写法，维持原有搜索顺序。
    def _include_dirs_from_tokens(tokens: list[str]) -> list[str]:
        include_dirs: list[str] = []
        index = 1

        # 从程序名之后逐项检查，只收集当前实现识别的 include 形式。
        # 其他编译器的搜索选项不应被当作已经模拟过。
        while index < len(tokens):
            token = tokens[index]

            # 分离形式消费下一项并移动两个位置，不能再次把目录本身当成选项。
            if token == "-I" and index + 1 < len(tokens):
                include_dirs.append(tokens[index + 1])
                # 分离形式中的目录参数已消费，游标越过这两个词。
                # 即使目录名恰好长得像选项，也不在这里二次解释。
                index += 2
                continue

            # 紧贴形式只取选项后缀；没有目录的孤立选项不会在此构造空路径。
            if token.startswith("-I") and len(token) > 2:
                include_dirs.append(token[2:])

            index += 1

        return include_dirs

    def _resolve_generated_dependency(
        self,
        root: Path,
        dependency: str,
        include_dirs: list[str],
        generated_targets: set[str],
    ) -> str:
        # 编译器可能以未经解析的名字列出生成头；先验证该名字是否已经存在。
        normalized = self._normalize_path(dependency)

        # 真实文件优先于同名生成目标；未出现的头文件才做候选归并。
        if (root / normalized).exists():
            # 实际文件优先于推断出来的生成候选。
            # 可以直接定位的路径不需要经过多个同名目标的歧义判断。
            return normalized

        # 仅在已知 include 目录和原始名字中寻找与声明目标的精确交集。
        # 不能在工程内按文件名猜测唯一产物。
        candidates = {
            self._normalize_path((Path(include_dir) / normalized).as_posix())
            for include_dir in include_dirs
        }
        # 把原始拼写也纳入匹配，支持 Make 直接用头文件名声明生成目标。
        candidates.add(normalized)
        # 匹配依靠完整相对路径交集，不仅比较文件 basename。
        # 相同文件名出现在多个 include 位置时可保留歧义证据。
        matches = candidates & generated_targets

        # 唯一声明目标可安全用于修复，多义或完全缺失时显式报告分析失败。
        # 唯一匹配返回规则中的目标路径，便于检测器与声明边比较。
        # 不会把缺失时编译器回显的简写当成实际文件位置。
        if len(matches) == 1:
            return next(iter(matches))

        # 多个可行目标意味着无法确定正确依赖边，诊断列出候选便于修正 Makefile。
        if matches:
            # 多项匹配无法确定一条可靠边，因此报告候选让用户消歧。
            # 排序仅用于诊断稳定，不代表候选优先级。
            raise DependencyAnalysisError(
                "依赖分析失败: 缺失头文件 "
                f"{dependency} 匹配多个 Makefile target: "
                f"{', '.join(sorted(matches))}"
            )

        # 声明中没有生成该头的目标时中止分析，避免修复预览加入悬空边。
        raise DependencyAnalysisError(
            "依赖分析失败: 缺失头文件 "
            f"{dependency} 无法解析为 Makefile generated target"
        )

    @staticmethod
    def _compiler_for_source(
        source_file: str,
    ) -> str:
        # 源类型由扩展名确定，不从 Make 配方猜测跨编译工具链。
        # 依赖探测依赖系统已有对应编译器。
        suffix = Path(source_file).suffix.lower()

        # 只以源后缀选择 GCC 家族前端；这里不执行项目自定义的编译器命令。
        # recipe 在此模块主要提供关联路径和 include 证据。
        if suffix == ".c":
            return "gcc"

        return "g++"

    @staticmethod
    # 编译器依赖输出可能跨行，先还原声明再切分目标与前置项。
    def _parse_compiler_dependency_output(
        output: str,
    ) -> tuple[str, list[str]]:
        # 编译器可能折行输出 Make 风格依赖；先接续物理行再查找目标与输入的分隔。
        # 后续解析适用于这个输出子集，并非任意 Make 语句。
        output = output.replace(
            "\\\n",
            " ",
        ).strip()

        # 缺少声明分隔符时报告编译器输出不符合预期，而不是返回空依赖。
        if ":" not in output:
            raise ValueError(
                f"无法解析编译器依赖输出: {output}"
            )

        # 解析针对单条简单依赖声明，不尝试解释整个 Makefile 或一般转义语法。
        target_part, dependency_part = output.split(
            ":",
            1,
        )

        # 当前按空白切分先决项。
        # 这份解析结果不承诺无损保留所有可能的 Make 文件名转义。
        target = target_part.strip()
        dependencies = dependency_part.split()

        return target, dependencies

    @staticmethod
    # 按当前规则上下文替换常见自动变量，供后续命令识别使用。
    def _expand_automatic_variables(
        recipe: str,
        rule: MakeRule,
    ) -> str:
        # 没有正常前置项时首项自动变量为空；order-only 项只供独立变量使用。
        first = (
            rule.normal_prerequisites[0]
            if rule.normal_prerequisites
            else ""
        )

        # 普通依赖与顺序依赖保持不同来源；没有在此重现 Make 的完整自动变量语义。
        all_dependencies = " ".join(
            rule.normal_prerequisites
        )

        result = recipe
        # 替换仅针对解析器已建模的自动变量。
        # 不通过运行 Make 获取完整的规则展开结果。
        result = result.replace("$@", rule.target)
        result = result.replace("$<", first)
        result = result.replace("$^", all_dependencies)
        result = result.replace("$+", all_dependencies)
        result = result.replace("$|", " ".join(rule.order_only_prerequisites))

        return result

    @staticmethod
    # 只拆分明确支持的复合命令形式，并非通用 shell 语法解释器。
    def _split_shell_commands(
        command: str,
    ) -> list[str]:
        # 对常见 "mkdir ... && g++ ..." 做最低成本支持。
        # 片段切分只为识别常见连续配方，不能作为完整 shell 解析结果使用。
        parts = [command]

        # 分割是受限文本处理，不是带引号、重定向和管道语义的 shell 解释。
        # 无法识别的片段最多缺少证据，不作为命令执行。
        for separator in ("&&", ";"):
            split_parts: list[str] = []

            for part in parts:
                split_parts.extend(
                    item.strip()
                    for item in part.split(separator)
                    if item.strip()
                )

            # 分隔符按固定顺序处理；所得片段用于搜集证据，不能作为可以安全执行的命令。
            parts = split_parts

        return parts

    @staticmethod
    # 兼容带独立值或紧贴参数的输出选项，以免把输出文件计为输入。
    def _output_argument(
        tokens: list[str],
    ) -> str | None:
        # 首个可识别输出参数决定关联目标，后续参数不影响此分析模型。
        for index, token in enumerate(tokens):
            # 找到第一种可识别的输出写法就返回。
            # 不模拟编译器如何处理多个互相冲突的输出选项。
            if token == "-o" and index + 1 < len(tokens):
                return tokens[index + 1]

            if token.startswith("-o") and len(token) > 2:
                return token[2:]

        return None

    @staticmethod
    # 以选项标记开头的词不能仅凭后缀判为文件。
    def _has_suffix(
        token: str,
        suffixes: set[str],
    ) -> bool:
        # 排除 -Wl,... / -lfoo 之类 option
        if token.startswith("-"):
            return False

        return Path(token).suffix.lower() in suffixes

    @staticmethod
    def _normalize_path(
        value: str,
    ) -> str:
        # 去掉常见当前目录前缀，使配方和编译器输出可关联到同一个目标。
        value = value.strip()

        # 移除的是常见相对路径前缀。
        # 这不做文件访问，也不把图中的逻辑路径转换成主机上的绝对路径。
        while value.startswith("./"):
            value = value[2:]

        return Path(value).as_posix()

    @staticmethod
    def _unique(
        values: list[str],
    ) -> list[str]:
        # 去重保留首次出现顺序，使输出可比较且不重排输入优先级。
        return list(dict.fromkeys(values))

    @staticmethod
    # 按归一化目标合并两类证据；保留首次出现的边顺序。
    def _merge_dependency(
        target_map: dict[str, ActualDependency],
        incoming: ActualDependency,
    ) -> None:
        # 以归一化目标为节点标识，避免同一路径多种拼写形成重复节点。
        # 源字段保留最先建立该目标的来源，不会随每次并入而改写。
        target = DependencyAnalyzer._normalize_path(
            incoming.target
        )

        dependencies = [
            DependencyAnalyzer._normalize_path(item)
            for item in incoming.dependencies
        ]

        # 同一目标从后续证据得到更多前置项时保留最初出处；不会替换已建立的边。
        existing = target_map.get(target)

        # 新目标保存自己的前置项列表，后续合并不会修改调用方传入的证据对象。
        if existing is None:
            target_map[target] = ActualDependency(
                target=target,
                source_file=incoming.source_file,
                dependencies=list(
                    dict.fromkeys(dependencies)
                ),
            )
            # 新节点建立后本轮无需再次走并集逻辑。
            # 后续证据按相同目标键再合并。
            return

        # 已存在目标保持原来源记录，只扩充其实际前置项。
        existing.dependencies = list(
            dict.fromkeys(
                [
                    *existing.dependencies,
                    *dependencies,
                ]
            )
        )
