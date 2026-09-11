import asyncio
import threading
from uuid import uuid4

import pytest

from app.modules.co_debug.debug.broker import (
    DebugCommandBroker,
    DebugCommandBrokerClosed,
    DebugCommandKind,
    DebugCommandTimeout,
)
from app.modules.co_debug.debug.manager import (
    DebugSessionAlreadyExists,
    DebugSessionManager,
    DebugSessionNotFound,
    GdbMiCommandDispatcher,
)
from app.modules.co_debug.debug.models import (
    DebugBreakpoint,
    DebugSessionState,
    DebugSessionStateModel,
)


class FakeSession:
    def __init__(
        self,
        name: str = "session",
    ) -> None:
        self.name = name

        self.state = (
            DebugSessionStateModel(
                state=(
                    DebugSessionState.READY
                )
            )
        )

        self.console_output = ()
        self.target_output = ()
        self.log_output = ()
        self.stderr_output = ()

        self.calls: list[
            tuple
        ] = []

        self.closed = False

    async def set_arguments(
        self,
        arguments,
    ):
        self.calls.append(
            (
                "set_arguments",
                list(arguments),
            )
        )

    async def insert_breakpoint(
        self,
        location,
        temporary=False,
        disabled=False,
        condition=None,
    ):
        self.calls.append(
            (
                "insert_breakpoint",
                location,
                temporary,
                disabled,
                condition,
            )
        )

        breakpoint = (
            DebugBreakpoint(
                number="1",
                location=location,
                enabled=(
                    not disabled
                ),
                file="main.c",
                fullname="/tmp/main.c",
                line=7,
                function="main",
            )
        )

        self.state.breakpoints[
            "1"
        ] = breakpoint

        return breakpoint

    async def delete_breakpoint(
        self,
        number,
    ):
        self.calls.append(
            (
                "delete_breakpoint",
                number,
            )
        )

        self.state.breakpoints.pop(
            number,
            None,
        )

    async def enable_breakpoint(
        self,
        number,
    ):
        self.calls.append(
            (
                "enable_breakpoint",
                number,
            )
        )

    async def disable_breakpoint(
        self,
        number,
    ):
        self.calls.append(
            (
                "disable_breakpoint",
                number,
            )
        )

    async def run(
        self,
    ):
        self.calls.append(
            ("run",)
        )

        self.state.state = (
            DebugSessionState.RUNNING
        )

    async def continue_execution(
        self,
    ):
        self.calls.append(
            ("continue",)
        )

        self.state.state = (
            DebugSessionState.RUNNING
        )

    async def next(
        self,
    ):
        self.calls.append(
            ("next",)
        )

    async def step(
        self,
    ):
        self.calls.append(
            ("step",)
        )

    async def interrupt(
        self,
    ):
        self.calls.append(
            ("interrupt",)
        )

    async def evaluate_expression(
        self,
        expression,
    ):
        self.calls.append(
            (
                "evaluate",
                expression,
            )
        )

        return "42"

    async def list_stack_frames(
        self,
    ):
        self.calls.append(
            ("stack_frames",)
        )

        return [
            {
                "frame": {
                    "level": "0",
                    "func": "main",
                }
            }
        ]

    async def wait_for_stop(
        self,
        timeout_seconds=None,
    ):
        self.calls.append(
            (
                "wait_for_stop",
                timeout_seconds,
            )
        )

        self.state.state = (
            DebugSessionState.STOPPED
        )

        self.state.stop_reason = (
            "breakpoint-hit"
        )

        self.state.current_file = (
            "/tmp/main.c"
        )

        self.state.current_line = 7

        self.state.current_function = (
            "main"
        )

        return self.state.state

    async def close(
        self,
    ):
        self.calls.append(
            ("close",)
        )

        self.closed = True

        self.state.state = (
            DebugSessionState.EXITED
        )


@pytest.mark.asyncio
async def test_broker_round_trip():
    broker = (
        DebugCommandBroker()
    )

    async def worker():
        request = (
            await broker.receive()
        )

        assert (
            request.operation
            == DebugCommandKind.RUN
        )

        broker.resolve(
            request,
            {
                "state": "RUNNING",
            },
        )

    worker_task = (
        asyncio.create_task(
            worker()
        )
    )

    result = await broker.request(
        DebugCommandKind.RUN,
        timeout_seconds=1,
    )

    assert result == {
        "state": "RUNNING"
    }

    await worker_task

    broker.close()


@pytest.mark.asyncio
async def test_broker_propagates_error():
    broker = (
        DebugCommandBroker()
    )

    async def worker():
        request = (
            await broker.receive()
        )

        broker.reject(
            request,
            ValueError(
                "bad command"
            ),
        )

    task = asyncio.create_task(
        worker()
    )

    with pytest.raises(
        ValueError,
        match="bad command",
    ):
        await broker.request(
            DebugCommandKind.RUN,
            timeout_seconds=1,
        )

    await task

    broker.close()


