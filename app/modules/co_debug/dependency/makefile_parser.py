from pathlib import Path
import re

from app.modules.co_debug.dependency.models import (
    MakefileModel,
    MakeRule,
)


VARIABLE_ASSIGNMENT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\s*(?:=|:=|\+=|\?=|!=)"
)


class MakefileParser:
    """
    B2 Makefile 基础解析器。

    当前主要解析显式依赖规则。
    """

    def parse(
        self,
        makefile_path: str | Path,
    ) -> MakefileModel:

        path = Path(makefile_path).resolve()

        if not path.exists():
            raise FileNotFoundError(
                f"Makefile 不存在: {path}"
            )

        if not path.is_file():
            raise ValueError(
                f"Makefile 路径不是文件: {path}"
            )

        model = MakefileModel(
            makefile_path=path
        )

        for line_number, line in self._logical_lines(path):

            rules = self._parse_rule(
                line=line,
                line_number=line_number,
            )

            if rules:
                model.rules.extend(rules)

        return model

    def _logical_lines(
        self,
        path: Path,
    ):

        lines = path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()

        buffer = ""
        start_line = 0

        for line_number, raw_line in enumerate(
            lines,
            start=1,
        ):

            if not buffer:
                start_line = line_number

            stripped = raw_line.rstrip()

            if stripped.endswith("\\"):
                buffer += stripped[:-1] + " "
                continue

            buffer += stripped

            yield start_line, buffer

            buffer = ""

        if buffer:
            yield start_line, buffer

    def _parse_rule(
        self,
        line: str,
        line_number: int,
    ) -> list[MakeRule] | None:

        if line.startswith("\t"):
            return None

        line = line.strip()

        if not line:
            return None

        line = line.split("#", 1)[0].strip()

        if not line:
            return None

        if VARIABLE_ASSIGNMENT.match(line):
            return None

        if ":" not in line:
            return None

        target_part, prerequisite_part = line.split(
            ":",
            1,
        )

        target_part = target_part.strip()
        prerequisite_part = prerequisite_part.strip()

        if not target_part:
            return None

        if ";" in prerequisite_part:
            prerequisite_part = prerequisite_part.split(
                ";",
                1,
            )[0].strip()

        targets = target_part.split()

        prerequisites = [
            item
            for item in prerequisite_part.split()
            if item != "|"
        ]

        rules: list[MakeRule] = []

        for target in targets:

            if target == ".PHONY":
                continue

            rules.append(
                MakeRule(
                    target=target,
                    prerequisites=prerequisites.copy(),
                    line_number=line_number,
                )
            )

        return rules