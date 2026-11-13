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


# 保存协作组的持久化关系和事件记录；实际会话仍由调试任务管理。
class DebugCoordinationStore:
    """Persistent logical partition membership without changing TaskRecord."""

    # 数据库既可独立持久化，也可使用内存实例；所有连接访问受服务锁协调。
    # 存储只负责持久化协调关系和执行证据。
    # 会话是否仍在运行要去实时服务查询，不能从成员行反推。
    def __init__(self, database_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._closed = False
        # 未给路径时用独立内存库；给定文件路径时才创建父目录并跨进程保留历史。
        if database_path is None or str(database_path) == ":memory:":
            connection_path: str | Path = ":memory:"
        else:
            connection_path = Path(database_path)
            connection_path.parent.mkdir(parents=True, exist_ok=True)
        # 单连接可供不同线程使用；对连接的访问由该存储实例的锁串行化。
        # 文件库保留组、规则和事件，内存库只在连接存活时保留记录。
        self._connection = sqlite3.connect(connection_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            # 模式初始化在连接上下文中提交。
            # 下列索引分别服务于按项目查询、成员路由以及事件去重。
            with self._connection:
                # 组表仅保存稳定身份与关闭时间，ACTIVE 和 DEGRADED 不写入此表。
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
                # 分区保留历史关联与移除时间，关闭组时不删除记录。
                # 成员关联采用显式状态和时间戳，便于组关闭或成员移除后继续展示历史。
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
                # 项目索引服务于按项目及创建时间列组，不影响组 ID 全局唯一性。
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_debug_coordination_groups_project
                    ON debug_coordination_groups(project_id, created_at)
                    """
                )
                # 成员按组读取时以加入时间排序，便于命令与历史响应有稳定显示次序。
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_debug_coordination_members_group
                    ON debug_coordination_members(group_id, joined_at)
                    """
                )
                # 只对仍关联的会话建立全局唯一约束。
                # 历史分区可保留同一会话 ID，而不会阻止后来建立的新关联。
                self._connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_debug_coordination_active_session
                    ON debug_coordination_members(session_task_id)
                    WHERE associated = 1
                    """
                )
                # 规则关联的是分区而非直接绑定进程；退出后的分区仍能给历史提供身份。
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
                # 只对未删除规则约束组合唯一性。
                # 保留旧规则身份可供历史事件引用，并允许用户重新创建同样的规则。
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
                # 事件保存来源和目标的分区及会话身份，历史查询不依赖当前现场。
                # 来源事件与规则的组合唯一约束负责阻止重复下发。
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
                # 事件按组及源端发生时刻列出，目标暂停确认有延迟也不改变其源端顺序。
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_debug_coordination_events_group
                    ON debug_coordination_events(group_id, occurred_at DESC)
                    """
                )
        # 表结构初始化失败时关闭连接，不保留半初始化实例。
        # 建表或建索引失败时关闭尚未交付的连接，不留下半初始化的存储实例。
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    @staticmethod
    # 可空时间用 SQL NULL 表示，不能把未关闭时间编码成普通字符串。
    def _datetime(value: datetime | None) -> str | None:
        # 可空时间仍使用 SQL NULL；组未关闭和成员未移除都不以空字符串表示。
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        # 反序列化保留存储中的时区信息，调用方无需根据宿主本地时钟猜测。
        return datetime.fromisoformat(value) if value is not None else None

    @classmethod
    # 行转换只重建持久组数据，不在存储锁内请求实时会话状态。
    def _group_from_row(cls, row: sqlite3.Row) -> DebugCoordinationGroup:
        # 组的持久对象不含活动人数或实时状态，这两项由服务层查询会话后计算。
        return DebugCoordinationGroup(
            id=UUID(row["id"]),
            project_id=UUID(row["project_id"]),
            name=row["name"],
            created_at=cls._parse_datetime(row["created_at"]),
            closed_at=cls._parse_datetime(row["closed_at"]),
        )

    @classmethod
    # 分区 ID、所属组和会话任务各有独立字段，避免移除时误用进程任务编号。
    def _member_from_row(cls, row: sqlite3.Row) -> DebugLogicalPartition:
        # 分区的关联标记与移除时间从存储恢复，不根据对应任务当前状态重新计算。
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
    # 规则恢复时保留原动作与删除时刻；是否属于活动规则由查询条件控制。
    def _rule_from_row(cls, row: sqlite3.Row) -> DebugCoordinationRule:
        # 规则记录保留软删除时间，便于已有事件继续关联其历史规则。
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
    # 历史事件从记录的身份和标记恢复，即使组关闭或源端退出仍可追溯。
    def _event_from_row(cls, row: sqlite3.Row) -> DebugCoordinationEventRecord:
        # 重建事件时同时保存来源与目标任务身份；两者无需再从活跃会话索引解析。
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

    # 关闭后不自动重连，以免继续使用已经失效的服务实例。
    def _ensure_open(self) -> None:
        # 连接关闭后不自动打开新连接；重新组装服务时应使用新存储实例。
        if self._closed:
            raise RuntimeError("debug coordination store is closed")

    # 组及成员在同一事务创建，成员约束失败不会留下空组。
    def create(
        self,
        group: DebugCoordinationGroup,
        members: list[DebugLogicalPartition],
    ) -> DebugCoordinationGroup:
        # 组记录和初始成员在同一事务中登记。
        # 会话关联冲突导致事务失败时，不留缺少成员的新组。
        with self._lock:
            self._ensure_open()
            # 组与成员在同一事务写入，成员唯一性校验失败时不会留下空组。
            try:
                with self._connection:
                    # 先登记组，再批量登记成员；事务未提交前不返回组记录。
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
                    # 批量绑定保留请求映射，数据库唯一约束兜住其他请求同时关联同一会话的竞态。
                    # 批量写入分区；即使前几个成员写入成功，也须等整个事务提交才可对外可见。
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
            # 数据库约束承担并发提交下的唯一性，冲突转换为业务错误。
            # 活动关联冲突通过事务回滚，调用者不会拿到仅创建了部分成员的组。
            # 约束错误在写入边界统一转成业务错误，原 SQLite 异常仍可用于诊断。
            except sqlite3.IntegrityError as exc:
                raise AppError(
                    "one or more debug sessions already belong to an active coordination group"
                ) from exc
        return group

    # 按全局组 ID 查行，缺失返回资源不存在；已关闭组仍是有效历史对象。
    def require(self, group_id: UUID) -> DebugCoordinationGroup:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM debug_coordination_groups WHERE id = ?",
                (str(group_id),),
            ).fetchone()
        # 无记录时保留明确的缺失语义，不能把未知组与没有成员的已知组混淆。
        if row is None:
            raise NotFoundError(f"debug coordination group not found: {group_id}")
        return self._group_from_row(row)

    # 项目过滤可缺省，结果仍从存储快照生成。
    # 项目参数省略时列出全库历史；两种模式都按创建时间倒序提供。
    def list(self, project_id: UUID | None = None) -> list[DebugCoordinationGroup]:
        with self._lock:
            self._ensure_open()
            # 不带项目筛选时返回全部持久组，包括已经关闭但仍可查询历史的组。
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
        # 事务锁外将独立行转成领域对象，调用者不会拿到仍在遍历的数据库游标。
        return [self._group_from_row(row) for row in rows]

    # 调用者可选择当前成员或含已解除关联的历史视图。
    # 默认读取历史成员；仅命令广播等需要当前参与者的路径才排除已移除成员。
    def list_members(
        self,
        group_id: UUID,
        *,
        include_removed: bool = True,
    ) -> list[DebugLogicalPartition]:
        # 先检查组存在，使空成员列表与未知组具有不同语义。
        # 先确认组存在，避免把不存在的组误报成合法的空成员列表。
        self.require(group_id)
        with self._lock:
            self._ensure_open()
            # 只在当前组内查询关联行；过滤条件由明确的 include_removed 布尔值决定。
            query = """
                SELECT * FROM debug_coordination_members
                WHERE group_id = ?
            """
            # 广播只需要仍关联成员；展示历史时可保留已经移除的记录。
            if not include_removed:
                query += " AND associated = 1"
            query += " ORDER BY joined_at, partition_id"
            rows = self._connection.execute(query, (str(group_id),)).fetchall()
        return [self._member_from_row(row) for row in rows]

    # 按组和分区一起查找，禁止借其他组的分区 ID 操作当前组。
    # 分区查询同时绑定组和分区 ID，防止把其他组的分区用作当前规则。
    def require_member(
        self,
        group_id: UUID,
        partition_id: UUID,
    ) -> DebugLogicalPartition:
        # 允许返回已移除分区，以便事件处理区分成员失效和身份根本不存在。
        # 同时检查组和分区，防止用另一个组的分区 ID 读取不相关成员。
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
        # 查不到分区时不会退回用任务 ID 查找；两种身份用途不同。
        if row is None:
            raise NotFoundError(f"logical partition not found: {partition_id}")
        return self._member_from_row(row)

    # 规则由上层校验会话及断点；存储继续约束规则关系和重复绑定。
    # 调用方完成状态与断点检查后，存储只提交规则定义及初始未删除状态。
    def create_rule(self, rule: DebugCoordinationRule) -> DebugCoordinationRule:
        with self._lock:
            self._ensure_open()
            # 规则写入只负责持久化约束，断点编号是否来自当前会话由调用方检查。
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
            # 活动规则唯一性按组、来源分区、断点、目标和动作组合处理，不以规则名称去重。
            # 同一活动规则不能重复登记；先前已删除的定义仍可用于历史记录。
            except sqlite3.IntegrityError as exc:
                raise AppError("an equivalent active coordination rule already exists") from exc
        return rule

    # 规则列表只返回尚可匹配的定义，删除的规则仍供事件历史引用。
    def list_rules(self, group_id: UUID) -> list[DebugCoordinationRule]:
        # 列表只给当前未删除规则，已有事件读取则使用持久化的历史关联。
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

    # 删除规则使用时间戳，既停止新事件匹配也保留历史关联。
    # 规则采用软删除，保留被既有事件引用的记录。
    # 更新限定在所属组和未删除行，找不到有效规则时显式报错。
    def delete_rule(self, group_id: UUID, rule_id: UUID) -> None:
        # 先确认请求的组存在，再按组和规则一起更新；不存在与已删除都不会改动其他行。
        self.require(group_id)
        # 删除使用单一时间戳更新记录，而非删除可能被事件引用的数据行。
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
        # 重复删除或者组内不存在该规则时命中行数为零，按缺失处理。
        if cursor.rowcount != 1:
            raise NotFoundError(f"active coordination rule not found: {rule_id}")

    # 按源会话与断点定位当前规则，排除已关闭组和已解除的来源。
    # 按来源会话与断点号查规则，并筛掉已关闭组或已解除关联的来源。
    # 查询后再解析分区记录；得到的绑定不是现场持续有效的保证。
    def matching_rules(
        self,
        session_task_id: UUID,
        breakpoint_number: str,
    ) -> list[DebugCoordinationRuleBinding]:
        # 规则匹配以来源任务和该会话断点号为入口。
        # 目标现场不属于此查询，执行器取得组锁后读取。
        with self._lock:
            self._ensure_open()
            # 查询仅匹配活动来源、规则和组。
            # 目标分区是否仍可执行还需要服务在组锁内检查。
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
        # 查出规则后补充双方成员快照；执行前上层仍须重新核对状态。
        # 先完成数据库查询，再为每条规则构造两个分区对象；不在查询事务中调用调试器。
        # 返回结构包含当时的规则和两端分区快照，执行前仍要读取当前关联。
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

    # 先插入来源事件与规则的处理记录，用唯一性约束作为去重闸门。
    # 用插入和数据库唯一约束争取处理权，不先查后写。
    # 这里只登记事件，现场可否中断由服务层在后续阶段判断。
    def claim_event(self, event: DebugCoordinationEventRecord) -> bool:
        # 事务成功插入 PROCESSING 记录后服务才会发命令；存储不直接持有调试器。
        # 唯一约束在写入时判定认领，不能先在内存检查再无条件插入。
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
            # 未能取得处理权的回调不重复发送协调命令。
            # 完整性冲突在认领阶段视为没有取得处理权。
            # 重复源事件与规则的组合不会启动第二次目标动作。
            # 约束拒绝代表本次未取得执行资格。
            # 调用方不得在此情况下尝试向调试器下发。
            except sqlite3.IntegrityError:
                return False
        return True

    # 只更新处理结果字段，来源事件、规则和成员身份保持不变。
    # 更新认领时已创建的事件，不追加第二条结果。
    # 保留原始发生时间与身份，并附最终处理时间、结果和证据。
    def finalize_event(self, event: DebugCoordinationEventRecord) -> None:
        with self._lock, self._connection:
            self._ensure_open()
            # 完成阶段仅更新可变处理字段，原始事件身份、参与者及命中时间保持不变。
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
        # 找不到先前领取的记录属于异常，不能悄悄丢弃执行结果。
        # 完成回写必须对应已有认领记录；找不到时报告存储异常，不尝试补插结果。
        # 终态写回必须命中已认领记录；缺失时抛出错误而不重新插入。
        if cursor.rowcount != 1:
            raise NotFoundError(f"coordination event not found: {event.id}")

    # 读取持久化事件用于展示命中、发送和确认经过，不会重放动作。
    # 事件历史按发生时间倒序排列，时间相同再用 ID 定序。
    # 处理较慢的事件不会因此被当成最新发生的事件。
    def list_events(
        self,
        group_id: UUID,
        *,
        limit: int = 100,
    ) -> list[DebugCoordinationEventRecord]:
        # 组不存在与没有事件是不同的结果，先校验组再取窗口。
        # 事件查询可用于关闭组的历史页面，不依赖任何 GDB 进程仍在线。
        self.require(group_id)
        with self._lock:
            self._ensure_open()
            # 按事件发生时间从新到旧分页式截取，处理完成时间不会改变列表顺序。
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

    # 解除成员只更新关联状态，不触碰实际 GDB 会话。
    # 解除关联不删除分区行，也不管理对应的 GDB 进程。
    # 只更新仍处于关联态的成员，重复移除会得到找不到活动成员。
    def remove_member(self, group_id: UUID, partition_id: UUID) -> None:
        group = self.require(group_id)
        # 已关闭组不接受成员移除；该成员应已经由关闭组的事务解除关联。
        # 关闭后不允许单独移除成员，组级关闭已经撤销活动关联。
        if group.closed_at is not None:
            raise AppError("debug coordination group is closed")
        removed_at = utc_now()
        # 仅修改指定组内仍关联的分区并记录时间，历史规则无需搬迁到其他成员。
        with self._lock, self._connection:
            self._ensure_open()
            # 仅修改仍关联的分区；不会因重复操作覆盖第一次移除时间。
            cursor = self._connection.execute(
                """
                UPDATE debug_coordination_members
                SET associated = 0, removed_at = ?
                WHERE group_id = ? AND partition_id = ? AND associated = 1
                """,
                (self._datetime(removed_at), str(group_id), str(partition_id)),
            )
        # 不存在或已解除的分区无法再次迁移，交付明确的未找到错误。
        if cursor.rowcount != 1:
            raise NotFoundError(f"active logical partition not found: {partition_id}")

    # 组关闭时间和成员解除在同一事务完成；重复关闭保留原时间。
    # 关闭关联可以重复请求，原组记录保留给历史查询。
    def close_group(self, group_id: UUID) -> DebugCoordinationGroup:
        self.require(group_id)
        closed_at = utc_now()
        # 同一事务关闭组并释放仍关联的成员，避免留下占用会话的已关闭组。
        # 保留首次关闭时间和历史移除时间，重复关闭不会改写旧时间。
        with self._lock, self._connection:
            self._ensure_open()
            # 第一次关闭时间保留，重复关闭不会将历史起点移到后续请求时间。
            self._connection.execute(
                """
                UPDATE debug_coordination_groups
                SET closed_at = COALESCE(closed_at, ?)
                WHERE id = ?
                """,
                (self._datetime(closed_at), str(group_id)),
            )
            # 组关闭和活动成员解除关联同一事务提交，外部不能看到只完成了一半的关闭。
            self._connection.execute(
                """
                UPDATE debug_coordination_members
                SET associated = 0, removed_at = COALESCE(removed_at, ?)
                WHERE group_id = ? AND associated = 1
                """,
                (self._datetime(closed_at), str(group_id)),
            )
        return self.require(group_id)

    # 关闭仅回收数据库连接，调试进程由平台会话单独收尾。
    # 关闭与读写使用同一把锁，避免另一个线程使用已关闭的连接。
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # 连接释放在存储锁内执行，避免同一连接同时被读取和关闭。
            self._connection.close()
            self._closed = True