@pytest.mark.asyncio
async def test_broker_timeout():
    broker = (
        DebugCommandBroker()
    )

    with pytest.raises(
        DebugCommandTimeout,
        match="run",
    ):
        await broker.request(
            DebugCommandKind.RUN,
            timeout_seconds=0.05,
        )

    broker.close()


@pytest.mark.asyncio
async def test_broker_close_fails_pending_request():
    broker = (
        DebugCommandBroker()
    )

    request_task = (
        asyncio.create_task(
            broker.request(
                DebugCommandKind.RUN,
            )
        )
    )

    await asyncio.sleep(
        0.01
    )

    broker.close()

    with pytest.raises(
        DebugCommandBrokerClosed
    ):
        await request_task


@pytest.mark.asyncio
async def test_broker_rejects_new_commands_after_close():
    broker = (
        DebugCommandBroker()
    )

    broker.close()

    with pytest.raises(
        DebugCommandBrokerClosed
    ):
        await broker.request(
            DebugCommandKind.RUN
        )


@pytest.mark.asyncio
async def test_dispatcher_executes_session_commands():
    broker = (
        DebugCommandBroker()
    )

    session = FakeSession()

    dispatcher = (
        GdbMiCommandDispatcher()
    )

    dispatcher_task = (
        asyncio.create_task(
            dispatcher.run(
                broker=broker,
                session=session,
            )
        )
    )

    breakpoint = (
        await broker.request(
            DebugCommandKind.INSERT_BREAKPOINT,
            location="main.c:7",
            temporary=True,
            disabled=False,
            condition="x > 0",
            timeout_seconds=1,
        )
    )

    assert breakpoint == {
        "number": "1",
        "location": "main.c:7",
        "enabled": True,
        "file": "main.c",
        "fullname": "/tmp/main.c",
        "line": 7,
        "function": "main",
    }

    run_state = (
        await broker.request(
            DebugCommandKind.RUN,
            timeout_seconds=1,
        )
    )

    assert (
        run_state["state"]
        == "RUNNING"
    )

    evaluated = (
        await broker.request(
            DebugCommandKind.EVALUATE,
            expression="x + 1",
            timeout_seconds=1,
        )
    )

    assert evaluated == {
        "value": "42"
    }

    frames = (
        await broker.request(
            DebugCommandKind.STACK_FRAMES,
            timeout_seconds=1,
        )
    )

    assert frames == {
        "frames": [
            {
                "frame": {
                    "level": "0",
                    "func": "main",
                }
            }
        ]
    }

    await broker.request(
        DebugCommandKind.CLOSE,
        timeout_seconds=1,
    )

    await dispatcher_task

    assert session.closed is True


@pytest.mark.asyncio
async def test_dispatcher_returns_state_snapshot():
    broker = (
        DebugCommandBroker()
    )

    session = FakeSession()

    dispatcher_task = (
        asyncio.create_task(
            GdbMiCommandDispatcher()
            .run(
                broker=broker,
                session=session,
            )
        )
    )

    state = await broker.request(
        DebugCommandKind.STATE,
        timeout_seconds=1,
    )

    assert state == {
        "state": "READY",
        "stop_reason": None,
        "current_file": None,
        "current_line": None,
        "current_function": None,
        "breakpoints": [],
        "console_output": [],
        "target_output": [],
        "log_output": [],
        "stderr_output": [],
    }

    await broker.request(
        DebugCommandKind.CLOSE,
        timeout_seconds=1,
    )

    await dispatcher_task


@pytest.mark.asyncio
async def test_manager_registers_independent_sessions():
    manager = (
        DebugSessionManager()
    )

    first_id = uuid4()
    second_id = uuid4()

    first = (
        DebugCommandBroker()
    )

    second = (
        DebugCommandBroker()
    )

    manager.register(
        first_id,
        first,
    )

    manager.register(
        second_id,
        second,
    )

    assert (
        manager.contains(
            first_id
        )
        is True
    )

    assert (
        manager.contains(
            second_id
        )
        is True
    )

    assert set(
        manager.list_session_ids()
    ) == {
        first_id,
        second_id,
    }

    manager.unregister(
        first_id
    )

    assert (
        manager.contains(
            first_id
        )
        is False
    )

    assert (
        manager.contains(
            second_id
        )
        is True
    )

    manager.unregister(
        second_id
    )


def test_manager_rejects_duplicate_session():
    manager = (
        DebugSessionManager()
    )

    task_id = uuid4()

    broker = (
        DebugCommandBroker()
    )

    manager.register(
        task_id,
        broker,
    )

    with pytest.raises(
        DebugSessionAlreadyExists
    ):
        manager.register(
            task_id,
            DebugCommandBroker(),
        )

    manager.unregister(
        task_id
    )


