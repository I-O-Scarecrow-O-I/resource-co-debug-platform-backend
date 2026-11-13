import re
from pathlib import Path

from app.modules.co_debug.dependency.models import MakefileModel, MakeRule

# Security: these are fixed grammar patterns, not expressions built from project
# input.  Both use only simple character-class repetitions/single-pass tails, with no
# nested quantifiers or ambiguous repeated alternation that could cause ReDoS.
_VARIABLE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(:=|\?=|\+=|=)\s*(.*)$")

# 模式只提取括号内不含同类括号的引用。
# 因此某些嵌套表达式只能分轮化简，不能完整还原 Make 的参数展开。
_VARIABLE_REF_RE = re.compile(r"\$\(([^()]+)\)|\${([^{}]+)}")


class MakefileParser:
    """
    B2 Makefile parser.

    快速增强版：
    - 普通 target: prerequisites
    - 简单 Make 变量
    - recipe 保存
    - 反斜杠续行
    - 跳过 .PHONY 声明

    暂不尝试完整实现 GNU Make。
    """

    # 按文本读取构建声明，无法解码的字节用替代符保留其余行。
    # 本解析器只建立分析模型，不执行配方或求值任意 Make 函数。
    def parse(self, makefile_path: Path) -> MakefileModel:
        # 解析需要尽量保留物理行；无法解码的文本不假定成另一种系统编码。
        text = makefile_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        # 以物理行建立索引，拼接逻辑行后仍保留首行位置供修复。
        physical_lines = text.splitlines()

        # 变量表、规则列表和当前配方目标只属于一次解析。
        # 分析另一个构建入口时不能带入上一份文件的变量定义。
        variables: dict[str, str] = {}
        rules: list[MakeRule] = []

        # 当前规则组只为紧邻的配方服务，多目标规则各自持有同一条配方内容。
        current_rules: list[MakeRule] = []
        line_index = 0

        while line_index < len(physical_lines):
            # 记录每个逻辑段的首行，后续依赖诊断可以指出声明位置。
            raw_line = physical_lines[line_index]
            line_number = line_index + 1

            # recipe 必须优先处理，因为 recipe 本身可能包含 ':'
            # 制表符配方继承前一个声明的全部目标，不能作为新规则解析。
            if raw_line.startswith("\t"):
                # 仅移除 recipe 的制表符和外围空白。
                # 本阶段保存分析用文本，不尝试按真实 shell 的语义执行或还原多行脚本。
                recipe = raw_line[1:].strip()

                # 配方续行保持为空格连接的同一条文本供分析，避免把第二行当成顶层规则。
                while recipe.endswith("\\") and line_index + 1 < len(physical_lines):
                    recipe = recipe[:-1].rstrip()
                    line_index += 1
                    continuation = physical_lines[line_index].lstrip()
                    recipe += " " + continuation

                # 无所属规则的配方不产生独立目标。
                # 多目标声明的命令可同时为若干目标提供依赖证据。
                # 每个目标拿到自己的配方列表，后续修改不会串到另一规则。
                if recipe and current_rules:
                    for rule in current_rules:
                        rule.recipes.append(recipe)

                # 若配方跨多行，前面的循环已移到最后一行；这里推进到下一条记录的开头。
                line_index += 1
                continue

            # 非配方行结束当前目标组，后续制表符行不再继承已关闭的组。
            # 即使遇到空白或注释，非配方分支也先结束当前关联。
            # 这属于本解析器的语法边界，不承诺与完整 Make 对空行的处理一致。
            current_rules = []

            logical_line = raw_line

            # 声明续行与配方续行分开处理，当前下标越过所有已消费的物理行。
            while (
                logical_line.rstrip().endswith("\\")
                and line_index + 1 < len(physical_lines)
            ):
                logical_line = logical_line.rstrip()
                logical_line = logical_line[:-1].rstrip()

                line_index += 1
                # 声明续行被拼进本逻辑行，扫描下标不会回去再次识别其局部赋值或冒号。
                logical_line += " " + physical_lines[line_index].strip()

            # 空白和完整注释行不产生语义模型，行索引仍按原始文件推进。
            stripped = logical_line.strip()

            if not stripped or stripped.startswith("#"):
                line_index += 1
                continue

            # 先识别赋值，再处理规则分隔符，避免把立即赋值误读为目标。
            # 识别的是有限的变量赋值语法，不把条件指令或函数当作 Python 代码求值。
            variable_match = _VARIABLE_RE.match(stripped)
            if variable_match:
                name = variable_match.group(1)
                operator = variable_match.group(2)
                # 当前值按赋值语法保存；下面的规则行尾去注释逻辑不适用于变量值。
                value = variable_match.group(3).strip()

                # 条件赋值仅在名称尚未声明时生效，追加保留已有词串。
                if operator == "?=" and name in variables:
                    line_index += 1
                    continue
                # 追加保留词边界；普通延迟赋值先保存文本，后面再展开剩余引用。
                if operator == "+=" and name in variables:
                    variables[name] = (variables[name] + " " + value).strip()
                # 立即赋值在当前位置展开，普通赋值可在后续模型整理时解析。
                # 立即展开只基于当前变量表。
                # 引用的值此刻还不存在时保留原写法，后续模型整理仍可能看到它。
                elif operator == ":=":
                    variables[name] = self._expand(value, variables)
                else:
                    variables[name] = value

                line_index += 1
                continue

            # 去掉规则尾部注释
            # 只有规则部分去掉行内尾注，配方文本走单独分支并保留内容。
            rule_text = stripped.split("#", 1)[0].rstrip()

            # 不在支持语法范围内的非规则行跳过，不为其生成推断目标。
            # 不含冒号的剩余记录不会建立规则。
            # 条件指令和 include 在这里也不会触发另一轮文件读取。
            if ":" not in rule_text:
                line_index += 1
                continue

            # 只以首个冒号切分，目标和前置项各自做变量展开。
            target_part, prerequisite_part = rule_text.split(":", 1)

            # 先展开目标再分词，以支持一个变量提供多个目标。
            target_part = self._expand(target_part.strip(), variables)
            prerequisite_part = self._expand(
                prerequisite_part.strip(),
                variables,
            )

            targets = target_part.split()

            # .PHONY: all clean 本身不是构建数据依赖
            # 只跳过独立的伪目标声明；被标记为伪目标的实际规则仍可能提供可分析配方。
            if targets == [".PHONY"]:
                line_index += 1
                continue

            # 保存 order-only 边供配方展开，但缺失依赖检测只以正常边为准。
            prerequisite_tokens = prerequisite_part.split()
            # 分界之后的项仍记录，但不会被缺失重建边检测当成普通边。
            separator = prerequisite_tokens.index("|") if "|" in prerequisite_tokens else None
            # 分隔符缺省时普通先决项取完整 token 列表。
            # 有分隔符时不把其本身加入图边，保持两种依赖的语义区别。
            normal_prerequisites = prerequisite_tokens[:separator]
            order_only_prerequisites = (
                prerequisite_tokens[separator + 1 :] if separator is not None else []
            )

            # 多目标声明拆成独立规则记录，各自持有前置项与配方列表。
            for target in targets:
                rule = MakeRule(
                    target=target,
                    normal_prerequisites=list(normal_prerequisites),
                    order_only_prerequisites=list(order_only_prerequisites),
                    line_number=line_number,
                )
                # 每个目标保留它所在声明的同一首行编号。
                # 诊断命中不同目标时可指向同一物理行，但其依赖列表彼此独立。
                rules.append(rule)
                current_rules.append(rule)

            line_index += 1

        # 所有变量收集完成后再做一次展开，
        # 处理 OBJ = $(SRC:.cpp=.o) 以外的普通嵌套变量。
        # 二次展开使用最终变量表；不会重新读原文件或执行任何 recipe。
        for rule in rules:
            # 二次展开更新已有规则的目标名，但不会把晚绑定得到的多个词再次拆成多条目标。
            rule.target = self._expand(rule.target, variables)

            # 前置项可由一个引用展开成多个词，普通和 order-only 集合分别保持。
            rule.normal_prerequisites = self._expand_prerequisites(
                rule.normal_prerequisites, variables
            )
            rule.order_only_prerequisites = self._expand_prerequisites(
                rule.order_only_prerequisites, variables
            )

            # 配方的 Make 变量在此展开，目标相关的自动变量留给规则分析器。
            rule.recipes = [
                self._expand(recipe, variables)
                for recipe in rule.recipes
            ]

        # 模型携带原路径和赋值表，后续分析器据此确定相对路径与编译参数。
        # 保留变量表是为了让调用者检查本次静态分析看到的输入。
        # 返回模型不代表已经验证 Make 能构建这些目标。
        return MakefileModel(
            makefile_path=makefile_path,
            rules=rules,
            variables=variables,
        )

    def _expand(
        self,
        value: str,
        variables: dict[str, str],
    ) -> str:
        # 每轮只替换当前可识别的引用，避免无界递归扩展字符串。
        result = value

        # 限制迭代次数，避免循环变量引用。
        # 限制的是引用替换轮数而不是依赖数量。
        # 复杂或循环引用可能在返回时仍有未展开的部分。
        for _ in range(10):
            changed = False

            # 无法解析的引用原样保留，避免在模型中悄悄删除构建标识。
            def replace(match: re.Match[str]) -> str:
                nonlocal changed

                name = match.group(1) or match.group(2)

                # 未知引用原样留存，使调用者能看到支持范围之外的语法。
                replacement = self._resolve_reference(name, variables)
                if replacement is None:
                    return match.group(0)

                changed = True
                return replacement

            # 一次替换可能引入新引用，逐轮展开直到没有可替换项。
            new_result = _VARIABLE_REF_RE.sub(replace, result)

            result = new_result

            # 是否继续由本轮是否找到可替换引用决定。
            # 循环引用即使每轮有匹配，也会在固定次数后保留当时的文本。
            if not changed:
                break

        return result

    # 仅支持普通变量和词尾替换，其他引用交由调用方按未识别文本处理。
    # 后缀替换在变量词列表内作用，不匹配的单词不丢失。
    def _resolve_reference(self, reference: str, variables: dict[str, str]) -> str | None:
        name, separator, substitution = reference.partition(":")
        # 先从本次变量表查找名字，未声明的名字不被视为空值。
        # 保留未识别文本有助于区分缺定义和定义了空字符串。
        replacement = variables.get(name)
        if replacement is None:
            return None
        if not separator:
            return replacement

        # 缺少等号或源后缀不构成可处理的替换式，沿用未解析引用路径。
        source_suffix, equals, target_suffix = substitution.partition("=")
        if not equals or not source_suffix:
            return None
        # 只替换匹配后缀的词，保留不匹配词与整体顺序。
        return " ".join(
            word[: -len(source_suffix)] + target_suffix
            if word.endswith(source_suffix)
            else word
            for word in replacement.split()
        )

    # 单个前置项的迟延展开可能产生多个词，重新分词后再交给图分析。
    def _expand_prerequisites(
        self,
        prerequisites: list[str],
        variables: dict[str, str],
    ) -> list[str]:
        # 拼回前置项列表时保留展开后的词序，不在解析层去重或重排依赖。
        expanded_dependencies: list[str] = []
        # 逐个展开前置项再分词，可保留一个变量产生多个成员的效果。
        # 已分出的普通与仅顺序先决项不会在这里合并。
        for prerequisite in prerequisites:
            expanded_dependencies.extend(self._expand(prerequisite, variables).split())
        return expanded_dependencies
