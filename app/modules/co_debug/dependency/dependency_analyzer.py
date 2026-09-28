import shlex
import subprocess
from pathlib import Path

from app.modules.co_debug.dependency.models import (
    ActualDependency,
    MakefileModel,
    MakeRule,
    ProjectModel,
)

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

    def analyze(
        self,
        project: ProjectModel,
        makefile: MakefileModel | None = None,
    ) -> list[ActualDependency]:
        merged: dict[str, ActualDependency] = {}

        if makefile is not None:
            for dependency in self._analyze_makefile_recipes(
                makefile
            ):
                self._merge_dependency(
                    merged,
                    dependency,
                )

        project_root = project.source_root
        analysis_root = (
            makefile.makefile_path.parent
            if makefile is not None
            else project_root
        )
        include_dirs = self._collect_include_dirs(
            project,
            analysis_root,
        )
        generated_targets = {
            self._normalize_path(rule.target)
            for rule in makefile.rules
        } if makefile is not None else set()

        for source_file in project.source_files:
            source_path = project_root / source_file

            try:
                analyzed_source = source_path.relative_to(
                    analysis_root
                ).as_posix()
            except ValueError:
                # 选中的 Makefile 只负责其所在工程子树。
                continue

            source_include_dirs = list(include_dirs)

            if makefile is not None:
                source_include_dirs.extend(
                    self._recipe_include_dirs_for_source(
                        makefile,
                        analyzed_source,
                    )
                )

            dependency = self._analyze_source(
                root=analysis_root,
                source_file=analyzed_source,
                include_dirs=self._unique(source_include_dirs),
                generated_targets=generated_targets,
            )

            # 如果 recipe 已经告诉我们
            # "build/main.o <- src/main.cpp"，
            # 就把 -MM 找到的 header 也挂到该真实 object target。
            recipe_target = self._find_recipe_target_for_source(
                merged,
                analyzed_source,
            )

            if recipe_target is not None:
                dependency = ActualDependency(
                    target=recipe_target,
                    source_file=analyzed_source,
                    dependencies=dependency.dependencies,
                )

            self._merge_dependency(
                merged,
                dependency,
            )

        return list(merged.values())

    def _analyze_makefile_recipes(
        self,
        makefile: MakefileModel,
    ) -> list[ActualDependency]:
        results: list[ActualDependency] = []

        for rule in makefile.rules:
            for recipe in rule.recipes:
                command = self._expand_automatic_variables(
                    recipe,
                    rule,
                )

                for command_part in self._split_shell_commands(command):
                    dependency = self._analyze_command(
                        command_part,
                        rule,
                    )

                    if dependency is not None:
                        results.append(dependency)

        return results

    def _analyze_command(
        self,
        command: str,
        rule: MakeRule,
    ) -> ActualDependency | None:
        tokens = self._command_tokens(command)
        if not tokens:
            return None

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

        if executable == "ar":
            return self._analyze_ar_command(
                tokens,
                rule,
            )

        if executable in {"ld", "gold", "ld.lld"}:
            return self._analyze_linker_command(
                tokens,
                rule,
            )

        return None

    def _analyze_compiler_command(
        self,
        tokens: list[str],
        rule: MakeRule,
    ) -> ActualDependency | None:
        output = self._output_argument(tokens)

        if output is None:
            output = rule.target

        if "-c" in tokens:
            sources = [
                token
                for token in tokens[1:]
                if self._has_suffix(
                    token,
                    _SOURCE_SUFFIXES,
                )
            ]

            if not sources:
                return None

            return ActualDependency(
                target=output,
                source_file=sources[0],
                dependencies=self._unique(sources),
            )

        # 编译器作为 linker 使用
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

        if not inputs:
            return None

        return ActualDependency(
            target=output,
            source_file=f"Makefile:{rule.line_number}",
            dependencies=self._unique(inputs),
        )

    def _analyze_linker_command(
        self,
        tokens: list[str],
        rule: MakeRule,
    ) -> ActualDependency | None:
        output = self._output_argument(tokens) or rule.target

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

        return ActualDependency(
            target=output,
            source_file=f"Makefile:{rule.line_number}",
            dependencies=self._unique(inputs),
        )

    def _analyze_ar_command(
        self,
        tokens: list[str],
        rule: MakeRule,
    ) -> ActualDependency | None:
        archive_index = None

        for index, token in enumerate(tokens[1:], start=1):
            if token.endswith(".a"):
                archive_index = index
                break

        if archive_index is None:
            return None

        output = tokens[archive_index]

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

    def _find_recipe_target_for_source(
        self,
        merged: dict[str, ActualDependency],
        source_file: str,
    ) -> str | None:
        normalized_source = self._normalize_path(source_file)

        for target, dependency in merged.items():
            normalized_dependencies = {
                self._normalize_path(item)
                for item in dependency.dependencies
            }

            if normalized_source in normalized_dependencies:
                return target

        return None

    def _collect_include_dirs(
        self,
        project: ProjectModel,
        analysis_root: Path,
    ) -> list[str]:
        include_dirs: set[str] = {"."}

        for header_file in project.header_files:
            header_path = project.source_root / header_file

            try:
                parent = header_path.parent.relative_to(
                    analysis_root
                )
            except ValueError:
                continue

            if str(parent) != ".":
                include_dirs.add(
                    parent.as_posix()
                )

        return sorted(include_dirs)

    def _analyze_source(
        self,
        root: Path,
        source_file: str,
        include_dirs: list[str],
        generated_targets: set[str],
    ) -> ActualDependency:
        compiler = self._compiler_for_source(
            source_file
        )

        command = [
            compiler,
            "-MM",
            "-MG",
            source_file,
        ]

        for include_dir in include_dirs:
            command.extend(
                ["-I", include_dir]
            )

        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"依赖分析失败: {source_file}\n"
                f"{result.stderr}"
            )

        target, dependencies = (
            self._parse_compiler_dependency_output(
                result.stdout
            )
        )

        dependencies = [
            self._resolve_generated_dependency(
                root=root,
                dependency=item,
                include_dirs=include_dirs,
                generated_targets=generated_targets,
            )
            for item in dependencies
        ]

        return ActualDependency(
            target=target,
            source_file=source_file,
            dependencies=dependencies,
        )

    def _recipe_include_dirs_for_source(
        self,
        makefile: MakefileModel,
        source_file: str,
    ) -> list[str]:
        normalized_source = self._normalize_path(source_file)
        include_dirs: list[str] = []

        for rule in makefile.rules:
            for recipe in rule.recipes:
                command = self._expand_automatic_variables(recipe, rule)

                for command_part in self._split_shell_commands(command):
                    tokens = self._command_tokens(command_part)

                    if not tokens or Path(tokens[0]).name not in {
                        "gcc", "g++", "clang", "clang++", "cc", "c++",
                    }:
                        continue

                    sources = {
                        self._normalize_path(token)
                        for token in tokens[1:]
                        if self._has_suffix(token, _SOURCE_SUFFIXES)
                    }

                    if normalized_source not in sources:
                        continue

                    include_dirs.extend(self._include_dirs_from_tokens(tokens))

        return self._unique(include_dirs)

    @staticmethod
    def _command_tokens(command: str) -> list[str]:
        command = command.strip()

        while command and command[0] in "@-+":
            command = command[1:].lstrip()

        try:
            tokens = shlex.split(command)
        except ValueError:
            return []

        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            left, _, _ = tokens[0].partition("=")
            if not left.replace("_", "").isalnum():
                break
            tokens = tokens[1:]

        if tokens and Path(tokens[0]).name in {"ccache", "sccache"}:
            tokens = tokens[1:]

        return tokens

    @staticmethod
    def _include_dirs_from_tokens(tokens: list[str]) -> list[str]:
        include_dirs: list[str] = []
        index = 1

        while index < len(tokens):
            token = tokens[index]

            if token == "-I" and index + 1 < len(tokens):
                include_dirs.append(tokens[index + 1])
                index += 2
                continue

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
        normalized = self._normalize_path(dependency)

        if (root / normalized).exists():
            return normalized

        candidates = {
            self._normalize_path((Path(include_dir) / normalized).as_posix())
            for include_dir in include_dirs
        }
        candidates.add(normalized)
        matches = candidates & generated_targets

        if len(matches) == 1:
            return next(iter(matches))

        if matches:
            raise RuntimeError(
                "依赖分析失败: 缺失头文件 "
                f"{dependency} 匹配多个 Makefile target: "
                f"{', '.join(sorted(matches))}"
            )

        raise RuntimeError(
            "依赖分析失败: 缺失头文件 "
            f"{dependency} 无法解析为 Makefile generated target"
        )

    @staticmethod
    def _compiler_for_source(
        source_file: str,
    ) -> str:
        suffix = Path(source_file).suffix.lower()

        if suffix == ".c":
            return "gcc"

        return "g++"

    @staticmethod
    def _parse_compiler_dependency_output(
        output: str,
    ) -> tuple[str, list[str]]:
        output = output.replace(
            "\\\n",
            " ",
        ).strip()

        if ":" not in output:
            raise ValueError(
                f"无法解析编译器依赖输出: {output}"
            )

        target_part, dependency_part = output.split(
            ":",
            1,
        )

        target = target_part.strip()
        dependencies = dependency_part.split()

        return target, dependencies

    @staticmethod
    def _expand_automatic_variables(
        recipe: str,
        rule: MakeRule,
    ) -> str:
        first = (
            rule.prerequisites[0]
            if rule.prerequisites
            else ""
        )

        all_dependencies = " ".join(
            rule.prerequisites
        )

        result = recipe
        result = result.replace("$@", rule.target)
        result = result.replace("$<", first)
        result = result.replace("$^", all_dependencies)
        result = result.replace("$+", all_dependencies)

        return result

    @staticmethod
    def _split_shell_commands(
        command: str,
    ) -> list[str]:
        # 对常见 "mkdir ... && g++ ..." 做最低成本支持。
        parts = [command]

        for separator in ("&&", ";"):
            split_parts: list[str] = []

            for part in parts:
                split_parts.extend(
                    item.strip()
                    for item in part.split(separator)
                    if item.strip()
                )

            parts = split_parts

        return parts

    @staticmethod
    def _output_argument(
        tokens: list[str],
    ) -> str | None:
        for index, token in enumerate(tokens):
            if token == "-o" and index + 1 < len(tokens):
                return tokens[index + 1]

            if token.startswith("-o") and len(token) > 2:
                return token[2:]

        return None

    @staticmethod
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
        value = value.strip()

        while value.startswith("./"):
            value = value[2:]

        return Path(value).as_posix()

    @staticmethod
    def _unique(
        values: list[str],
    ) -> list[str]:
        return list(dict.fromkeys(values))

    @staticmethod
    def _merge_dependency(
        target_map: dict[str, ActualDependency],
        incoming: ActualDependency,
    ) -> None:
        target = DependencyAnalyzer._normalize_path(
            incoming.target
        )

        dependencies = [
            DependencyAnalyzer._normalize_path(item)
            for item in incoming.dependencies
        ]

        existing = target_map.get(target)

        if existing is None:
            target_map[target] = ActualDependency(
                target=target,
                source_file=incoming.source_file,
                dependencies=list(
                    dict.fromkeys(dependencies)
                ),
            )
            return

        existing.dependencies = list(
            dict.fromkeys(
                [
                    *existing.dependencies,
                    *dependencies,
                ]
            )
        )
