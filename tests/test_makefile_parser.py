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