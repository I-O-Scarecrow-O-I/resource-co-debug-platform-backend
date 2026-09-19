from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "docs" / "openapi.json"
SCHEMA_COMMAND = (
    "import json\n"
    "from app.main import create_app\n"
    "print(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True))\n"
)
FIXED_SETTINGS = {
    "APP_NAME": "resource-co-debug-platform-backend",
    "APP_ENV": "local",
    "APP_HOST": "0.0.0.0",
    "APP_PORT": "8000",
    "DEFAULT_TASK_TIMEOUT_SECONDS": "300",
    "MAX_LOG_LINES_PER_TASK": "2000",
    "NATURALCC_BASE_URL": "http://127.0.0.1:7860",
    "NATURALCC_CONNECT_TIMEOUT_SECONDS": "5",
    "NATURALCC_REQUEST_TIMEOUT_SECONDS": "30",
    "NATURALCC_APPROVE_EXECUTE": "false",
    "ALLOWED_CORS_ORIGINS": "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5173",
    "PYTHONUTF8": "1",
}


def render_openapi() -> str:
    with tempfile.TemporaryDirectory(prefix="resource-co-debug-openapi-") as directory:
        temporary_root = Path(directory)
        environment = os.environ.copy()
        environment.update(FIXED_SETTINGS)
        environment.update(
            {
                "STORAGE_ROOT": str(temporary_root / "workspaces"),
                "TASK_DATABASE_PATH": str(temporary_root / "tasks.sqlite3"),
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", SCHEMA_COMMAND],
            check=True,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the runtime OpenAPI schema.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when docs/openapi.json is missing or differs from the runtime schema.",
    )
    args = parser.parse_args()
    rendered = render_openapi()

    if args.check:
        snapshot_is_current = (
            OUTPUT_PATH.is_file() and OUTPUT_PATH.read_text(encoding="utf-8") == rendered
        )
        if not snapshot_is_current:
            print(f"OpenAPI snapshot is missing or stale: {OUTPUT_PATH}", file=sys.stderr)
            return 1
        return 0

    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
