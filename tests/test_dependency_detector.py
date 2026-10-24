from app.modules.co_debug.dependency.dependency_analyzer import DependencyAnalyzer
from app.modules.co_debug.dependency.dependency_detector import DependencyDetector
from app.modules.co_debug.dependency.makefile_parser import MakefileParser
from app.modules.co_debug.dependency.models import ActualDependency
from app.modules.co_debug.dependency.project_parser import ProjectParser


def test_detect_missing_dependency(tmp_path):

    src_dir = tmp_path / "src"
    include_dir = tmp_path / "include"

    src_dir.mkdir()
    include_dir.mkdir()

    # 头文件
    (include_dir / "add.h").write_text(
        """
#ifndef ADD_H
#define ADD_H

int add(int a, int b);

#endif
""",
        encoding="utf-8",
    )

    # main.c 实际依赖 add.h
    (src_dir / "main.c").write_text(
        """
#include "add.h"

int main() {
    return add(1, 2);
}
""",
        encoding="utf-8",
    )

    # add.c 同样依赖 add.h
    (src_dir / "add.c").write_text(
        """
#include "add.h"

int add(int a, int b) {
    return a + b;
}
""",
        encoding="utf-8",
    )

    # 故意隐藏 main.o 对 include/add.h 的依赖
    (tmp_path / "Makefile").write_text(
        """
main: main.o add.o

main.o: src/main.c
add.o: src/add.c include/add.h
""",
        encoding="utf-8",
    )

    # B1
    project_parser = ProjectParser()
    project = project_parser.parse(tmp_path)

    # B2
    makefile_parser = MakefileParser()
    makefile_model = makefile_parser.parse(
        tmp_path / "Makefile"
    )

    # B3
    analyzer = DependencyAnalyzer()
    actual_dependencies = analyzer.analyze(project)

    # B4
    detector = DependencyDetector()

    missing = detector.detect(
        makefile=makefile_model,
        actual_dependencies=actual_dependencies,
    )

    assert len(missing) == 1

    assert missing[0].target == "main.o"

    assert missing[0].dependency == "include/add.h"

    assert missing[0].source_file == "src/main.c"


def test_order_only_prerequisite_does_not_satisfy_source_dependency(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text("build/main.o: src/main.c | generated/version.h\n")
    model = MakefileParser().parse(makefile)

    missing = DependencyDetector().detect(
        model,
        [
            ActualDependency(
                target="build/main.o",
                source_file="src/main.c",
                dependencies=["src/main.c", "generated/version.h"],
            )
        ],
    )

    assert [(item.target, item.dependency) for item in missing] == [
        ("build/main.o", "generated/version.h")
    ]


def test_pattern_rule_is_instantiated_for_an_actual_target(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text("build/%.o: src/%.c | build\n")
    model = MakefileParser().parse(makefile)

    missing = DependencyDetector().detect(
        model,
        [
            ActualDependency(
                target="build/main.o",
                source_file="src/main.c",
                dependencies=["src/main.c"],
            )
        ],
    )

    assert missing == []


def test_generated_header_rule_does_not_hide_missing_compile_dependency(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "generated/version.h: VERSION scripts/gen_version.sh\n"
        "\ttouch $@\n"
        "build/main.o: src/main.c | build\n"
    )
    model = MakefileParser().parse(makefile)

    missing = DependencyDetector().detect(
        model,
        [
            ActualDependency(
                target="build/main.o",
                source_file="src/main.c",
                dependencies=["src/main.c", "generated/version.h"],
            )
        ],
    )

    assert [(item.target, item.dependency) for item in missing] == [
        ("build/main.o", "generated/version.h")
    ]
