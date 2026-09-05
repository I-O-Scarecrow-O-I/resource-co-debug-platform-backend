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
    ]
    paths = set(app.openapi()["paths"])
    assert "/api/v1/modules/co-debug/dependencies/analyze" in paths
    assert "/api/v1/modules/co-debug/debug/sessions/{task_id}" in paths
    assert "/api/v1/modules/co-debug/metrics/build-success-rate" in paths
