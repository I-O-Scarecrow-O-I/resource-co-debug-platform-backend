"""Preflight the Windows lab backend; --start launches only the backend."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND_ENV = ROOT / "deploy" / "backend.local.env.example"
BACKEND_KEYS = {
    "APP_ENV", "APP_HOST", "APP_PORT", "STORAGE_ROOT", "TASK_DATABASE_PATH",
    "ALLOWED_CORS_ORIGINS", "NATURALCC_BASE_URL", "NATURALCC_APPROVE_EXECUTE",
}


def _read_env(path: Path, label: str) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"{label} file is unavailable")
    values = dotenv_values(path, interpolate=False)
    if any(value is None for value in values.values()):
        raise ValueError(f"{label} contains an empty assignment")
    return {key: value for key, value in values.items() if value is not None}


def _preflight(backend_path: Path) -> tuple[Settings, dict[str, str]]:
    if os.name != "nt" or sys.version_info[:2] != (3, 11):
        raise ValueError("Windows and Python 3.11 are required")
    backend = _read_env(backend_path, "backend environment")
    if set(backend) != BACKEND_KEYS:
        raise ValueError("backend environment has missing or unsupported fields")
    try:
        url = urlsplit(backend["NATURALCC_BASE_URL"])
        port = url.port
    except ValueError:
        raise ValueError("NATURALCC_BASE_URL must be local HTTP loopback") from None
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("NATURALCC_BASE_URL must be local HTTP loopback")
    if backend["APP_HOST"] != "127.0.0.1":
        raise ValueError("APP_HOST must be 127.0.0.1")
    if not Path(backend["STORAGE_ROOT"]).is_absolute():
        raise ValueError("STORAGE_ROOT must be absolute")
    if not Path(backend["TASK_DATABASE_PATH"]).is_absolute():
        raise ValueError("TASK_DATABASE_PATH must be absolute")
    try:
        settings = Settings(_env_file=None, **backend)
    except ValueError as exc:
        raise ValueError("backend environment is invalid") from exc
    if settings.naturalcc_approve_execute:
        raise ValueError("NATURALCC_APPROVE_EXECUTE must remain false")
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    settings.task_database_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=settings.storage_root):
        pass
    with tempfile.TemporaryFile(dir=settings.task_database_path.parent):
        pass
    import_check = subprocess.run(
        [sys.executable, "-c", "import app.main; import uvicorn"],
        cwd=ROOT, env=_backend_environment(backend), capture_output=True, check=False,
    )
    if import_check.returncode != 0:
        raise ValueError("backend dependencies cannot be imported")
    return settings, backend


def _backend_environment(backend: dict[str, str]) -> dict[str, str]:
    allowed = (
        "ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH",
        "LOCALAPPDATA", "OS", "PATH", "PATHEXT", "PROGRAMDATA", "PUBLIC",
        "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE",
        "USERNAME", "WINDIR", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
        "LC_MESSAGES", "TZ",
    )
    child = {
        key: os.environ[key] for key in allowed if key in os.environ
    }
    child.update(backend)
    child["NO_PROXY"] = "localhost,127.0.0.1,::1"
    child.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "UV_OFFLINE": "1", "PIP_NO_INDEX": "1",
    })
    return child


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-env", type=Path, default=DEFAULT_BACKEND_ENV)
    parser.add_argument("--start", action="store_true", help="launch backend after preflight")
    args = parser.parse_args()
    try:
        settings, backend = _preflight(args.backend_env)
    except OSError:
        print("Backend preflight failed: local file access is unavailable", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Backend preflight failed: {exc}", file=sys.stderr)
        return 1
    print("Backend preflight OK")
    if not args.start:
        return 0
    return subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", settings.app_host,
         "--port", str(settings.app_port)],
        cwd=ROOT, env=_backend_environment(backend), check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
