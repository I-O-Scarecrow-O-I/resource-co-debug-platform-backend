import shutil
import stat
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import AppError
from app.modules.co_debug.debug.manager import DebugSessionManager
from app.modules.co_debug.services.interactive_debug_service import InteractiveDebugService
from app.platform.domain.enums import TaskStatus, TaskType

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc is required")


def _compile_elf_files(tmp_path):
    source = tmp_path / "main.c"
    pie_executable = tmp_path / "app-pie"
    executable = tmp_path / "app-exec"
    object_file = tmp_path / "main.o"
    shared_library = tmp_path / "libsample.so"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    subprocess.run(["gcc", "-g", "-o", str(pie_executable), str(source)], check=True)
    subprocess.run(
        ["gcc", "-g", "-no-pie", "-o", str(executable), str(source)],
        check=True,
    )
    subprocess.run(["gcc", "-c", "-o", str(object_file), str(source)], check=True)
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-o", str(shared_library), str(source)],
        check=True,
    )
    return executable, pie_executable, object_file, shared_library


def _task(project_id, *, status=TaskStatus.SUCCEEDED, repair=True):
    return SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        task_type=TaskType.BUILD,
        status=status,
        metadata={"operation": "dependency_repair_build"} if repair else {},
        created_at=datetime.now(UTC),
    )


def test_debuggable_elf_type_does_not_depend_on_execute_permission(tmp_path):
    executable, pie_executable, object_file, shared_library = _compile_elf_files(tmp_path)
    executable.chmod(0o644)
    pie_executable.chmod(0o644)
    object_file.chmod(0o755)
    executable_text = tmp_path / "helper.sh"
    executable_text.write_text("#!/bin/sh\n", encoding="utf-8")
    executable_text.chmod(0o755)

    assert InteractiveDebugService._is_debuggable_executable(executable)
    assert InteractiveDebugService._is_debuggable_executable(pie_executable)
    assert not InteractiveDebugService._is_debuggable_executable(object_file)
    assert not InteractiveDebugService._is_debuggable_executable(shared_library)
    assert not InteractiveDebugService._is_debuggable_executable(executable_text)


def test_debug_task_copy_gets_only_owner_execute_permission(tmp_path):
    _, retained, object_file, shared_library = _compile_elf_files(tmp_path)
    retained.chmod(0o644)
    debug_copy = tmp_path / "debug" / "build" / "app"
    debug_copy.parent.mkdir(parents=True)
    shutil.copy2(retained, debug_copy)

    InteractiveDebugService._ensure_debuggable_executable(debug_copy)

    assert stat.S_IMODE(retained.stat().st_mode) == 0o644
    assert stat.S_IMODE(debug_copy.stat().st_mode) == 0o744

    executable_755 = tmp_path / "already-executable"
    shutil.copy2(retained, executable_755)
    executable_755.chmod(0o755)
    InteractiveDebugService._ensure_debuggable_executable(executable_755)
    assert stat.S_IMODE(executable_755.stat().st_mode) == 0o755

    text = tmp_path / "plain.txt"
    text.write_text("not an ELF\n", encoding="utf-8")
    invalid_files = [object_file, shared_library, text]
    original_modes = [path.stat().st_mode for path in invalid_files]
    for path in invalid_files:
        with pytest.raises(AppError, match="not a runnable ELF"):
            InteractiveDebugService._ensure_debuggable_executable(path)
    assert [path.stat().st_mode for path in invalid_files] == original_modes


def test_candidates_filter_status_project_and_actual_elf_type(tmp_path):
    executable, _, object_file, _ = _compile_elf_files(tmp_path)
    executable.chmod(0o644)
    object_file.chmod(0o755)
    note = tmp_path / "README.md"
    note.write_text("not executable\n", encoding="utf-8")

    selected_project = uuid4()
    other_project = uuid4()
    valid = _task(selected_project)
    failed = _task(selected_project, status=TaskStatus.FAILED)
    without_executable = _task(selected_project)
    old_valid = _task(other_project)
    artifacts = {
        valid.id: [
            ("build/app", executable.stat().st_size),
            ("build/main.o", object_file.stat().st_size),
        ],
        failed.id: [("build/app", executable.stat().st_size)],
        without_executable.id: [("README.md", note.stat().st_size)],
        old_valid.id: [("legacy/app", executable.stat().st_size)],
    }
    resolved = {
        (valid.id, "build/app"): executable,
        (valid.id, "build/main.o"): object_file,
        (failed.id, "build/app"): executable,
        (without_executable.id, "README.md"): note,
        (old_valid.id, "legacy/app"): executable,
    }
    task_service = SimpleNamespace(
        list_tasks=lambda: [valid, failed, without_executable, old_valid],
        list_task_artifacts=lambda task_id: artifacts[task_id],
        resolve_task_artifact=lambda task_id, path: resolved[(task_id, path)],
    )
    service = InteractiveDebugService(
        task_service=task_service,
        session_manager=DebugSessionManager(),
    )

    all_candidates = service.list_candidates()
    filtered = service.list_candidates(project_id=selected_project)

    assert {candidate.build_task_id for candidate in all_candidates} == {valid.id, old_valid.id}
    assert [candidate.build_task_id for candidate in filtered] == [valid.id]
    assert filtered[0].build_kind == "repair-build"
    assert [item.executable_path for item in filtered[0].executables] == ["build/app"]
