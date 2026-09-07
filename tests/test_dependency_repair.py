from app.modules.co_debug.dependency.dependency_repair import (
    DependencyRepair,
)
from app.modules.co_debug.dependency.models import (
    MissingDependency,
)


def test_generate_repaired_makefile_without_modifying_original(
    tmp_path,
):
    """
    测试 generate()：

    1. 能生成修复后的 Makefile 内容
    2. 不修改原始 Makefile
    3. 不创建 Makefile.repaired
    """

    makefile = tmp_path / "Makefile"

    original_content = (
        "app: main.o\n"
        "\tgcc main.o -o app\n"
        "\n"
        "main.o: src/main.c\n"
        "\tgcc -c src/main.c -o main.o\n"
    )

    makefile.write_text(
        original_content,
        encoding="utf-8",
    )

    missing_dependencies = [
        MissingDependency(
            target="main.o",
            dependency="include/add.h",
            source_file="src/main.c",
        )
    ]

    repair = DependencyRepair()

    result = repair.generate(
        makefile_path=makefile,
        missing_dependencies=missing_dependencies,
    )

    # 修复结果中应该包含新增依赖
    assert (
        "main.o: include/add.h"
        in result.content
    )

    # 应该包含自动补偿说明
    assert (
        "# Auto-generated dependency compensation"
        in result.content
    )

    # generate() 只是预览，所以没有实际输出文件
    assert result.repaired_makefile is None

    # applied_dependencies 应保存这次真正应用的依赖
    assert len(result.applied_dependencies) == 1
    assert (
        result.applied_dependencies[0].dependency
        == "include/add.h"
    )

    # 最重要：原始 Makefile 必须完全不变
    assert (
        makefile.read_text(encoding="utf-8")
        == original_content
    )

    # generate() 不能偷偷创建修复文件
    assert not (
        tmp_path / "Makefile.repaired"
    ).exists()


def test_write_creates_repaired_makefile_without_overwriting_original(
    tmp_path,
):
    """
    测试 write()：

    1. 创建 Makefile.repaired
    2. 修复文件中包含补偿依赖
    3. 原始 Makefile 保持不变
    """

    makefile = tmp_path / "Makefile"

    original_content = (
        "app: main.o\n"
        "\tgcc main.o -o app\n"
        "\n"
        "main.o: src/main.c\n"
        "\tgcc -c src/main.c -o main.o\n"
    )

    makefile.write_text(
        original_content,
        encoding="utf-8",
    )

    missing_dependencies = [
        MissingDependency(
            target="main.o",
            dependency="include/add.h",
            source_file="src/main.c",
        )
    ]

    repair = DependencyRepair()

    result = repair.write(
        makefile_path=makefile,
        missing_dependencies=missing_dependencies,
    )

    repaired_makefile = (
        tmp_path / "Makefile.repaired"
    )

    # 应该真正创建修复文件
    assert repaired_makefile.exists()
    assert repaired_makefile.is_file()

    # 返回路径应该和实际文件一致
    assert (
        result.repaired_makefile
        == repaired_makefile.resolve()
    )

    repaired_content = repaired_makefile.read_text(
        encoding="utf-8"
    )

    # 原有内容仍然存在
    assert "app: main.o" in repaired_content
    assert "main.o: src/main.c" in repaired_content

    # 新增补偿规则存在
    assert (
        "# Auto-generated dependency compensation"
        in repaired_content
    )

    assert (
        "main.o: include/add.h"
        in repaired_content
    )

    # 原始 Makefile 必须保持不变
    assert (
        makefile.read_text(encoding="utf-8")
        == original_content
    )


def test_multiple_dependencies_for_same_target_are_grouped(
    tmp_path,
):
    """
    同一个 target 存在多个缺失依赖时，
    应合并为一条 Makefile 依赖规则。

    同时重复依赖应该自动去重。
    """

    makefile = tmp_path / "Makefile"

    makefile.write_text(
        "main.o: src/main.c\n"
        "\tgcc -c src/main.c -o main.o\n",
        encoding="utf-8",
    )

    missing_dependencies = [
        MissingDependency(
            target="main.o",
            dependency="include/add.h",
            source_file="src/main.c",
        ),
        MissingDependency(
            target="main.o",
            dependency="include/config.h",
            source_file="src/main.c",
        ),
        # 故意重复
        MissingDependency(
            target="main.o",
            dependency="include/add.h",
            source_file="src/main.c",
        ),
    ]

    repair = DependencyRepair()

    result = repair.generate(
        makefile_path=makefile,
        missing_dependencies=missing_dependencies,
    )

    # dependency_repair 中进行了排序，
    # 所以最终应该得到这一行
    expected_rule = (
        "main.o: include/add.h include/config.h"
    )

    assert expected_rule in result.content

    # 不能因为重复输入产生两条相同规则
    assert result.content.count(expected_rule) == 1

    # 补偿区只应该出现一次 target 规则
    compensation_part = result.content.split(
        "# Auto-generated dependency compensation"
    )[1]

    assert compensation_part.count("main.o:") == 1


