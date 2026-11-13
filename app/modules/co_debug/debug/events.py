from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class DebugSessionEventKind(StrEnum):
    BREAKPOINT_HIT = "breakpoint_hit"


# 事件身份用于协调去重；源任务和断点编号用于规则匹配。
@dataclass(slots=True, frozen=True)
class DebugSessionEvent:
    # 事件 ID 用于识别一次实际投递；会话任务 ID 与断点号共同定位触发来源。
    event_id: UUID
    session_task_id: UUID
    kind: DebugSessionEventKind
    occurred_at: datetime
    breakpoint_number: str


class DebugSessionEventPublisher(Protocol):
    def publish(self, event: DebugSessionEvent) -> None: ...


DebugSessionEventCallback = Callable[[DebugSessionEvent], None]


@dataclass(slots=True, frozen=True)
class _Subscription:
    loop: asyncio.AbstractEventLoop
    callback: DebugSessionEventCallback


# 后台调试循环投递事件，协调订阅者在各自指定的循环上运行。
class DebugSessionEventBus:
    """Thread-safe, non-blocking bridge from managed task loops to the API loop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[UUID, _Subscription] = {}

    # 每次订阅有独立标识，替换循环时可以精确撤销旧回调。
    def subscribe(
        self,
        loop: asyncio.AbstractEventLoop,
        callback: DebugSessionEventCallback,
    ) -> UUID:
        # 订阅保存回调所属循环，发布线程不能直接调用该回调。
        subscription_id = uuid4()
        with self._lock:
            self._subscriptions[subscription_id] = _Subscription(loop, callback)
        return subscription_id

    def unsubscribe(self, subscription_id: UUID) -> None:
        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    # 持锁仅复制订阅快照，不在发布线程执行订阅者逻辑。
    def publish(self, event: DebugSessionEvent) -> None:
        # 先在锁内复制订阅列表，再跨线程投递；不持锁等待其他循环。
        with self._lock:
            subscriptions = list(self._subscriptions.items())

        stale: list[UUID] = []
        # 线程安全调度将事件交给订阅循环；已退出的循环随后移除。
        for subscription_id, subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(
                    self._deliver,
                    subscription_id,
                    subscription.callback,
                    event,
                )
            # 已关闭循环可能拒绝新回调，记录其订阅并在本轮结束后移除。
            except RuntimeError:
                stale.append(subscription_id)

        if stale:
            with self._lock:
                for subscription_id in stale:
                    self._subscriptions.pop(subscription_id, None)

    # 投递开始前重新检查订阅仍有效，避免已取消的旧回调处理排队事件。
    def _deliver(
        self,
        subscription_id: UUID,
        callback: DebugSessionEventCallback,
        event: DebugSessionEvent,
    ) -> None:
        # 已投递的回调执行前再检查订阅，以便退订能拦截尚未开始的事件。
        with self._lock:
            if subscription_id not in self._subscriptions:
                return
        # 隔离订阅者异常，不能让协调规则故障中止 GDB 输出读取。
        # 订阅者异常只进入日志，不向发布方传播或中断其他订阅的交付。
        try:
            callback(event)
        except Exception:
            logger.exception("debug session event subscriber failed")
