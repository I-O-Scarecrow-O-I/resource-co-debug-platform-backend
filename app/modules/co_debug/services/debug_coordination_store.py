from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.core.errors import AppError, NotFoundError
from app.core.time import utc_now
from app.modules.co_debug.coordination.models import (
    DebugCoordinationEventOutcome,
    DebugCoordinationEventRecord,
    DebugCoordinationGroup,
    DebugCoordinationRule,
    DebugCoordinationRuleAction,
    DebugCoordinationRuleBinding,
    DebugLogicalPartition,
)


class DebugCoordinationStore:
    """Persistent logical partition membership without changing TaskRecord."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._closed = False
        if database_path is None or str(database_path) == ":memory:":
            connection_path: str | Path = ":memory:"
        else:
            connection_path = Path(database_path)
            connection_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(connection_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            with self._connection:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS debug_coordination_groups (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        closed_at TEXT
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS debug_coordination_members (
                        partition_id TEXT PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        partition_name TEXT NOT NULL,
                        session_task_id TEXT NOT NULL,
                        joined_at TEXT NOT NULL,
                        associated INTEGER NOT NULL DEFAULT 1
                            CHECK (associated IN (0, 1)),
                        removed_at TEXT,
                        FOREIGN KEY (group_id)
                            REFERENCES debug_coordination_groups(id)
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_debug_coordination_groups_project
                    ON debug_coordination_groups(project_id, created_at)
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_debug_coordination_members_group
                    ON debug_coordination_members(group_id, joined_at)
                    """
                )
                self._connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_debug_coordination_active_session
                    ON debug_coordination_members(session_task_id)
                    WHERE associated = 1
                    """
                )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS debug_coordination_rules (
                        id TEXT PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        source_partition_id TEXT NOT NULL,
                        source_breakpoint_number TEXT NOT NULL,
                        target_partition_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        deleted_at TEXT,
                        FOREIGN KEY (group_id)
                            REFERENCES debug_coordination_groups(id)
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_debug_coordination_active_rule
                    ON debug_coordination_rules(
                        group_id,
                        source_partition_id,
                        source_breakpoint_number,
                        target_partition_id,
                        action
                    )
                    WHERE deleted_at IS NULL
                    """
                )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS debug_coordination_events (
                        id TEXT PRIMARY KEY,
                        source_event_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        rule_id TEXT NOT NULL,
                        source_partition_id TEXT NOT NULL,
                        source_session_task_id TEXT NOT NULL,
                        target_partition_id TEXT NOT NULL,
                        target_session_task_id TEXT NOT NULL,
                        breakpoint_number TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        processed_at TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        command_sent INTEGER NOT NULL
                            CHECK (command_sent IN (0, 1)),
                        target_paused INTEGER NOT NULL
                            CHECK (target_paused IN (0, 1)),
                        message TEXT NOT NULL,
                        UNIQUE (source_event_id, rule_id),
                        FOREIGN KEY (group_id)
                            REFERENCES debug_coordination_groups(id),
                        FOREIGN KEY (rule_id)
                            REFERENCES debug_coordination_rules(id)
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_debug_coordination_events_group
                    ON debug_coordination_events(group_id, occurred_at DESC)
                    """
                )
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    @staticmethod
    def _datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None

    @classmethod
    def _group_from_row(cls, row: sqlite3.Row) -> DebugCoordinationGroup:
        return DebugCoordinationGroup(
            id=UUID(row["id"]),
            project_id=UUID(row["project_id"]),
            name=row["name"],
            created_at=cls._parse_datetime(row["created_at"]),
            closed_at=cls._parse_datetime(row["closed_at"]),
        )

    @classmethod
    def _member_from_row(cls, row: sqlite3.Row) -> DebugLogicalPartition:
        return DebugLogicalPartition(
            id=UUID(row["partition_id"]),
            group_id=UUID(row["group_id"]),
            name=row["partition_name"],
            session_task_id=UUID(row["session_task_id"]),
            joined_at=cls._parse_datetime(row["joined_at"]),
            associated=bool(row["associated"]),
            removed_at=cls._parse_datetime(row["removed_at"]),
        )

    @classmethod
    def _rule_from_row(cls, row: sqlite3.Row) -> DebugCoordinationRule:
        return DebugCoordinationRule(
            id=UUID(row["id"]),
            group_id=UUID(row["group_id"]),
            source_partition_id=UUID(row["source_partition_id"]),
            source_breakpoint_number=row["source_breakpoint_number"],
            target_partition_id=UUID(row["target_partition_id"]),
            action=DebugCoordinationRuleAction(row["action"]),
            created_at=cls._parse_datetime(row["created_at"]),
            deleted_at=cls._parse_datetime(row["deleted_at"]),
        )

    @classmethod
    def _event_from_row(cls, row: sqlite3.Row) -> DebugCoordinationEventRecord:
        return DebugCoordinationEventRecord(
            id=UUID(row["id"]),
            source_event_id=UUID(row["source_event_id"]),
            group_id=UUID(row["group_id"]),
            rule_id=UUID(row["rule_id"]),
            source_partition_id=UUID(row["source_partition_id"]),
            source_session_task_id=UUID(row["source_session_task_id"]),
            target_partition_id=UUID(row["target_partition_id"]),
            target_session_task_id=UUID(row["target_session_task_id"]),
            breakpoint_number=row["breakpoint_number"],
            occurred_at=cls._parse_datetime(row["occurred_at"]),
            processed_at=cls._parse_datetime(row["processed_at"]),
            outcome=DebugCoordinationEventOutcome(row["outcome"]),
            command_sent=bool(row["command_sent"]),
            target_paused=bool(row["target_paused"]),
            message=row["message"],
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("debug coordination store is closed")

    def create(
        self,
        group: DebugCoordinationGroup,
        members: list[DebugLogicalPartition],
    ) -> DebugCoordinationGroup:
        with self._lock:
            self._ensure_open()
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO debug_coordination_groups (
                            id, project_id, name, created_at, closed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(group.id),
                            str(group.project_id),
                            group.name,
                            self._datetime(group.created_at),
                            self._datetime(group.closed_at),
                        ),
                    )
                    self._connection.executemany(
                        """
                        INSERT INTO debug_coordination_members (
                            partition_id, group_id, partition_name,
                            session_task_id, joined_at, associated, removed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                str(member.id),
                                str(member.group_id),
                                member.name,
                                str(member.session_task_id),
                                self._datetime(member.joined_at),
                                int(member.associated),
                                self._datetime(member.removed_at),
                            )
                            for member in members
                        ],
                    )
            except sqlite3.IntegrityError as exc:
                raise AppError(
                    "one or more debug sessions already belong to an active coordination group"
                ) from exc
        return group

    def require(self, group_id: UUID) -> DebugCoordinationGroup:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM debug_coordination_groups WHERE id = ?",
                (str(group_id),),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"debug coordination group not found: {group_id}")
        return self._group_from_row(row)

    def list(self, project_id: UUID | None = None) -> list[DebugCoordinationGroup]:
        with self._lock:
            self._ensure_open()
            if project_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM debug_coordination_groups ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM debug_coordination_groups
                    WHERE project_id = ?
                    ORDER BY created_at DESC
                    """,
                    (str(project_id),),
                ).fetchall()
        return [self._group_from_row(row) for row in rows]

    def list_members(
        self,
        group_id: UUID,
        *,
        include_removed: bool = True,
    ) -> list[DebugLogicalPartition]:
        self.require(group_id)
        with self._lock:
            self._ensure_open()
            query = """
                SELECT * FROM debug_coordination_members
                WHERE group_id = ?
            """
            if not include_removed:
                query += " AND associated = 1"
            query += " ORDER BY joined_at, partition_id"
            rows = self._connection.execute(query, (str(group_id),)).fetchall()
        return [self._member_from_row(row) for row in rows]

    def require_member(
        self,
        group_id: UUID,
        partition_id: UUID,
    ) -> DebugLogicalPartition:
        self.require(group_id)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT * FROM debug_coordination_members
                WHERE group_id = ? AND partition_id = ?
                """,
                (str(group_id), str(partition_id)),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"logical partition not found: {partition_id}")
        return self._member_from_row(row)

    def create_rule(self, rule: DebugCoordinationRule) -> DebugCoordinationRule:
        with self._lock:
            self._ensure_open()
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO debug_coordination_rules (
                            id, group_id, source_partition_id,
                            source_breakpoint_number, target_partition_id,
                            action, created_at, deleted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(rule.id),
                            str(rule.group_id),
                            str(rule.source_partition_id),
                            rule.source_breakpoint_number,
                            str(rule.target_partition_id),
                            rule.action.value,
                            self._datetime(rule.created_at),
                            self._datetime(rule.deleted_at),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise AppError("an equivalent active coordination rule already exists") from exc
        return rule

    def list_rules(self, group_id: UUID) -> list[DebugCoordinationRule]:
        self.require(group_id)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM debug_coordination_rules
                WHERE group_id = ? AND deleted_at IS NULL
                ORDER BY created_at, id
                """,
                (str(group_id),),
            ).fetchall()
        return [self._rule_from_row(row) for row in rows]

    def delete_rule(self, group_id: UUID, rule_id: UUID) -> None:
        self.require(group_id)
        deleted_at = utc_now()
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE debug_coordination_rules
                SET deleted_at = ?
                WHERE group_id = ? AND id = ? AND deleted_at IS NULL
                """,
                (self._datetime(deleted_at), str(group_id), str(rule_id)),
            )
        if cursor.rowcount != 1:
            raise NotFoundError(f"active coordination rule not found: {rule_id}")

    def matching_rules(
        self,
        session_task_id: UUID,
        breakpoint_number: str,
    ) -> list[DebugCoordinationRuleBinding]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT rule.*
                FROM debug_coordination_rules AS rule
                JOIN debug_coordination_groups AS group_record
                    ON group_record.id = rule.group_id
                JOIN debug_coordination_members AS source
                    ON source.partition_id = rule.source_partition_id
                    AND source.group_id = rule.group_id
                WHERE source.session_task_id = ?
                    AND source.associated = 1
                    AND rule.source_breakpoint_number = ?
                    AND rule.deleted_at IS NULL
                    AND group_record.closed_at IS NULL
                ORDER BY rule.created_at, rule.id
                """,
                (str(session_task_id), breakpoint_number),
            ).fetchall()
        bindings: list[DebugCoordinationRuleBinding] = []
        for row in rows:
            rule = self._rule_from_row(row)
            bindings.append(
                DebugCoordinationRuleBinding(
                    rule=rule,
                    source=self.require_member(rule.group_id, rule.source_partition_id),
                    target=self.require_member(rule.group_id, rule.target_partition_id),
                )
            )
        return bindings

    def claim_event(self, event: DebugCoordinationEventRecord) -> bool:
        with self._lock:
            self._ensure_open()
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO debug_coordination_events (
                            id, source_event_id, group_id, rule_id,
                            source_partition_id, source_session_task_id,
                            target_partition_id, target_session_task_id,
                            breakpoint_number, occurred_at, processed_at,
                            outcome, command_sent, target_paused, message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.id),
                            str(event.source_event_id),
                            str(event.group_id),
                            str(event.rule_id),
                            str(event.source_partition_id),
                            str(event.source_session_task_id),
                            str(event.target_partition_id),
                            str(event.target_session_task_id),
                            event.breakpoint_number,
                            self._datetime(event.occurred_at),
                            self._datetime(event.processed_at),
                            event.outcome.value,
                            int(event.command_sent),
                            int(event.target_paused),
                            event.message,
                        ),
                    )
            except sqlite3.IntegrityError:
                return False
        return True

    def finalize_event(self, event: DebugCoordinationEventRecord) -> None:
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE debug_coordination_events
                SET processed_at = ?, outcome = ?, command_sent = ?,
                    target_paused = ?, message = ?
                WHERE id = ?
                """,
                (
                    self._datetime(event.processed_at),
                    event.outcome.value,
                    int(event.command_sent),
                    int(event.target_paused),
                    event.message,
                    str(event.id),
                ),
            )
        if cursor.rowcount != 1:
            raise NotFoundError(f"coordination event not found: {event.id}")

    def list_events(
        self,
        group_id: UUID,
        *,
        limit: int = 100,
    ) -> list[DebugCoordinationEventRecord]:
        self.require(group_id)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM debug_coordination_events
                WHERE group_id = ?
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
                (str(group_id), limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def remove_member(self, group_id: UUID, partition_id: UUID) -> None:
        group = self.require(group_id)
        if group.closed_at is not None:
            raise AppError("debug coordination group is closed")
        removed_at = utc_now()
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                """
                UPDATE debug_coordination_members
                SET associated = 0, removed_at = ?
                WHERE group_id = ? AND partition_id = ? AND associated = 1
                """,
                (self._datetime(removed_at), str(group_id), str(partition_id)),
            )
        if cursor.rowcount != 1:
            raise NotFoundError(f"active logical partition not found: {partition_id}")

    def close_group(self, group_id: UUID) -> DebugCoordinationGroup:
        self.require(group_id)
        closed_at = utc_now()
        with self._lock, self._connection:
            self._ensure_open()
            self._connection.execute(
                """
                UPDATE debug_coordination_groups
                SET closed_at = COALESCE(closed_at, ?)
                WHERE id = ?
                """,
                (self._datetime(closed_at), str(group_id)),
            )
            self._connection.execute(
                """
                UPDATE debug_coordination_members
                SET associated = 0, removed_at = COALESCE(removed_at, ?)
                WHERE group_id = ? AND associated = 1
                """,
                (self._datetime(closed_at), str(group_id)),
            )
        return self.require(group_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
