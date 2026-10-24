from app.modules.co_debug.dependency.makefile_parser import MakefileParser


def test_parse_makefile(tmp_path):

    makefile = tmp_path / "Makefile"

    makefile.write_text(
        """
CC = gcc

main: main.o add.o
\t$(CC) main.o add.o -o main

main.o: main.c add.h \\
        common.h

add.o: add.c add.h
\t$(CC) -c add.c

.PHONY: clean

clean:
\trm -f *.o main
""",
        encoding="utf-8",
    )

    parser = MakefileParser()

    result = parser.parse(makefile)

    rules = {
        rule.target: rule.prerequisites
        for rule in result.rules
    }

    assert rules["main"] == [
        "main.o",
        "add.o",
    ]

    assert rules["main.o"] == [
        "main.c",
        "add.h",
        "common.h",
    ]

    assert rules["add.o"] == [
        "add.c",
        "add.h",
    ]

    assert rules["clean"] == []


def test_parser_distinguishes_normal_and_order_only_prerequisites(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text("build/main.o: src/main.c generated/a.h | build generated-dir\n")

    rule = MakefileParser().parse(makefile).rules[0]

    assert rule.normal_prerequisites == ["src/main.c", "generated/a.h"]
    assert rule.order_only_prerequisites == ["build", "generated-dir"]
    assert rule.prerequisites == ["src/main.c", "generated/a.h", "build", "generated-dir"]


def test_parser_supports_common_variable_assignments_and_references(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "SRC = src/main.c\n"
        "SRC += src/util.c\n"
        "DEFAULT ?= include/default.h\n"
        "DEFAULT ?= ignored.h\n"
        "EAGER := include/eager.h\n"
        "OBJ = $(SRC:.c=.o)\n"
        "app: $(OBJ) ${DEFAULT} $(EAGER)\n"
    )

    model = MakefileParser().parse(makefile)

    assert model.variables["SRC"] == "src/main.c src/util.c"
    assert model.variables["DEFAULT"] == "include/default.h"
    assert model.variables["EAGER"] == "include/eager.h"
    assert model.rules[0].normal_prerequisites == [
        "src/main.o",
        "src/util.o",
        "include/default.h",
        "include/eager.h",
    ]


def test_parser_expands_recursive_variables_with_a_depth_limit(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text("A = $(B)\nB = src/main.c\nC = $(C)\napp: $(A) $(C)\n")

    rule = MakefileParser().parse(makefile).rules[0]

    assert rule.normal_prerequisites[0] == "src/main.c"
    assert rule.normal_prerequisites[1] == "$(C)"


def test_parser_never_executes_make_functions(tmp_path):
    marker = tmp_path / "SHOULD_NOT_EXIST"
    makefile = tmp_path / "Makefile"
    makefile.write_text(f"X := $(shell touch {marker})\napp: $(X)\n")

    MakefileParser().parse(makefile)

    assert not marker.exists()
