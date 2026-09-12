from app.modules.co_debug.dependency.project_parser import ProjectParser
from app.modules.co_debug.dependency.dependency_analyzer import DependencyAnalyzer


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