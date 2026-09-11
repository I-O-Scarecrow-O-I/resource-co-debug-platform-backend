import asyncio

import pytest

from app.modules.co_debug.debug.models import (
    DebugSessionState,
)
from app.modules.co_debug.debug.session import (
    GdbMiCommandError,
    GdbMiError,
    GdbMiSession,
    GdbMiTimeout,
)
from app.modules.co_debug.debug.transport import (
    GdbTransportClosed,
    GdbTransportMessage,
)


class FakeGdbTransport:
    def __init__(
        self,
    ) -> None:
        self.sent: list[str] = []

        self._returncode = None

        self._messages: asyncio.Queue[
            GdbTransportMessage
            | BaseException
        ] = asyncio.Queue()

        self._finished = (
            asyncio.Event()
        )

    @property
    def returncode(
        self,
    ) -> int | None:
        return self._returncode

    async def send(
        self,
        command: str,
    ) -> None:
        if (
            self._returncode
            is not None
        ):
            raise GdbTransportClosed(
                self._returncode
            )

        self.sent.append(
            command
        )

    async def receive(
        self,
    ) -> GdbTransportMessage:
        item = await (
            self._messages.get()
        )

        if isinstance(
            item,
            BaseException,
        ):
            raise item

        return item

    async def wait(
        self,
    ) -> int:
        await self._finished.wait()

        assert (
            self._returncode
            is not None
        )

        return self._returncode

    async def close(
        self,
    ) -> None:
        if (
            self._returncode
            is None
        ):
            self.finish(-9)

    def emit(
        self,
        text: str,
        stream: str = "stdout",
    ) -> None:
        self._messages.put_nowait(
            GdbTransportMessage(
                stream=stream,
                text=text,
            )
        )

    def finish(
        self,
        returncode: int,
    ) -> None:
        if (
            self._returncode
            is not None
        ):
            return

        self._returncode = (
            returncode
        )

        self._finished.set()

        self._messages.put_nowait(
            GdbTransportClosed(
                returncode
            )
        )


async def _wait_until(
    predicate,
    *,
    timeout: float = 1,
):
    async with asyncio.timeout(
        timeout
    ):
        while not predicate():
            await asyncio.sleep(
                0.001
            )


async def _start_session(
    transport: FakeGdbTransport,
    *,
    command_timeout_seconds: float = 1,
) -> GdbMiSession:
    session = GdbMiSession(
        transport,
        command_timeout_seconds=(
            command_timeout_seconds
        ),
    )

    transport.emit(
        '~"GNU gdb test\\n"'
    )

    transport.emit(
        "(gdb)"
    )

    await session.start()

    return session


@pytest.mark.asyncio
async def test_session_waits_for_initial_prompt():
    transport = (
        FakeGdbTransport()
    )

    session = GdbMiSession(
        transport
    )

    start_task = (
        asyncio.create_task(
            session.start()
        )
    )

    await asyncio.sleep(
        0
    )

    assert (
        session.state.state
        == DebugSessionState.STARTING
    )

    transport.emit(
        '~"GNU gdb\\n"'
    )

    transport.emit(
        "(gdb)"
    )

    await start_task

    assert (
        session.state.state
        == DebugSessionState.READY
    )

    assert (
        session.console_output
        == (
            "GNU gdb\n",
        )
    )

    await session.close()


