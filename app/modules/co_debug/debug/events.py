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


@dataclass(slots=True, frozen=True)
class DebugSessionEvent:
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


class DebugSessionEventBus:
    """Thread-safe, non-blocking bridge from managed task loops to the API loop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[UUID, _Subscription] = {}

    def subscribe(
        self,
        loop: asyncio.AbstractEventLoop,
        callback: DebugSessionEventCallback,
    ) -> UUID:
        subscription_id = uuid4()
        with self._lock:
            self._subscriptions[subscription_id] = _Subscription(loop, callback)
        return subscription_id

    def unsubscribe(self, subscription_id: UUID) -> None:
        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    def publish(self, event: DebugSessionEvent) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions.items())

        stale: list[UUID] = []
        for subscription_id, subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(
                    self._deliver,
                    subscription_id,
                    subscription.callback,
                    event,
                )
            except RuntimeError:
                stale.append(subscription_id)

        if stale:
            with self._lock:
                for subscription_id in stale:
                    self._subscriptions.pop(subscription_id, None)

    def _deliver(
        self,
        subscription_id: UUID,
        callback: DebugSessionEventCallback,
        event: DebugSessionEvent,
    ) -> None:
        with self._lock:
            if subscription_id not in self._subscriptions:
                return
        try:
            callback(event)
        except Exception:
            logger.exception("debug session event subscriber failed")
