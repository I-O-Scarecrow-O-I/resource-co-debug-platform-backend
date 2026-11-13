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


# SQLite 是状态与资源归属的权威；内存索引随查询和事务结果刷新。
class TaskStore:
    # 这个实例在初始化时确定持久化位置，后续方法不会随工作目录切换数据库。
    # 数据库连接属于存储对象；调用方只持有任务记录，不应单独管理连接。
    def __init__(self, database_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._closed = False
        # 缺省路径供短生命周期调用使用。
        # 落盘模式会先创建父目录，任务和资源标记可跨服务重启保留。
        if database_path is None or str(database_path) == ":memory:":
            connection_path = ":memory:"
        else:
            connection_path = Path(database_path)
            connection_path.parent.mkdir(parents=True, exist_ok=True)
        # 连接由 API 和后台线程共用，访问以服务锁及写事务串行化。
        self._connection = sqlite3.connect(connection_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._tasks: dict[UUID, TaskRecord] = {}
        # 建表、历史数据回填和字段迁移作为一笔写事务。
        # 提前取得写入权，避免另一个初始化者同时依据旧表状态回填。
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            # 探测必须早于 CREATE IF NOT EXISTS，否则无法辨别新建表与曾被主动清空的旧表。
            # 后续迁移以这个先前事实决定是否补齐历史关系。
            workspace_holds_existed = self._table_exists("task_workspace_holds")
            artifacts_available_existed = self._table_exists(
                "task_artifacts_available"
            )
            # 任务记录保存生命周期与模块数据。
            # 副本保留和发布资格使用独立关系，旧记录不必重写整份结果。
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
                """,
            )
            # 清理表不是目录清单，而是尚未执行完的持久化意图。
            # 带回调元数据时，删除目录后才兑现其中的更新。
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_workspace_cleanup (
                    task_id TEXT PRIMARY KEY,
                    completion_metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            # 占用用任务 ID 去重，不是可叠加的引用计数。
            # 模块必须统一协调何时释放同一个任务的副本。
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_workspace_holds (
                    task_id TEXT PRIMARY KEY
                )
                """
            )
            # 对早期数据库迁移旧占用记录，仅在新表首次创建时回填。
            # 旧版代码生成将远端清理状态记在元数据中。
            # 迁移把这些可能仍在用的副本视为被持有。
            if not workspace_holds_existed:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO task_workspace_holds (task_id)
                    SELECT id
                    FROM tasks
                    WHERE module = ?
                      AND json_extract(metadata_json, '$.cleanup_pending') = 1
                    """,
                    (BackendModuleName.CODE_GENERATION.value,),
                )
            # 发布关系只保存任务身份，没有把文件路径持久化到另一份索引中。
            # 实际路径仍由工作区服务按当前存储根解析，权限标记与文件存在性是两道条件。
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_artifacts_available (
                    task_id TEXT PRIMARY KEY
                )
                """
            )
            # 产物发布标记首次引入时迁移旧任务，之后不在每次启动重新推断。
            # 回填范围仅限曾发布固定副本的成功任务类型。
            # 扫描或调度结果不会因为是成功终态就获得下载资格。
            if not artifacts_available_existed:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO task_artifacts_available (task_id)
                    SELECT id
                    FROM tasks
                    WHERE status = ?
                      AND task_type IN (?, ?, ?, ?)
                    """,
                    (
                        TaskStatus.SUCCEEDED.value,
                        TaskType.BUILD.value,
                        TaskType.CODE_GENERATION.value,
                        TaskType.CODE_REPAIR.value,
                        TaskType.CODE_REFACTOR.value,
                    ),
                )
            # 增列判断与表存在性判断分开：已有任务表也可能来自不带修订号的旧版。
            # 默认值使这批旧记录可以直接参与第一次条件更新。
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(tasks)")
            }
            # 旧表可能没有修订号，迁移补齐后才能对陈旧写入做比较。
            if "revision" not in columns:
                self._connection.execute(
                    "ALTER TABLE tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            cleanup_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(task_workspace_cleanup)"
                )
            }
            # 旧清理记录继续有效，空对象表示没有随清理提交的字段更新。
            if "completion_metadata_json" not in cleanup_columns:
                self._connection.execute(
                    "ALTER TABLE task_workspace_cleanup "
                    "ADD COLUMN completion_metadata_json TEXT NOT NULL DEFAULT '{}'"
                )
            # 先提交模式和回填，再载入任务索引。
            # 读到的对象对应迁移完成后的字段契约。
            self._connection.commit()
            rows = self._connection.execute("SELECT * FROM tasks").fetchall()
            self._tasks = {task.id: task for task in map(self._deserialize, rows)}
        # 初始化失败先结束事务再关闭连接，调用者不会拿到部分初始化的存储。
        except BaseException:
            # 提交后发生的载入错误不会尝试撤销已提交的模式迁移。
            # 当前实例仍然不可继续使用，重新打开时应从数据库建立新索引。
            if self._connection.in_transaction:
                self._connection.rollback()
            self._connection.close()
            self._closed = True
            raise

    @staticmethod
    # 固定 JSON 编码便于持久化和比较任务字段；中文数据保留原字符。
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    # 用 ISO 形式存储时间，缺省阶段保持 SQL NULL。
    # 读取后仍可区分还未开始和时间戳实际存在。
    def _datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None

    @classmethod
    # 在存储边界将状态、时间和 JSON 还原成领域对象。
    def _deserialize(cls, row: sqlite3.Row) -> TaskRecord:
        # 标识、枚举和时间还原时不猜测非法持久值。
        # 上层不能把无法反序列化的状态错当成某个合法生命周期状态。
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

    # 冲突时只允许相同修订号的快照覆盖整行。
    # 部分更新已提升修订号时，后到的全量保存不能撤销它。
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

    # 访问由上层锁保护；已关闭的实例不重新连接。
    # 缓存替换与重启由服务生命周期负责。
    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("task store is closed")

    # 查询模式元数据只用于迁移探测，不把表名拼接成 SQL。
    def _table_exists(self, table_name: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    # 内部读取返回缺省对象，让事务调用方区分未命中与已知任务。
    # 外部必需读取则转换成统一的不存在错误。
    def _load(self, task_id: UUID) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (str(task_id),)
        ).fetchone()
        return self._deserialize(row) if row is not None else None

    # 普通保存通过修订检查保护已被其他请求更新的状态。
    def save(self, task: TaskRecord) -> TaskRecord:
        with self._lock, self._connection:
            self._ensure_open()
            # 是否增加入参对象的版本由当前索引是否已有该任务决定。
            # 真正防止竞争覆盖的是数据库条件，不能仅靠这个进程内存在性判断。
            existing = task.id in self._tasks
            cursor = self._upsert(task)
            # 写入没有命中时回读当前记录，不把陈旧对象当作成功保存。
            if cursor.rowcount != 1:
                # 失败更新不合并两个任务对象的字段。
                # 这里选择整份最新快照；只想合并模块数据的调用方应走元数据补丁入口。
                latest = self._load(task.id)
                if latest is None:
                    raise NotFoundError(f"task not found: {task.id}")
                self._tasks[task.id] = latest
                return latest
            # 更新命中后同步内存对象的修订号；新插入的记录沿用起始版本。
            if existing:
                task.revision += 1
            self._tasks[task.id] = task
        return task

    # 直接读数据库获得当前状态，避免后台继续使用旧的取消标记。
    def require(self, task_id: UUID) -> TaskRecord:
        with self._lock:
            self._ensure_open()
            # require 在同一锁区读取并替换索引引用。
            # 返回的是该次读取的任务快照，后续另一次更新仍可能使其过时。
            task = self._load(task_id)
            if task is not None:
                self._tasks[task_id] = task
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    # 以数据库当前记录重建索引，再按任务创建时间交付列表。
    def list(self) -> list[TaskRecord]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute("SELECT * FROM tasks").fetchall()
            self._tasks = {task.id: task for task in map(self._deserialize, rows)}
            # 列表结果按登记时间排序，更新进度不会把旧任务挪到最前。
            # 两个时间相同的任务没有额外的业务排序键。
            return sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)

    def request_cancel(self, task_id: UUID) -> TaskRecord:
        task, _ = self.request_cancel_with_transition(task_id)
        return task

    # 排队任务可以原地结束；运行任务先持久化请求，由执行者收尾。
    # 只有首个对活跃任务的取消才返回发生迁移。
    def request_cancel_with_transition(self, task_id: UUID) -> tuple[TaskRecord, bool]:
        # 运行任务只设置请求位，保留执行者仍有机会提交必要收尾数据。
        # 重复取消或终态任务不命中更新条件，也不改变修订号。
        with self._lock, self._connection:
            self._ensure_open()
            # 状态、结束时间和错误文案的条件分支都以更新前的状态为准。
            # 仍排队的任务一次写入即可结束，不依赖一个尚未启动的 worker 来补尾。
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET cancel_requested = 1,
                    status = CASE WHEN status = ? THEN ? ELSE status END,
                    finished_at = CASE WHEN status = ? THEN ? ELSE finished_at END,
                    error = CASE WHEN status = ? THEN ? ELSE error END,
                    revision = revision + 1
                WHERE id = ? AND status IN (?, ?) AND cancel_requested = 0
                """,
                (
                    TaskStatus.PENDING.value,
                    TaskStatus.CANCELLED.value,
                    TaskStatus.PENDING.value,
                    self._datetime(utc_now()),
                    TaskStatus.PENDING.value,
                    "cancelled",
                    str(task_id),
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                ),
            )
            # 返回数据库当前行而不是重用入参。
            # 调用方据此决定是否记录第一次取消或通知持有资源的 worker。
            latest = self._load(task_id)
            if latest is None:
                raise NotFoundError(f"task not found: {task_id}")
            # 即使请求没有触发迁移也刷新缓存。
            # 重复取消返回当前终态或已登记取消的记录，避免调用端看到自己传入的旧视图。
            self._tasks[task_id] = latest
            return latest, cursor.rowcount == 1

    # 限制进度范围，并在数据库保证不倒退、不更新终态或已请求取消的记录。
    def update_progress(self, task_id: UUID, progress: int) -> TaskRecord:
        if not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")

        # 在同一事务写入并回读。
        # 即使进度被拒绝，调用方也能知道是否已取消或完成。
        with self._lock, self._connection:
            self._ensure_open()
            # 同值更新也可命中单调条件；修订号不是进度增量。
            # 不能用版本变化判断界面上的百分比是否真的增加。
            self._connection.execute(
                """
                UPDATE tasks
                SET progress = ?, revision = revision + 1
                WHERE id = ?
                  AND status = ?
                  AND cancel_requested = 0
                  AND progress <= ?
                """,
                (progress, str(task_id), TaskStatus.RUNNING.value, progress),
            )
            # 回读不代表传入百分比被采纳。
            # 调用方需要检查持久化状态和进度本身，尤其是在任务结束与进度回调竞争时。
            latest = self._load(task_id)
            if latest is None:
                raise NotFoundError(f"task not found: {task_id}")
            self._tasks[task_id] = latest
            return latest

    # 仅正在运行且未取消的任务接受准备器回写，避免竞态发布过期命令。
    def update_command_if_running(self, task_id: UUID, command: list[str]) -> TaskRecord | None:
        """Persist a prepared command only while its worker still owns an active task."""
        with self._lock, self._connection:
            self._ensure_open()
            # 更新命令时只写命令字段与版本，不提交准备器握有的完整记录。
            # 预处理期间并发写入的进度或元数据因此不会被抹掉。
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET command_json = ?, revision = revision + 1
                WHERE id = ? AND status = ? AND cancel_requested = 0
                """,
                (
                    self._json(command),
                    str(task_id),
                    TaskStatus.RUNNING.value,
                ),
            )
            # 未命中运行条件时保留最新状态供其他读取。
            # 准备器据 None 放弃发起实际进程。
            latest = self._load(task_id)
            if latest is None:
                raise NotFoundError(f"task not found: {task_id}")
            self._tasks[task_id] = latest
            # None 专指已存在任务没有满足更新条件；不存在任务在上方走独立错误。
            # 准备器可据此放弃启动，而无需自行解释 SQL 结果。
            return latest if cursor.rowcount == 1 else None

    # 只合并模块自己的字段，不用本地快照覆盖其他回调的元数据。
    def merge_metadata(self, task_id: UUID, updates: dict[str, object]) -> TaskRecord:
        """Atomically merge adapter-owned metadata without saving a stale TaskRecord."""
        with self._lock, self._connection:
            self._ensure_open()
            # 由 SQLite 在现有 JSON 上应用补丁。
            # 空值采用 JSON Merge Patch 的删除语义，不是普通字典赋值。
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET metadata_json = json_patch(metadata_json, ?), revision = revision + 1
                WHERE id = ?
                """,
                (self._json(updates), str(task_id)),
            )
            # 这里与有状态前置条件的更新不同，零命中意味着没有该任务。
            # 补丁可在终态后使用，供资源清理写回模块自己的诊断。
            if cursor.rowcount != 1:
                raise NotFoundError(f"task not found: {task_id}")
            latest = self._load(task_id)
            assert latest is not None
            self._tasks[task_id] = latest
            return latest

    # 单独登记待清理任务，文件系统操作失败时可以在重启后恢复。
    def mark_workspace_cleanup_pending(self, task_id: UUID) -> None:
        with self._lock, self._connection:
            self._ensure_open()
            # 清理队列通过应用检查引用有效性。
            # 先验证任务再写记录，避免为不存在的 ID 建立无法解析工程归属的回收意图。
            if self._load(task_id) is None:
                raise NotFoundError(f"task not found: {task_id}")
            # 重复登记不覆盖已有完成数据，避免失败重试抹掉先前保存的补丁。
            self._connection.execute(
                "INSERT OR IGNORE INTO task_workspace_cleanup (task_id) VALUES (?)",
                (str(task_id),),
            )

    # 显式撤销队列项，只影响清理意图，不改变任务结果。
    def clear_workspace_cleanup_pending(self, task_id: UUID) -> None:
        with self._lock, self._connection:
            self._ensure_open()
            # 此接口仅撤销清理意图，不会兑现原先随队列保存的完成补丁。
            # 实际完成的调用方应使用会同时处理元数据的收尾入口。
            self._connection.execute(
                "DELETE FROM task_workspace_cleanup WHERE task_id = ?",
                (str(task_id),),
            )

    # 返回可解析的任务标识；异常旧记录不能阻断其他目录的恢复。
    def list_workspace_cleanup_pending(self) -> list[UUID]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT task_id FROM task_workspace_cleanup ORDER BY task_id"
            ).fetchall()
        # 解析 ID 放在查询完成后进行，异常行不改变持久化队列本身。
        # 故障恢复可以继续处理有效项，原始异常行仍可排查。
        pending: list[UUID] = []
        for row in rows:
            try:
                pending.append(UUID(row["task_id"]))
            except (AttributeError, TypeError, ValueError):
                continue
        return pending

    # 占用关系与可选元数据合并在同一写事务内建立。
    def hold_workspaces(
        self,
        task_id: UUID,
        metadata_updates: dict[str, object] | None = None,
    ) -> TaskRecord:
        # 先序列化可选更新，再开启占用事务。
        # 无法持久化的补丁不会留下已持有但缺少上下文的副本。
        serialized_updates = (
            self._json(metadata_updates) if metadata_updates is not None else None
        )
        with self._lock:
            self._ensure_open()
            # 占用事务结束后才把回读记录交给调用方。
            # 调用方拿到成功结果时，后续重启已能查到持久化占用。
            with self._connection:
                if self._load(task_id) is None:
                    raise NotFoundError(f"task not found: {task_id}")
                # 元数据更新与占用标记一起提交，恢复时可以同时观察两者。
                # 没提供更新时不无故提高任务修订号。
                if serialized_updates is not None:
                    self._connection.execute(
                        """
                        UPDATE tasks
                        SET metadata_json = json_patch(metadata_json, ?),
                            revision = revision + 1
                        WHERE id = ?
                        """,
                        (serialized_updates, str(task_id)),
                    )
                # 占用登记可重入；重复登记不创建多个待释放标记。
                self._connection.execute(
                    "INSERT OR IGNORE INTO task_workspace_holds (task_id) VALUES (?)",
                    (str(task_id),),
                )
                latest = self._load(task_id)
                assert latest is not None
            self._tasks[task_id] = latest
            return latest

    # 释放占用可以连同完成元数据提交，避免后继消费者看到不一致状态。
    def release_workspaces(
        self,
        task_id: UUID,
        metadata_updates: dict[str, object] | None = None,
    ) -> TaskRecord:
        # 单纯释放不表示目录要删。
        # 保留的结果副本可以继续为下载或后续任务提供输入。
        serialized_updates = (
            self._json(metadata_updates) if metadata_updates is not None else None
        )
        with self._lock:
            self._ensure_open()
            # 释放同样检查任务是否仍存在；不能把未知身份与已释放的有效身份混为一谈。
            # 存在时即使没有占用行也可更新附带元数据。
            with self._connection:
                if self._load(task_id) is None:
                    raise NotFoundError(f"task not found: {task_id}")
                if serialized_updates is not None:
                    self._connection.execute(
                        """
                        UPDATE tasks
                        SET metadata_json = json_patch(metadata_json, ?),
                            revision = revision + 1
                        WHERE id = ?
                        """,
                        (serialized_updates, str(task_id)),
                    )
                # 删除占用的动作幂等，不依赖上一次释放是否已成功。
                # 读取最新任务反映同一事务可能提交的元数据。
                self._connection.execute(
                    "DELETE FROM task_workspace_holds WHERE task_id = ?",
                    (str(task_id),),
                )
                latest = self._load(task_id)
                assert latest is not None
            self._tasks[task_id] = latest
            return latest

    # 先持久化清理意图与完成数据，再移除占用，避免留下不可恢复的目录。
    def queue_workspace_cleanup_and_release_hold(
        self,
        task_id: UUID,
        completion_metadata: dict[str, object] | None = None,
    ) -> TaskRecord:
        # 完成数据先挂在清理意图上，删除磁盘内容后才写进任务。
        # 重复安排清理会合并新旧补丁，保留不同字段的完成信息。
        serialized_metadata = self._json(completion_metadata or {})
        with self._lock:
            self._ensure_open()
            with self._connection:
                if self._load(task_id) is None:
                    raise NotFoundError(f"task not found: {task_id}")
                # 重复安排清理仅合并完成字段，不要求先读取并解释旧记录。
                # 调用方可以提交本次已确认的字段而保留之前安排的其他字段。
                self._connection.execute(
                    """
                    INSERT INTO task_workspace_cleanup (
                        task_id, completion_metadata_json
                    ) VALUES (?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        completion_metadata_json = json_patch(
                            task_workspace_cleanup.completion_metadata_json,
                            excluded.completion_metadata_json
                        )
                    """,
                    (str(task_id), serialized_metadata),
                )
                # 释放前已在同一事务登记清理，因此重启不会看见两者都没有的间隙。
                self._connection.execute(
                    "DELETE FROM task_workspace_holds WHERE task_id = ?",
                    (str(task_id),),
                )
                # 安排回收与移除占用均未直接操作文件系统。
                # 返回任务记录时，后续清理仍需要在事务外由任务服务执行。
                latest = self._load(task_id)
                assert latest is not None
            self._tasks[task_id] = latest
            return latest

    # 回收完成后提交排队时记录的元数据，并撤销持久化清理意图。
    def complete_workspace_cleanup(self, task_id: UUID) -> TaskRecord:
        with self._lock:
            self._ensure_open()
            with self._connection:
                if self._load(task_id) is None:
                    raise NotFoundError(f"task not found: {task_id}")
                # 回收执行方无需把完成补丁重新带进来；它以队列里已经持久化的值为准。
                # 这也适用于进程重启后接手的清理。
                row = self._connection.execute(
                    """
                    SELECT completion_metadata_json
                    FROM task_workspace_cleanup
                    WHERE task_id = ?
                    """,
                    (str(task_id),),
                ).fetchone()
                # 没有排队记录时此调用只是读取现有任务。
                # 这使已经执行过的清理可以安全收尾两次。
                if row is not None:
                    completion_metadata = json.loads(
                        row["completion_metadata_json"]
                    )
                    # 损坏的持久化补丁不应被视为空补丁。
                    # 保留待处理项供故障诊断，而不是误报清理完成。
                    if not isinstance(completion_metadata, dict):
                        raise ValueError("completion metadata must be a JSON object")
                    # 空对象无需写回任务，因此不会为没有业务数据变化的补丁额外增加版本。
                    # 即使没有补丁，清理记录仍会在成功收尾时移除。
                    if completion_metadata:
                        self._connection.execute(
                            """
                            UPDATE tasks
                            SET metadata_json = json_patch(metadata_json, ?),
                                revision = revision + 1
                            WHERE id = ?
                            """,
                            (self._json(completion_metadata), str(task_id)),
                        )
                    # 应用补丁与删除待清理项同步提交。
                    # 中途失败保留恢复所需数据，不会只删队列而遗漏任务更新。
                    self._connection.execute(
                        "DELETE FROM task_workspace_cleanup WHERE task_id = ?",
                        (str(task_id),),
                    )
                latest = self._load(task_id)
                assert latest is not None
            self._tasks[task_id] = latest
            return latest

    # 返回持久化占用事实，而不是根据任务是否成功猜测目录能否删除。
    def workspaces_are_held(self, task_id: UUID) -> bool:
        # 目录是否被保护可按 ID 查询，不需要打开工作区或检查路径是否存在。
        # 这里回答的是逻辑占用，物理目录状态由另一个层负责。
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT 1 FROM task_workspace_holds WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        return row is not None

    # 恢复时按持有关系枚举，而不是仅筛选运行中的任务。
    # 进程重启后任务已经转终态，外部运行仍可能存在。
    def list_held_workspaces(self) -> list[UUID]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT task_id FROM task_workspace_holds ORDER BY task_id"
            ).fetchall()
        # 查询结果只用作待检查身份列表，不把它视为永久占用的快照。
        # 执行回收前仍应根据当前关系判断占用是否已释放。
        held: list[UUID] = []
        for row in rows:
            try:
                held.append(UUID(row["task_id"]))
            except (AttributeError, TypeError, ValueError):
                continue
        return held

    # 产物是否对 API 可见由独立发布记录表示，与磁盘上碰巧存在文件区分。
    def artifacts_are_available(self, task_id: UUID) -> bool:
        # 这里只验证发布关系，不尝试枚举文件。
        # 发布后若副本遭外部删除，解析实际路径仍然可能报不存在。
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT 1 FROM task_artifacts_available WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
        return row is not None

    def finalize(self, task: TaskRecord) -> TaskRecord:
        finalized, _ = self.finalize_with_transition(task)
        return finalized

    # 只允许正在运行的记录写入终态，并在同一事务处理相关资源标记。
    # 终态写入以数据库中的 RUNNING 为前置条件。
    # 并发调用只允许一个收尾者实际发生状态迁移。
    def finalize_with_transition(
        self,
        task: TaskRecord,
        *,
        artifacts_on_success: bool = False,
        queue_workspace_cleanup_on_finalize: bool = False,
        preserve_workspace_on_success: bool = False,
    ) -> tuple[TaskRecord, bool]:
        # 终态类型在尝试更新数据库之前验证。
        # 传入仍在排队或运行的快照是调用错误，不等同于竞争中未迁移。
        if task.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            raise ValueError("task must be finalized with a terminal status")

        # 模块没给结束时间时使用当前 UTC，取消覆盖也共享该结束时刻。
        finished_at = self._datetime(task.finished_at or utc_now())
        is_cancelled = task.status == TaskStatus.CANCELLED
        with self._lock, self._connection:
            self._ensure_open()
            # 取消请求优先于 worker 的完成结果；同时保护已有进度与结果字段。
            # 取消覆盖时不发布 worker 新算出的结果或退出码。
            # 时长仍可补齐，保留排查结束过程所需信息。
            cursor = self._connection.execute(
                """
                UPDATE tasks
                SET status = CASE WHEN cancel_requested = 1 OR ? THEN ? ELSE ? END,
                    finished_at = ?,
                    exit_code = CASE
                        WHEN cancel_requested = 1 OR ? THEN exit_code ELSE ?
                    END,
                    elapsed_ms = CASE
                        WHEN cancel_requested = 1 OR ? THEN COALESCE(?, elapsed_ms) ELSE ?
                    END,
                    progress = CASE
                        WHEN cancel_requested = 1 OR ? THEN progress ELSE MAX(progress, ?)
                    END,
                    result_json = CASE
                        WHEN cancel_requested = 1 OR ? THEN result_json ELSE ?
                    END,
                    error = CASE
                        WHEN cancel_requested = 1 OR ? THEN 'cancelled' ELSE ?
                    END,
                    revision = revision + 1
                WHERE id = ? AND status = ?
                """,
                (
                    is_cancelled,
                    TaskStatus.CANCELLED.value,
                    task.status.value,
                    finished_at,
                    is_cancelled,
                    task.exit_code,
                    is_cancelled,
                    task.elapsed_ms,
                    task.elapsed_ms,
                    is_cancelled,
                    task.progress,
                    is_cancelled,
                    self._json(task.result),
                    is_cancelled,
                    task.error,
                    str(task.id),
                    TaskStatus.RUNNING.value,
                ),
            )
            # 后续资源操作必须使用持久化回读的终态。
            # 取消竞态下，该状态可能与执行器建议的状态不同。
            latest = self._load(task.id)
            if latest is None:
                raise NotFoundError(f"task not found: {task.id}")
            # 只有本次确实进入成功终态，且调用者请求公开产物，才发布下载标记。
            if (
                cursor.rowcount == 1
                and artifacts_on_success
                and latest.status == TaskStatus.SUCCEEDED
            ):
                # 写入发布关系仍处于终态事务内。
                # 如果发布写入失败，调用方不会拿到一个已经提交成功但漏掉标记的终态。
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO task_artifacts_available (task_id)
                    VALUES (?)
                    """,
                    (str(task.id),),
                )
            # 声明保留成功副本的任务不进入清理队列，失败终态仍按回收规则处理。
            should_queue_workspace_cleanup = not (
                latest.status == TaskStatus.SUCCEEDED
                and preserve_workspace_on_success
            )
            # 副本回收是同一状态迁移的一部分。
            # 保留成功副本的选项不会阻止失败副本入队。
            if (
                cursor.rowcount == 1
                and queue_workspace_cleanup_on_finalize
                and should_queue_workspace_cleanup
            ):
                # 即使任务结束，正在被模块持有的副本也不能立即安排删除。
                held = self._connection.execute(
                    "SELECT 1 FROM task_workspace_holds WHERE task_id = ?",
                    (str(task.id),),
                ).fetchone()
                # 占用解除后的回收由释放路径重新安排。
                # 当前收尾不能替远端消费者断言副本已经不再使用。
                if held is None:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO task_workspace_cleanup (task_id)
                        VALUES (?)
                        """,
                        (str(task.id),),
                    )
            self._tasks[task.id] = latest
            # 返回布尔值用于判断要不要写终态日志，不等同于任务成功。
            return latest, cursor.rowcount == 1

    # 任务认领将排队状态切到运行；已被别人领取或取消则返回未认领。
    def try_start(self, task_id: UUID) -> TaskRecord | None:
        """Atomically move a task from PENDING to RUNNING."""
        with self._lock, self._connection:
            self._ensure_open()
            # 先读取现有记录以区分不存在与不能认领。
            # 真正的认领结果仍依赖下方带状态前置条件的写入。
            current = self._load(task_id)
            if current is None:
                raise NotFoundError(f"task not found: {task_id}")
            if current.status != TaskStatus.PENDING:
                return None

            # 开始时刻和运行状态同时落库，避免状态已运行而时间尚未写入。
            started_at = utc_now()
            # 认领写入还要重查取消位；不能只依赖此前读取的 PENDING。
            result = self._connection.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = ?, revision = revision + 1
                WHERE id = ? AND status = ? AND cancel_requested = 0
                """,
                (
                    TaskStatus.RUNNING.value,
                    self._datetime(started_at),
                    str(task_id),
                    TaskStatus.PENDING.value,
                ),
            )
            # 没有成功写入时不生成新的开始时间或返回可执行句柄。
            # 另一个调用方可能已经认领或取消这条任务。
            if result.rowcount != 1:
                return None
            latest = self._load(task_id)
            assert latest is not None
            self._tasks[task_id] = latest
            return latest

    # 重启后的中断恢复只结束旧运行，不会重放命令。
    def recover_interrupted_tasks(self) -> list[TaskRecord]:
        """Atomically terminalize tasks left active by a previous process."""
        with self._lock:
            self._ensure_open()
            # 恢复的读集合与状态更新必须在一个事务内。
            # 否则收尾日志可能对应错误的一批任务，遗漏真正被转为终态的记录。
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                # 中断恢复先捕获本轮要终态化的任务 ID。
                # 后续只回读这些行，调用者可以逐项处理日志和副本。
                rows = self._connection.execute(
                    "SELECT id FROM tasks WHERE status IN (?, ?)",
                    (TaskStatus.PENDING.value, TaskStatus.RUNNING.value),
                ).fetchall()
                task_ids = [row["id"] for row in rows]
                if task_ids:
                    # 已登记的取消继续按取消解释。
                    # 其余未结束的任务转为中断失败，不用当前机器重新执行原命令。
                    self._connection.execute(
                        """
                        UPDATE tasks
                        SET status = CASE WHEN cancel_requested = 1 THEN ? ELSE ? END,
                            error = CASE
                                WHEN cancel_requested = 1 THEN ? ELSE ?
                            END,
                            finished_at = ?,
                            revision = revision + 1
                        WHERE status IN (?, ?)
                        """,
                        (
                            TaskStatus.CANCELLED.value,
                            TaskStatus.FAILED.value,
                            "cancelled",
                            "interrupted by process restart",
                            self._datetime(utc_now()),
                            TaskStatus.PENDING.value,
                            TaskStatus.RUNNING.value,
                        ),
                    )
                    # 事务内读回转换后的记录，避免用旧状态驱动启动时回收。
                    recovered_rows = [
                        self._connection.execute(
                            "SELECT * FROM tasks WHERE id = ?", (task_id,)
                        ).fetchone()
                        for task_id in task_ids
                    ]
                # 没有活动记录时显式使用空结果。
                # 调用者不会为已经正常结束的任务再补一条中断日志。
                else:
                    recovered_rows = []
                self._connection.commit()
            # 恢复出错回滚整次状态迁移。
            # 内存索引只在成功提交后更新，避免缓存先行。
            except Exception:
                self._connection.rollback()
                raise
            # 反序列化的是更新后的数据库行，而非启动前的内存对象。
            # 返回的时间、版本和错误原因可直接用于后续恢复处理。
            recovered = [self._deserialize(row) for row in recovered_rows]
            self._tasks.update({task.id: task for task in recovered})
            return recovered

    # 在锁内关闭一次连接，防止并发 API 操作使用半关闭对象。
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