def test_dependencies_for_different_targets_are_written_separately(
    tmp_path,
):
    """
    不同 target 的依赖不能合并到一起。
    """

    makefile = tmp_path / "Makefile"

    makefile.write_text(
        (
            "main.o: src/main.c\n"
            "\tgcc -c src/main.c -o main.o\n"
            "\n"
            "add.o: src/add.c\n"
            "\tgcc -c src/add.c -o add.o\n"
        ),
        encoding="utf-8",
    )

    missing_dependencies = [
        MissingDependency(
            target="main.o",
            dependency="include/config.h",
            source_file="src/main.c",
        ),
        MissingDependency(
            target="add.o",
            dependency="include/add.h",
            source_file="src/add.c",
        ),
    ]

    result = DependencyRepair().generate(
        makefile_path=makefile,
        missing_dependencies=missing_dependencies,
    )

    assert (
        "main.o: include/config.h"
        in result.content
    )

    assert (
        "add.o: include/add.h"
        in result.content
    )


def test_no_missing_dependencies_keeps_content_unchanged(
    tmp_path,
):
    """
    B4 没有发现缺失依赖时，
    B5 不应该对 Makefile 内容做任何修改。
    """

    makefile = tmp_path / "Makefile"

    original_content = (
        "app: main.o\n"
        "\tgcc main.o -o app\n"
        "\n"
        "main.o: src/main.c\n"
        "\tgcc -c src/main.c -o main.o\n"
    )

    makefile.write_text(
        original_content,
        encoding="utf-8",
    )

    result = DependencyRepair().generate(
        makefile_path=makefile,
        missing_dependencies=[],
    )

    assert result.content == original_content
    assert result.applied_dependencies == []
    assert result.repaired_makefile is None

    # 仍然不能生成文件
    assert not (
        tmp_path / "Makefile.repaired"
    ).exists()


def test_write_supports_custom_output_path(
    tmp_path,
):
    """
    write() 应允许调用方指定输出路径。

    以后 B6 获得 Task Workspace 后，
    可以明确指定修复文件写到哪个位置。
    """

    makefile = tmp_path / "Makefile"

    makefile.write_text(
        "main.o: src/main.c\n",
        encoding="utf-8",
    )

    missing_dependencies = [
        MissingDependency(
            target="main.o",
            dependency="include/add.h",
            source_file="src/main.c",
        )
    ]

    custom_output = (
        tmp_path
        / "build"
        / "Makefile.repaired"
    )

    result = DependencyRepair().write(
        makefile_path=makefile,
        missing_dependencies=missing_dependencies,
        output_path=custom_output,
    )

    assert custom_output.exists()

    assert (
        result.repaired_makefile
        == custom_output.resolve()
    )

    assert (
        "main.o: include/add.h"
        in custom_output.read_text(
            encoding="utf-8"
        )
    )


def test_write_rejects_overwriting_original_makefile(
    tmp_path,
):
    """
    B5 的核心原则：
    绝不能覆盖用户原始 Makefile。
    """

    import pytest

    makefile = tmp_path / "Makefile"

    original_content = (
        "main.o: src/main.c\n"
    )

    makefile.write_text(
        original_content,
        encoding="utf-8",
    )

    missing_dependencies = [
        MissingDependency(
            target="main.o",
            dependency="include/add.h",
            source_file="src/main.c",
        )
    ]

    with pytest.raises(
        ValueError,
        match="must not overwrite",
    ):
        DependencyRepair().write(
            makefile_path=makefile,
            missing_dependencies=missing_dependencies,
            output_path=makefile,
        )

    # 即使调用非法，原文件也必须没变化
    assert (
        makefile.read_text(encoding="utf-8")
        == original_content
    )


def test_missing_makefile_raises_file_not_found(
    tmp_path,
):
    """
    输入 Makefile 不存在时应明确报错。
    """

    import pytest

    makefile = tmp_path / "Makefile"

    with pytest.raises(
        FileNotFoundError,
        match="Makefile does not exist",
    ):
        DependencyRepair().generate(
            makefile_path=makefile,
            missing_dependencies=[],
        )