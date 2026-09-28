import shutil
import subprocess

import pytest

from app.modules.co_debug.dependency.dependency_analyzer import DependencyAnalyzer
from app.modules.co_debug.dependency.dependency_detector import DependencyDetector
from app.modules.co_debug.dependency.dependency_repair import DependencyRepair
from app.modules.co_debug.dependency.makefile_parser import MakefileParser
from app.modules.co_debug.dependency.project_parser import ProjectParser


def test_dependency_analyzer(tmp_path):

    src_dir = tmp_path / "src"
    include_dir = tmp_path / "include"

    src_dir.mkdir()
    include_dir.mkdir()

    (include_dir / "add.h").write_text(
        """
#ifndef ADD_H
#define ADD_H

int add(int a, int b);

#endif
""",
        encoding="utf-8",
    )

    (src_dir / "main.c").write_text(
        """
#include "add.h"

int main() {
    return add(1, 2);
}
""",
        encoding="utf-8",
    )

    (src_dir / "add.c").write_text(
        """
#include "add.h"

int add(int a, int b) {
    return a + b;
}
""",
        encoding="utf-8",
    )

    (tmp_path / "Makefile").write_text(
        """
main: main.o add.o

main.o: src/main.c
add.o: src/add.c include/add.h
""",
        encoding="utf-8",
    )

    project_parser = ProjectParser()
    project = project_parser.parse(tmp_path)

    analyzer = DependencyAnalyzer()

    dependencies = analyzer.analyze(project)

    dependency_map = {
        item.target: item.dependencies
        for item in dependencies
    }

    assert "main.o" in dependency_map
    assert "add.o" in dependency_map

    assert "src/main.c" in dependency_map["main.o"]
    assert "include/add.h" in dependency_map["main.o"]

    assert "src/add.c" in dependency_map["add.o"]
    assert "include/add.h" in dependency_map["add.o"]


@pytest.mark.skipif(
    shutil.which("gcc") is None or shutil.which("make") is None,
    reason="gcc and make are required",
)
def test_generated_header_dependency_is_analyzed_repaired_and_builds_in_parallel(
    tmp_path,
):
    project_root = tmp_path / "case01_generated_version_header"
    (project_root / "src").mkdir(parents=True)
    (project_root / "include").mkdir()
    (project_root / "scripts").mkdir()

    (project_root / "VERSION").write_text("1.0.1\n", encoding="utf-8")
    (project_root / "include" / "banner.h").write_text(
        "const char *banner_text(void);\n",
        encoding="utf-8",
    )
    (project_root / "src" / "banner.c").write_text(
        '#include "banner.h"\n#include "version.h"\n'
        'const char *banner_text(void){ return "case01-" APP_VERSION; }\n',
        encoding="utf-8",
    )
    (project_root / "src" / "main.c").write_text(
        '#include <stdio.h>\n#include "version.h"\n#include "banner.h"\n'
        'int main(void){ printf("%s %s\\n", banner_text(), APP_VERSION); return 0; }\n',
        encoding="utf-8",
    )
    generator = project_root / "scripts" / "gen_version.sh"
    generator.write_text(
        '#!/usr/bin/env bash\nset -eu\nout="$1"\nver=$(cat VERSION)\n'
        'mkdir -p "$(dirname "$out")"\n'
        "printf '#define APP_VERSION \"%s\"\\n' \"$ver\" > \"$out\"\n",
        encoding="utf-8",
    )
    generator.chmod(0o755)

    makefile = project_root / "Makefile"
    makefile.write_text(
        "CC := gcc\n"
        "CFLAGS := -std=c11 -Wall -Werror -Iinclude -Igenerated\n"
        "OBJS := build/main.o build/banner.o\n"
        "all: generated/version.h build/app\n\n"
        "generated/version.h: VERSION scripts/gen_version.sh\n"
        "\tbash scripts/gen_version.sh $@\n\n"
        "build/main.o: src/main.c | build\n"
        "\t$(CC) $(CFLAGS) -c $< -o $@\n"
        "build/banner.o: src/banner.c include/banner.h | build\n"
        "\t$(CC) $(CFLAGS) -c $< -o $@\n"
        "build/app: $(OBJS)\n"
        "\t$(CC) $(OBJS) -o $@\n"
        "build:\n\tmkdir -p $@\n",
        encoding="utf-8",
    )

    project = ProjectParser().parse(tmp_path)
    makefile_model = MakefileParser().parse(makefile)
    actual = DependencyAnalyzer().analyze(project, makefile_model)
    actual_map = {item.target: item.dependencies for item in actual}

    assert "generated/version.h" in actual_map["build/main.o"]
    assert "generated/version.h" in actual_map["build/banner.o"]

    missing = DependencyDetector().detect(makefile_model, actual)
    generated_missing = {
        (item.target, item.dependency)
        for item in missing
        if item.dependency == "generated/version.h"
    }
    assert generated_missing == {
        ("build/main.o", "generated/version.h"),
        ("build/banner.o", "generated/version.h"),
    }

    repaired = DependencyRepair().write(makefile, missing)
    assert repaired.repaired_makefile is not None
    assert "build/main.o: generated/version.h" in repaired.content
    assert "build/banner.o: generated/version.h" in repaired.content

    completed = subprocess.run(
        ["make", "-f", repaired.repaired_makefile.name, "-j8"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (project_root / "build" / "app").is_file()


def test_missing_header_without_makefile_target_remains_an_analysis_error(tmp_path):
    (tmp_path / "main.c").write_text(
        '#include "missing.h"\nint main(void) { return 0; }\n',
        encoding="utf-8",
    )
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "main.o: main.c\n\t$(CC) -c $< -o $@\n",
        encoding="utf-8",
    )

    project = ProjectParser().parse(tmp_path)
    makefile_model = MakefileParser().parse(makefile)

    with pytest.raises(RuntimeError, match="无法解析为 Makefile generated target"):
        DependencyAnalyzer().analyze(project, makefile_model)
