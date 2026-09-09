from pathlib import Path

from app.main import app
from app.platform.modules.registry import get_backend_modules


def test_co_debug_ownership_and_public_routes_remain_stable() -> None:
    project_root = Path(__file__).parents[1]
    platform_services = {
        "dependency_service.py",
        "debug_service.py",
        "metric_service.py",
        "scheduler_service.py",
        "schedule_execution_service.py",
        "schedule_comparison_service.py",
    }
    platform_schemas = {"debug.py", "dependencies.py", "metrics.py", "scheduler.py"}

    platform_service_root = project_root / "app/platform/services"
    platform_schema_root = project_root / "app/platform/schemas"
    module_service_root = project_root / "app/modules/co_debug/services"
    module_schema_root = project_root / "app/modules/co_debug/schemas"
    assert not any((platform_service_root / name).exists() for name in platform_services)
    assert not any((platform_schema_root / name).exists() for name in platform_schemas)
    assert all((module_service_root / name).is_file() for name in platform_services)
    assert all((module_schema_root / name).is_file() for name in platform_schemas)

    assert [(module.name, module.route_prefix) for module in get_backend_modules()] == [
        ("co_debug", "/modules/co-debug"),
        ("code_generation", "/modules/code-generation"),
    ]
    paths = set(app.openapi()["paths"])
    assert "/api/v1/modules/co-debug/dependencies/analyze" in paths
    assert "/api/v1/modules/co-debug/debug/sessions/{task_id}" in paths
    assert "/api/v1/modules/co-debug/metrics/build-success-rate" in paths
    assert "/api/v1/modules/code-generation/health" in paths
    assert "/api/v1/modules/code-generation/capabilities" in paths
    assert "/api/v1/modules/code-generation/runs" not in paths
    assert "/api/v1/tasks/build" in paths
    assert "/api/v1/tasks/debug" in paths
    assert "/api/v1/tasks/schedule-experiments" in paths
    assert "/api/v1/tasks/schedule-comparisons" in paths

    platform_task_source = (platform_service_root / "task_service.py").read_text(encoding="utf-8")
    co_debug_task_source = (module_service_root / "task_service.py").read_text(encoding="utf-8")
    code_generation_task_source = (
        project_root / "app/modules/code_generation/task_service.py"
    ).read_text(encoding="utf-8")
    assert "gdb" not in platform_task_source.lower()
    assert "app.modules.co_debug" not in platform_task_source
    assert "app.modules" not in platform_task_source
    assert "NaturalCC" not in platform_task_source
    assert "code_generation" not in platform_task_source
    assert "_naturalcc" not in platform_task_source
    assert "create_code_generation_task" not in platform_task_source
    assert "SchedulerService" not in platform_task_source
    assert "create_build_task" not in platform_task_source
    assert "create_debug_task" not in platform_task_source
    assert "create_schedule" not in platform_task_source
    assert "_run_schedule" not in platform_task_source
    assert "create_build_task" in co_debug_task_source
    assert "create_debug_task" in co_debug_task_source
    assert "create_schedule_experiment" in co_debug_task_source
    assert "create_schedule_comparison" in co_debug_task_source
    assert '"gdb"' in co_debug_task_source
    assert "create_code_generation_task" in code_generation_task_source
