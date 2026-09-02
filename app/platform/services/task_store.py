from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.core.errors import NotFoundError
from app.core.time import utc_now
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord


class TaskStore:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._closed = False
        if database_path is None or str(database_path) == ":memory:":
            connection_path = ":memory:"
        else:
            connection_path = Path(database_path)
            connection_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(connection_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._tasks: dict[UUID, TaskRecord] = {}
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                module TEXT NOT NULL,
                project_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                command_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                elapsed_ms INTEGER,
                progress INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                error TEXT,
                metadata_json TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(tasks)")
        }
        if "revision" not in columns:
            self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        self._connection.commit()
        rows = self._connection.execute("SELECT * FROM tasks").fetchall()
        self._tasks = {task.id: task for task in map(self._deserialize, rows)}

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None

    @classmethod
    def _deserialize(cls, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=UUID(row["id"]),
            module=BackendModuleName(row["module"]),
            project_id=UUID(row["project_id"]),
            task_type=TaskType(row["task_type"]),
            status=TaskStatus(row["status"]),
            command=json.loads(row["command_json"]),
            created_at=cls._parse_datetime(row["created_at"]),
            started_at=cls._parse_datetime(row["started_at"]),
            finished_at=cls._parse_datetime(row["finished_at"]),
            exit_code=row["exit_code"],
            elapsed_ms=row["elapsed_ms"],
            progress=row["progress"],
            result=json.loads(row["result_json"]),
            error=row["error"],
            metadata=json.loads(row["metadata_json"]),
            cancel_requested=bool(row["cancel_requested"]),
            revision=row["revision"],
        )

    def _upsert(self, task: TaskRecord) -> sqlite3.Cursor:
        return self._connection.execute(
            """
            INSERT INTO tasks (
                id, module, project_id, task_type, status, command_json, created_at,
                started_at, finished_at, exit_code, elapsed_ms, progress, result_json,
                error, metadata_json, cancel_requested, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                module = excluded.module,
                project_id = excluded.project_id,
                task_type = excluded.task_type,
                status = excluded.status,
                command_json = excluded.command_json,
                created_at = excluded.created_at,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                exit_code = excluded.exit_code,
                elapsed_ms = excluded.elapsed_ms,
                progress = excluded.progress,
                result_json = excluded.result_json,
                error = excluded.error,
                metadata_json = excluded.metadata_json,
                cancel_requested = excluded.cancel_requested,
                revision = tasks.revision + 1
            WHERE tasks.revision = excluded.revision
            """,
            (
                str(task.id),
                task.module.value,
                str(task.project_id),
                task.task_type.value,
                task.status.value,
                self._json(task.command),
                self._datetime(task.created_at),
                self._datetime(task.started_at),
                self._datetime(task.finished_at),
                task.exit_code,
                task.elapsed_ms,
                task.progress,
                self._json(task.result),
                task.error,
                self._json(task.metadata),
                int(task.cancel_requested),
                task.revision,
            ),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("task store is closed")

    def _load(self, task_id: UUID) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (str(task_id),)
        ).fetchone()
        return self._deserialize(row) if row is not None else None

    def save(self, task: TaskRecord) -> TaskRecord:
        with self._lock, self._connection:
            self._ensure_open()
            existing = task.id in self._tasks
            cursor = self._upsert(task)
            if cursor.rowcount != 1:
                latest = self._load(task.id)
                if latest is None:
                    raise NotFoundError(f"task not found: {task.id}")
                self._tasks[task.id] = latest
                return latest
            if existing:
                task.revision += 1
            self._tasks[task.id] = task
        return task

    def require(self, task_id: UUID) -> TaskRecord:
        with self._lock:
            self._ensure_open()
            task = self._load(task_id)
            if task is not None:
                self._tasks[task_id] = task
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    def list(self) -> list[TaskRecord]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute("SELECT * FROM tasks").fetchall()
            self._tasks = {task.id: task for task in map(self._deserialize, rows)}
            return sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)

    def request_cancel(self, task_id: UUID) -> TaskRecord:
        with self._lock, self._connection:
            self._ensure_open()
            current = self._load(task_id)
            if current is None:
                raise NotFoundError(f"task not found: {task_id}")
            self._connection.execute(
                """
                UPDATE tasks
                SET cancel_requested = 1,
                    status = CASE WHEN status = ? THEN ? ELSE status END,
                    revision = revision + 1
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    TaskStatus.PENDING.value,
                    TaskStatus.CANCELLED.value,
                    str(task_id),
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                ),
            )
            latest = self._load(task_id)
            assert latest is not None
            self._tasks[task_id] = latest
            return latest

    def try_start(self, task_id: UUID) -> TaskRecord | None:
        """Atomically move a task from PENDING to RUNNING."""
        with self._lock, self._connection:
            self._ensure_open()
            current = self._load(task_id)
            if current is None:
                raise NotFoundError(f"task not found: {task_id}")
            if current.status != TaskStatus.PENDING:
                return None

            started_at = utc_now()
            result = self._connection.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = ?, revision = revision + 1
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.RUNNING.value,
                    self._datetime(started_at),
                    str(task_id),
                    TaskStatus.PENDING.value,
                ),
            )
            if result.rowcount != 1:
                return None
            latest = self._load(task_id)
            assert latest is not None
            self._tasks[task_id] = latest
            return latest

    def recover_interrupted_tasks(self) -> list[TaskRecord]:
        """Mark tasks left active by a previous process as failed."""
        with self._lock, self._connection:
            self._ensure_open()
            interrupted: list[TaskRecord] = []
            finished_at = utc_now()
            for task_id in list(self._tasks):
                task = self._load(task_id)
                if task is None or task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                    if task is not None:
                        self._tasks[task_id] = task
                    continue
                task.status = TaskStatus.FAILED
                task.error = "interrupted by process restart"
                task.finished_at = finished_at
                cursor = self._upsert(task)
                if cursor.rowcount == 1:
                    task.revision += 1
                    self._tasks[task_id] = task
                    interrupted.append(task)
                else:
                    latest = self._load(task_id)
                    if latest is not None:
                        self._tasks[task_id] = latest
            return interrupted

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