def test_manager_rejects_missing_session():
    manager = (
        DebugSessionManager()
    )

    with pytest.raises(
        DebugSessionNotFound
    ):
        manager.require(
            uuid4()
        )


@pytest.mark.asyncio
async def test_manager_controls_session():
    manager = (
        DebugSessionManager()
    )

    task_id = uuid4()

    broker = (
        DebugCommandBroker()
    )

    session = FakeSession()

    manager.register(
        task_id,
        broker,
    )

    dispatcher_task = (
        asyncio.create_task(
            GdbMiCommandDispatcher()
            .run(
                broker=broker,
                session=session,
            )
        )
    )

    breakpoint = (
        await manager
        .insert_breakpoint(
            task_id,
            "main.c:7",
        )
    )

    assert (
        breakpoint["number"]
        == "1"
    )

    state = await manager.run(
        task_id
    )

    assert (
        state["state"]
        == "RUNNING"
    )

    result = (
        await manager.evaluate(
            task_id,
            "x",
        )
    )

    assert result == {
        "value": "42"
    }

    stopped = (
        await manager
        .wait_for_stop(
            task_id,
            stop_timeout_seconds=2,
            request_timeout_seconds=3,
        )
    )

    assert (
        stopped["state"]
        == "STOPPED"
    )

    assert (
        stopped["stop_reason"]
        == "breakpoint-hit"
    )

    await manager.close_session(
        task_id
    )

    await dispatcher_task

    assert (
        manager.contains(
            task_id
        )
        is False
    )


@pytest.mark.asyncio
async def test_two_sessions_do_not_mix_commands():
    manager = (
        DebugSessionManager()
    )

    first_id = uuid4()
    second_id = uuid4()

    first_broker = (
        DebugCommandBroker()
    )

    second_broker = (
        DebugCommandBroker()
    )

    first_session = (
        FakeSession("first")
    )

    second_session = (
        FakeSession("second")
    )

    manager.register(
        first_id,
        first_broker,
    )

    manager.register(
        second_id,
        second_broker,
    )

    first_dispatcher = (
        asyncio.create_task(
            GdbMiCommandDispatcher()
            .run(
                broker=first_broker,
                session=first_session,
            )
        )
    )

    second_dispatcher = (
        asyncio.create_task(
            GdbMiCommandDispatcher()
            .run(
                broker=second_broker,
                session=second_session,
            )
        )
    )

    await asyncio.gather(
        manager.evaluate(
            first_id,
            "first_value",
        ),
        manager.evaluate(
            second_id,
            "second_value",
        ),
    )

    assert (
        "evaluate",
        "first_value",
    ) in first_session.calls

    assert (
        "evaluate",
        "second_value",
    ) not in first_session.calls

    assert (
        "evaluate",
        "second_value",
    ) in second_session.calls

    assert (
        "evaluate",
        "first_value",
    ) not in second_session.calls

    await asyncio.gather(
        manager.close_session(
            first_id
        ),
        manager.close_session(
            second_id
        ),
    )

    await asyncio.gather(
        first_dispatcher,
        second_dispatcher,
    )


@pytest.mark.asyncio
async def test_broker_really_crosses_thread_and_event_loop():
    """
    这个测试最重要：

    requester运行在pytest EventLoop。

    dispatcher运行在另一个线程，
    那个线程内部又有自己的asyncio.run()。

    这和A的Managed Task模型基本一致。
    """

    broker = (
        DebugCommandBroker()
    )

    session = FakeSession()

    started = (
        threading.Event()
    )

    finished = (
        threading.Event()
    )

    thread_error: list[
        BaseException
    ] = []

    def run_managed_loop():
        async def managed():
            started.set()

            await (
                GdbMiCommandDispatcher()
                .run(
                    broker=broker,
                    session=session,
                )
            )

        try:
            asyncio.run(
                managed()
            )

        except BaseException as exc:
            thread_error.append(
                exc
            )

        finally:
            finished.set()

    worker = threading.Thread(
        target=run_managed_loop,
        daemon=True,
    )

    worker.start()

    assert await asyncio.to_thread(
        started.wait,
        1,
    )

    result = await broker.request(
        DebugCommandKind.EVALUATE,
        expression="cross_thread",
        timeout_seconds=2,
    )

    assert result == {
        "value": "42"
    }

    await broker.request(
        DebugCommandKind.CLOSE,
        timeout_seconds=2,
    )

    assert await asyncio.to_thread(
        finished.wait,
        2,
    )

    worker.join(
        timeout=1
    )

    assert (
        thread_error
        == []
    )

    assert (
        "evaluate",
        "cross_thread",
    ) in session.calls