from pathlib import Path

from app.platform.services.metric_service import AcceptanceMetricService
from scripts.run_scheduler_comparison_demo import build_comparison_payload, load_workload_config
from scripts.scheduler_benchmark_worker import burn_cpu

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD_FILE = PROJECT_ROOT / "benchmarks" / "scheduler-workloads.json"


def test_internal_fixture_contains_three_eligible_workloads() -> None:
    config = load_workload_config(WORKLOAD_FILE)
    metric_service = AcceptanceMetricService()

    assert config["recommended_core_count"] == 2
    assert len(config["workloads"]) == 3
    for workload in config["workloads"]:
        durations_ms = [round(value * 1000) for value in workload["durations_seconds"]]
        assert len(durations_ms) > config["recommended_core_count"]
        assert (
            metric_service.duration_spread_rate(durations_ms)
            >= metric_service.REQUIRED_DURATION_SPREAD_RATE
        )


def test_demo_payload_marks_workloads_as_internal_only() -> None:
    config = load_workload_config(WORKLOAD_FILE)

    payload = build_comparison_payload(
        config=config,
        project_id="00000000-0000-0000-0000-000000000000",
        core_ids=[2, 3],
        python_executable="python",
        timeout_seconds=30,
    )

    assert payload["core_ids"] == [2, 3]
    assert len(payload["workloads"]) == 3
    assert payload["metadata"]["internal_development_demo"] is True
    assert payload["metadata"]["acceptance_case"] is False
    assert all(
        task["metadata"]["internal_development_case"] is True
        for workload in payload["workloads"]
        for task in workload["tasks"]
    )


def test_benchmark_worker_performs_cpu_work() -> None:
    result = burn_cpu(duration_seconds=0.005, seed=7)

    assert result["elapsed_seconds"] >= 0.005
    assert result["iterations"] > 0
