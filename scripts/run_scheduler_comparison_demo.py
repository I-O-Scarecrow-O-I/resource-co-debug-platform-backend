import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKLOAD_FILE = PROJECT_ROOT / "benchmarks" / "scheduler-workloads.json"
WORKER_FILE = PROJECT_ROOT / "scripts" / "scheduler_benchmark_worker.py"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def load_workload_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def default_core_ids(core_count: int) -> list[int]:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is not None:
        try:
            available = sorted(get_affinity(0))
        except OSError:
            available = []
        if len(available) >= core_count:
            return available[:core_count]

    available_count = os.cpu_count() or 1
    if available_count < core_count:
        raise RuntimeError(
            f"the demo requires {core_count} CPU cores, but only {available_count} are visible"
        )
    return list(range(core_count))


def parse_core_ids(value: str | None, recommended_core_count: int) -> list[int]:
    if value is None:
        return default_core_ids(recommended_core_count)

    core_ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not core_ids:
        raise ValueError("--core-ids must contain at least one CPU core ID")
    if len(set(core_ids)) != len(core_ids):
        raise ValueError("--core-ids must not contain duplicates")
    if any(core_id < 0 for core_id in core_ids):
        raise ValueError("--core-ids must not contain negative values")
    return core_ids


def build_comparison_payload(
    config: dict[str, Any],
    project_id: str,
    core_ids: list[int],
    python_executable: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    workloads = []
    for workload_index, workload in enumerate(config["workloads"], start=1):
        tasks = []
        for task_index, duration_seconds in enumerate(
            workload["durations_seconds"], start=1
        ):
            task_name = f"case-{workload_index}-task-{task_index}"
            tasks.append(
                {
                    "name": task_name,
                    "command": [
                        python_executable,
                        "benchmark_worker.py",
                        "--duration-seconds",
                        str(duration_seconds),
                        "--seed",
                        str(workload_index * 100 + task_index),
                        "--label",
                        task_name,
                    ],
                    "estimated_ms": max(round(duration_seconds * 1000), 1),
                    "metadata": {
                        "internal_development_case": True,
                        "requested_duration_seconds": duration_seconds,
                    },
                }
            )
        workloads.append({"name": workload["name"], "tasks": tasks})

    return {
        "module": "co_debug",
        "project_id": project_id,
        "workloads": workloads,
        "core_ids": core_ids,
        "timeout_seconds": timeout_seconds,
        "metadata": {
            "internal_development_demo": True,
            "acceptance_case": False,
            "workload_config": config["name"],
        },
    }


def build_project_archive() -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.write(WORKER_FILE, arcname="benchmark_worker.py")
        zip_file.writestr(
            "README.txt",
            "Internal C-module scheduler development workload. Not an acceptance case.\n",
        )
    return archive.getvalue()


def require_api_data(response: httpx.Response) -> Any:
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("message") or "backend API request failed")
    return payload["data"]


def create_project(client: httpx.Client) -> dict[str, Any]:
    return require_api_data(
        client.post(
            "/api/v1/projects",
            files={
                "archive": (
                    "c-scheduler-internal-benchmark.zip",
                    build_project_archive(),
                    "application/zip",
                )
            },
            data={"name": f"c-scheduler-internal-benchmark-{int(time.time())}"},
        )
    )


def wait_for_task(
    client: httpx.Client,
    task_id: str,
    poll_interval_seconds: float,
    wait_timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_timeout_seconds
    while time.monotonic() < deadline:
        task = require_api_data(client.get(f"/api/v1/tasks/{task_id}"))
        print(
            f"\r任务状态：{task['status']:<10} 进度：{task['progress']:>3}%",
            end="",
            flush=True,
        )
        if task["status"] in TERMINAL_STATUSES:
            print()
            return task
        time.sleep(poll_interval_seconds)
    raise TimeoutError(f"comparison task did not finish within {wait_timeout_seconds} seconds")


def print_summary(task: dict[str, Any]) -> None:
    result = task.get("result", {})
    print(f"任务状态：{task['status']}")
    print(f"负载组数：{result.get('workload_count')}")
    for workload in result.get("workload_results", []):
        print(
            f"- {workload['workload_name']}: "
            f"时耗差异 {workload['duration_spread_rate']}%，"
            f"改进率 {workload['improvement_rate']}%"
        )
    print(f"平均改进率：{result.get('average_improvement_rate')}%")
    print(f"满足当前合同判断：{result.get('meets_contract_target')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the internal C-module FIFO/optimized comparison demo."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workloads", type=Path, default=DEFAULT_WORKLOAD_FILE)
    parser.add_argument("--core-ids", help="Comma-separated CPU core IDs, for example 0,1")
    parser.add_argument("--task-timeout-seconds", type=int, default=30)
    parser.add_argument("--wait-timeout-seconds", type=int, default=120)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.25)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_workload_config(args.workloads)
    core_ids = parse_core_ids(args.core_ids, config["recommended_core_count"])

    if args.dry_run:
        payload = build_comparison_payload(
            config=config,
            project_id="00000000-0000-0000-0000-000000000000",
            core_ids=core_ids,
            python_executable=sys.executable,
            timeout_seconds=args.task_timeout_seconds,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print("提示：这是内部开发演示，不是甲方验收测试。")
    print(f"使用CPU核心：{core_ids}")
    with httpx.Client(base_url=args.base_url, timeout=30.0, trust_env=False) as client:
        project = create_project(client)
        payload = build_comparison_payload(
            config=config,
            project_id=project["id"],
            core_ids=core_ids,
            python_executable=sys.executable,
            timeout_seconds=args.task_timeout_seconds,
        )
        task = require_api_data(client.post("/api/v1/tasks/schedule-comparisons", json=payload))
        finished_task = wait_for_task(
            client=client,
            task_id=task["id"],
            poll_interval_seconds=args.poll_interval_seconds,
            wait_timeout_seconds=args.wait_timeout_seconds,
        )

    print_summary(finished_task)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(finished_task, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"完整结果已保存：{args.output.resolve()}")


if __name__ == "__main__":
    main()
