from app.modules.co_debug.dependency.project_parser import ProjectParser


def test_parse_project(tmp_path):
    # 构造一个临时 C 工程
    src_dir = tmp_path / "src"
    include_dir = tmp_path / "include"
    build_dir = tmp_path / "build"

    src_dir.mkdir()
    include_dir.mkdir()
    build_dir.mkdir()

    (src_dir / "main.c").write_text(
        '#include "add.h"\nint main() { return 0; }\n'
    )

    (src_dir / "add.c").write_text(
        "int add(int a, int b) { return a + b; }\n"
    )

    (include_dir / "add.h").write_text(
        "int add(int a, int b);\n"
    )

    (tmp_path / "Makefile").write_text(
        "main: src/main.c src/add.c\n"
    )

    (build_dir / "main.o").write_text("")

    parser = ProjectParser()

    result = parser.parse(tmp_path)

    assert result.source_files == [
        "src/add.c",
        "src/main.c",
    ]

    assert result.header_files == [
        "include/add.h",
    ]

    assert result.makefiles == [
        "Makefile",
    ]

    assert result.object_files == [
        "build/main.o",
    ]