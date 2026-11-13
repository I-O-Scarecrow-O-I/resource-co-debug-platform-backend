from app.modules.co_debug.dependency.models import MakeRule


class PatternRuleResolver:
    """Resolve the small, safe subset of single-`%` Make pattern rules."""

    # 按规则顺序选择首个匹配项，以同一 stem 实例化两种前置项。
    # 保留原行号用于修复定位；配方独立复制以免修改原规则。
    def resolve(self, target: str, rules: list[MakeRule]) -> MakeRule | None:
        # 匹配顺序来自解析后的规则列表；这里没有 GNU Make 的隐含规则优先级。
        # 因此一次匹配只支持后续分析采用该候选，不证明 Make 最终会选中它。
        for rule in rules:
            # 模式匹配返回 None 才是失败；空 stem 不能用布尔值判断。
            # 这关系到前后缀恰好覆盖整个名称时能否实例化规则。
            stem = self._match(rule.target, target)
            if stem is None:
                continue
            # 具体目标是调用者已经知道的文件名；替换只实例化其前置项。
            # 配方仍保留原始自动变量，稍后才在具体目标上下文中展开。
            return MakeRule(
                target=target,
                normal_prerequisites=[
                    item.replace("%", stem) for item in rule.normal_prerequisites
                ],
                order_only_prerequisites=[
                    item.replace("%", stem) for item in rule.order_only_prerequisites
                ],
                recipes=list(rule.recipes),
                line_number=rule.line_number,
            )
        # 没有匹配时由调用方保留未解析目标；这里不合成一条默认编译配方。
        return None

    # 从编译源反推可能的构建目标，只根据正常前置项推导。
    # 多个模式适用时沿用声明顺序，避免随容器顺序变化。
    def target_for_prerequisite(self, prerequisite: str, rules: list[MakeRule]) -> str | None:
        for rule in rules:
            # 反推只接受能够唯一划分前缀与后缀的目标模式。
            # 多占位符的规则不产生猜测性的目标。
            if rule.target.count("%") != 1:
                continue
            # 反向匹配依赖能从前置项取出同一个 stem。
            # 固定名称的前置项无法反推出目标通配部分。
            for pattern in rule.normal_prerequisites:
                # 普通先决项提供 stem 的来源；纯顺序依赖只影响准备次序。
                # 不能用公共输出目录来反推每个源码对应的对象文件。
                stem = self._match(pattern, prerequisite)
                if stem is not None:
                    return rule.target.replace("%", stem)
        return None

    @staticmethod
    # 仅识别单个通配符，两端字面量均须完整匹配。
    # 长度检查拒绝前缀和后缀互相重叠的伪匹配。
    def _match(pattern: str, target: str) -> str | None:
        if pattern.count("%") != 1:
            return None
        # 把百分号两侧视为普通字面量，不按正则或 shell 通配模式匹配。
        # 规则中的点号等字符只匹配其自身。
        prefix, suffix = pattern.split("%", 1)
        if not target.startswith(prefix) or not target.endswith(suffix):
            return None
        # 前后缀检查都成功也可能覆盖同一段字符；长度约束保证中间区间有效。
        # 前后缀恰好覆盖整个目标时，空字符串仍是一次匹配。
        if len(target) < len(prefix) + len(suffix):
            return None
        # 无后缀时切到字符串末尾，避免零长度负索引误截掉 stem。
        stem_end = len(target) - len(suffix) if suffix else len(target)
        return target[len(prefix):stem_end]