@pytest.mark.asyncio
async def test_insert_breakpoint_updates_state():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        command_task = (
            asyncio.create_task(
                session.insert_breakpoint(
                    "main.c:7"
                )
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        assert (
            transport.sent[0]
            == (
                '1-break-insert '
                '"main.c:7"'
            )
        )

        transport.emit(
            '1^done,bkpt={'
            'number="1",'
            'enabled="y",'
            'func="main",'
            'file="main.c",'
            'fullname="/tmp/main.c",'
            'line="7"'
            '}'
        )

        breakpoint = (
            await command_task
        )

        assert (
            breakpoint.number
            == "1"
        )

        assert (
            breakpoint.location
            == "main.c:7"
        )

        assert (
            breakpoint.file
            == "main.c"
        )

        assert (
            breakpoint.fullname
            == "/tmp/main.c"
        )

        assert (
            breakpoint.line
            == 7
        )

        assert (
            breakpoint.function
            == "main"
        )

        assert (
            session.state
            .breakpoints["1"]
            == breakpoint
        )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_run_and_breakpoint_hit_update_state():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        run_task = (
            asyncio.create_task(
                session.run()
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        assert (
            transport.sent[0]
            == "1-exec-run"
        )

        transport.emit(
            "1^running"
        )

        transport.emit(
            '*running,'
            'thread-id="all"'
        )

        await run_task

        assert (
            session.state.state
            == DebugSessionState.RUNNING
        )

        transport.emit(
            '*stopped,'
            'reason="breakpoint-hit",'
            'bkptno="1",'
            'frame={'
            'func="main",'
            'file="main.c",'
            'fullname="/project/main.c",'
            'line="12"'
            '}'
        )

        state = (
            await session.wait_for_stop(
                timeout_seconds=1
            )
        )

        assert (
            state
            == DebugSessionState.STOPPED
        )

        assert (
            session.state.stop_reason
            == "breakpoint-hit"
        )

        assert (
            session.state.current_file
            == "/project/main.c"
        )

        assert (
            session.state.current_line
            == 12
        )

        assert (
            session.state.current_function
            == "main"
        )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_continue_next_and_step():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        operations = [
            (
                session.continue_execution,
                "1-exec-continue",
                "1^running",
            ),
            (
                session.next,
                "2-exec-next",
                "2^running",
            ),
            (
                session.step,
                "3-exec-step",
                "3^running",
            ),
        ]

        for (
            operation,
            expected_command,
            response,
        ) in operations:
            task = asyncio.create_task(
                operation()
            )

            expected_count = (
                len(
                    transport.sent
                )
                + 1
            )

            await _wait_until(
                lambda: len(
                    transport.sent
                )
                >= expected_count
            )

            assert (
                transport.sent[-1]
                == expected_command
            )

            transport.emit(
                response
            )

            await task

            assert (
                session.state.state
                == DebugSessionState.RUNNING
            )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_program_exit_updates_state():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        run_task = (
            asyncio.create_task(
                session.run()
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        transport.emit(
            "1^running"
        )

        await run_task

        transport.emit(
            '*stopped,'
            'reason="exited-normally"'
        )

        state = (
            await session.wait_for_stop(
                timeout_seconds=1
            )
        )

        assert (
            state
            == DebugSessionState.EXITED
        )

        assert (
            session.state.stop_reason
            == "exited-normally"
        )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_command_error_is_raised():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        command_task = (
            asyncio.create_task(
                session.insert_breakpoint(
                    "missing.c:99"
                )
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        transport.emit(
            '1^error,'
            'msg="No source file named missing.c."'
        )

        with pytest.raises(
            GdbMiCommandError,
            match=(
                "No source file "
                "named missing.c."
            ),
        ):
            await command_task

        assert (
            session.state.state
            == DebugSessionState.READY
        )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_expression_evaluation():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        task = (
            asyncio.create_task(
                session
                .evaluate_expression(
                    "counter + 1"
                )
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        assert (
            transport.sent[0]
            == (
                "1-data-evaluate-expression "
                '"counter + 1"'
            )
        )

        transport.emit(
            '1^done,value="42"'
        )

        assert (
            await task
            == "42"
        )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_stack_frames_are_returned():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        task = (
            asyncio.create_task(
                session
                .list_stack_frames()
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        transport.emit(
            '1^done,stack=['
            'frame={'
            'level="0",'
            'func="main",'
            'file="main.c",'
            'line="7"'
            '}'
            ']'
        )

        frames = await task

        assert frames == [
            {
                "frame": {
                    "level": "0",
                    "func": "main",
                    "file": "main.c",
                    "line": "7",
                }
            }
        ]

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_stderr_is_preserved():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        transport.emit(
            "warning from gdb",
            "stderr",
        )

        await _wait_until(
            lambda: (
                session.stderr_output
                == (
                    "warning from gdb",
                )
            )
        )

        assert (
            session.stderr_output
            == (
                "warning from gdb",
            )
        )

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_command_timeout():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport,
        command_timeout_seconds=0.05,
    )

    try:
        with pytest.raises(
            GdbMiTimeout,
            match=(
                "timed out waiting for "
                "GDB/MI command response"
            ),
        ):
            await session.run()

    finally:
        await session.close()


@pytest.mark.asyncio
async def test_transport_failure_marks_session_failed():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    transport.finish(2)

    state = (
        await session.wait_for_stop(
            timeout_seconds=1
        )
    )

    assert (
        state
        == DebugSessionState.FAILED
    )

    assert (
        transport.returncode
        == 2
    )

    await session.close()


@pytest.mark.asyncio
async def test_normal_transport_exit_marks_session_exited():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    transport.finish(0)

    state = (
        await session.wait_for_stop(
            timeout_seconds=1
        )
    )

    assert (
        state
        == DebugSessionState.EXITED
    )

    await session.close()


@pytest.mark.asyncio
async def test_session_rejects_command_before_start():
    transport = (
        FakeGdbTransport()
    )

    session = GdbMiSession(
        transport
    )

    with pytest.raises(
        GdbMiError,
        match=(
            "has not been started"
        ),
    ):
        await session.run()

    await session.close()


@pytest.mark.asyncio
async def test_delete_breakpoint_updates_state():
    transport = (
        FakeGdbTransport()
    )

    session = await _start_session(
        transport
    )

    try:
        insert = (
            asyncio.create_task(
                session.insert_breakpoint(
                    "main.c:7"
                )
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 1
        )

        transport.emit(
            '1^done,bkpt={'
            'number="1",'
            'enabled="y",'
            'file="main.c",'
            'line="7"'
            '}'
        )

        await insert

        assert (
            "1"
            in session.state.breakpoints
        )

        delete = (
            asyncio.create_task(
                session.delete_breakpoint(
                    "1"
                )
            )
        )

        await _wait_until(
            lambda: len(
                transport.sent
            ) == 2
        )

        assert (
            transport.sent[-1]
            == "2-break-delete 1"
        )

        transport.emit(
            "2^done"
        )

        await delete

        assert (
            "1"
            not in session.state.breakpoints
        )

    finally:
        await session.close()