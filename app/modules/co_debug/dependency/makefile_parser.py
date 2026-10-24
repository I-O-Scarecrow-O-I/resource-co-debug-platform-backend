import re
from pathlib import Path

from app.modules.co_debug.dependency.models import MakefileModel, MakeRule

_VARIABLE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(:=|\?=|\+=|=)\s*(.*)$")

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

    def parse(self, makefile_path: Path) -> MakefileModel:
        text = makefile_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        physical_lines = text.splitlines()

        variables: dict[str, str] = {}
        rules: list[MakeRule] = []

        current_rules: list[MakeRule] = []
        line_index = 0

        while line_index < len(physical_lines):
            raw_line = physical_lines[line_index]
            line_number = line_index + 1

            # recipe 必须优先处理，因为 recipe 本身可能包含 ':'
            if raw_line.startswith("\t"):
                recipe = raw_line[1:].strip()

                while recipe.endswith("\\") and line_index + 1 < len(physical_lines):
                    recipe = recipe[:-1].rstrip()
                    line_index += 1
                    continuation = physical_lines[line_index].lstrip()
                    recipe += " " + continuation

                if recipe and current_rules:
                    for rule in current_rules:
                        rule.recipes.append(recipe)

                line_index += 1
                continue

            current_rules = []

            logical_line = raw_line

            while (
                logical_line.rstrip().endswith("\\")
                and line_index + 1 < len(physical_lines)
            ):
                logical_line = logical_line.rstrip()
                logical_line = logical_line[:-1].rstrip()

                line_index += 1
                logical_line += " " + physical_lines[line_index].strip()

            stripped = logical_line.strip()

            if not stripped or stripped.startswith("#"):
                line_index += 1
                continue

            variable_match = _VARIABLE_RE.match(stripped)
            if variable_match:
                name = variable_match.group(1)
                operator = variable_match.group(2)
                value = variable_match.group(3).strip()

                if operator == "?=" and name in variables:
                    line_index += 1
                    continue
                if operator == "+=" and name in variables:
                    variables[name] = (variables[name] + " " + value).strip()
                elif operator == ":=":
                    variables[name] = self._expand(value, variables)
                else:
                    variables[name] = value

                line_index += 1
                continue

            # 去掉规则尾部注释
            rule_text = stripped.split("#", 1)[0].rstrip()

            if ":" not in rule_text:
                line_index += 1
                continue

            target_part, prerequisite_part = rule_text.split(":", 1)

            target_part = self._expand(target_part.strip(), variables)
            prerequisite_part = self._expand(
                prerequisite_part.strip(),
                variables,
            )

            targets = target_part.split()

            # .PHONY: all clean 本身不是构建数据依赖
            if targets == [".PHONY"]:
                line_index += 1
                continue

            prerequisite_tokens = prerequisite_part.split()
            separator = prerequisite_tokens.index("|") if "|" in prerequisite_tokens else None
            normal_prerequisites = prerequisite_tokens[:separator]
            order_only_prerequisites = (
                prerequisite_tokens[separator + 1 :] if separator is not None else []
            )

            for target in targets:
                rule = MakeRule(
                    target=target,
                    normal_prerequisites=list(normal_prerequisites),
                    order_only_prerequisites=list(order_only_prerequisites),
                    line_number=line_number,
                )
                rules.append(rule)
                current_rules.append(rule)

            line_index += 1

        # 所有变量收集完成后再做一次展开，
        # 处理 OBJ = $(SRC:.cpp=.o) 以外的普通嵌套变量。
        for rule in rules:
            rule.target = self._expand(rule.target, variables)

            rule.normal_prerequisites = self._expand_prerequisites(
                rule.normal_prerequisites, variables
            )
            rule.order_only_prerequisites = self._expand_prerequisites(
                rule.order_only_prerequisites, variables
            )

            rule.recipes = [
                self._expand(recipe, variables)
                for recipe in rule.recipes
            ]

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
        result = value

        # 限制迭代次数，避免循环变量引用。
        for _ in range(10):
            changed = False

            def replace(match: re.Match[str]) -> str:
                nonlocal changed

                name = match.group(1) or match.group(2)

                replacement = self._resolve_reference(name, variables)
                if replacement is None:
                    return match.group(0)

                changed = True
                return replacement

            new_result = _VARIABLE_REF_RE.sub(replace, result)

            result = new_result

            if not changed:
                break

        return result

    def _resolve_reference(self, reference: str, variables: dict[str, str]) -> str | None:
        name, separator, substitution = reference.partition(":")
        replacement = variables.get(name)
        if replacement is None:
            return None
        if not separator:
            return replacement

        source_suffix, equals, target_suffix = substitution.partition("=")
        if not equals or not source_suffix:
            return None
        return " ".join(
            word[: -len(source_suffix)] + target_suffix
            if word.endswith(source_suffix)
            else word
            for word in replacement.split()
        )

    def _expand_prerequisites(
        self,
        prerequisites: list[str],
        variables: dict[str, str],
    ) -> list[str]:
        expanded_dependencies: list[str] = []
        for prerequisite in prerequisites:
            expanded_dependencies.extend(self._expand(prerequisite, variables).split())
        return expanded_dependencies
