from pathlib import PurePosixPath

from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakefileModel,
    MissingDependency,
)
from app.modules.co_debug.dependency.pattern_rule_resolver import PatternRuleResolver


class DependencyDetector:
    """
    B4 Makefile 缺失依赖检测器。
    """

    def __init__(self) -> None:
        # 解析具体目标时复用有限模式匹配；仅在声明图没有该目标时回退到模式规则。
        self.pattern_rules = PatternRuleResolver()

    # 比较已观察到的依赖与 Makefile 中声明的正常前置项。
    # 只输出缺失边，目标和源文件信息随边返回供修复定位。
    def detect(
        self,
        makefile: MakefileModel,
        actual_dependencies: list[ActualDependency],
    ) -> list[MissingDependency]:

        # 声明索引使用正规前置项，先聚合显式规则再为必要目标补模式解析。
        # 比较以规范化后的目标为键。
        # 同一目标的多条声明先并成一份普通前置项集合。
        declared_map = self._build_declared_map(
            makefile
        )

        missing_dependencies: list[
            MissingDependency
        ] = []

        # 同一目标可能来自不同编译单元，统一路径形式后再查询声明。
        # 实际证据可能来自不同工具或源码；这里按对应构建目标逐条验证声明。
        for actual in actual_dependencies:

            # 比较两侧路径时使用同一规范表达，工程里常见的当前目录前缀不影响结果。
            target = self._normalize_path(
                actual.target
            )

            # 没有显式规则时才用模式规则实例化目标。
            # 两者都找不到就不推断构建规则，避免把工程中未构建的源码当成缺陷。
            # 模式规则只用于无显式目标时的补查。
            # 它的产物仍属于已声明构建模型，不为未知目标发明规则。
            # 直接规则优先于模式规则，即使它当前没有正常前置项也不回退。
            if target not in declared_map:
                resolved = self.pattern_rules.resolve(target, makefile.rules)
                # 未知目标既无直接规则也无可识别模式时不创建补偿目标，保留此检测边界。
                if resolved is None:
                    continue
                # 只把正常前置项视作已声明，order-only 项不触发头文件修改后的重建。
                # 模式实例化后将具体目标放入局部映射，后续同名证据可使用同一声明集合。
                declared_map[target] = {
                    self._normalize_path(item)
                    for item in resolved.normal_prerequisites
                }

            # 一个目标的已声明集合可供多条实际证据复用；不反过来改写 Makefile 模型。
            declared = declared_map[target]

            # 来源可用于区分编译器和配方报告的相同边，输出本身不抹掉来源信息。
            # 只检查实际依赖列表提供的文件，不从声明倒推它们的生成配方。
            for dependency in actual.dependencies:

                normalized_dependency = (
                    self._normalize_path(
                        dependency
                    )
                )

                # 保留实际依赖的来源，即使最终补写需要在目标维度合并。
                # 顺序依赖不保证前置项变化触发重建，不能用于证明正常依赖已补齐。
                if normalized_dependency not in declared:

                    # 缺边记录保留证据出处，帮助诊断该边来自哪个源文件或哪条配方。
                    missing_dependencies.append(
                        MissingDependency(
                            target=target,
                            dependency=normalized_dependency,
                            source_file=actual.source_file,
                        )
                    )

        return missing_dependencies

    # 显式规则按目标归并，分散在不同声明行的前置项均有效。
    def _build_declared_map(
        self,
        makefile: MakefileModel,
    ) -> dict[str, set[str]]:

        declared_map: dict[
            str,
            set[str],
        ] = {}

        # 不要求单目标只出现一次，重复声明的正常依赖全部纳入比较。
        for rule in makefile.rules:

            target = self._normalize_path(
                rule.target
            )

            dependencies = {
                self._normalize_path(item)
                for item in rule.normal_prerequisites
            }

            # 目标可分多行声明，收集时取并集，避免把较晚声明的依赖误标为缺失。
            declared_map.setdefault(
                target,
                set(),
            )

            # 集合去重只用于成员检查，输出缺边顺序仍取决于实际证据遍历顺序。
            declared_map[target].update(
                dependencies
            )

        return declared_map

    # 分析结果与 Makefile 使用统一的 POSIX 表达，忽略常见的当前目录前缀。
    def _normalize_path(
        self,
        value: str,
    ) -> str:

        # 规范化用于减少不同拼写的误差；不访问文件系统解析符号链接或路径别名。
        value = value.strip()

        # 统一前缀只为文本比较服务，不读取文件系统，也不承担路径权限检查。
        # 移除当前目录前缀后用工程惯用的 POSIX 表达比较，不合并不同目录的同名文件。
        if value.startswith("./"):
            value = value[2:]

        return PurePosixPath(
            value
        ).as_posix()
