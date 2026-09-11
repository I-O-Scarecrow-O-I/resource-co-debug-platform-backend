import asyncio

import pytest

from app.modules.co_debug.debug.platform_transport import (
    PlatformGdbTransport,
)
from app.modules.co_debug.debug.transport import (
    GdbTransportClosed,
)


class FakeInteractiveProcessSession:
    def __init__(
        self,
    ) -> None:
        self.inputs: list[str] = []

        self._returncode = None

        self._finished = (
            asyncio.Event()
        )

        self.terminate_calls = 0

    @property
    def returncode(
        self,
    ) -> int | None:
        return self._returncode

    async def write(
        self,
        data: str,
    ) -> None:
        if (
            self._returncode
            is not None
        ):
            raise RuntimeError(
                "interactive process stdin is closed"
            )

        self.inputs.append(data)

    async def wait(
        self,
    ) -> int:
        await self._finished.wait()

        assert (
            self._returncode
            is not None
        )

        return self._returncode

    async def terminate(
        self,
    ) -> None:
        self.terminate_calls += 1

        if (
            self._returncode
            is None
        ):
            self.finish(-9)

    def finish(
        self,
        returncode: int,
    ) -> None:
        self._returncode = (
            returncode
        )

        self._finished.set()


class FakeManagedTaskContext:
    def __init__(
        self,
        session: FakeInteractiveProcessSession,
    ) -> None:
        self.session = session

        self.command = None
        self.workspace = None
        self.work_dir = None

        self.on_output = None

    async def open_interactive_process(
        self,
        command,
        workspace,
        work_dir=".",
        on_output=None,
    ):
        self.command = list(command)
        self.workspace = workspace
        self.work_dir = work_dir
        self.on_output = on_output

        return self.session

    def emit(
        self,
        message: str,
        stream: str = "stdout",
    ) -> None:
        assert (
            self.on_output
            is not None
        )

        self.on_output(
            message,
            stream,
        )


@pytest.mark.asyncio
async def test_platform_transport_opens_process(
    tmp_path,
):
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=[
                "gdb",
                "--interpreter=mi2",
                "./app",
            ],
            workspace=tmp_path,
            work_dir=".",
        )
    )

    try:
        assert context.command == [
            "gdb",
            "--interpreter=mi2",
            "./app",
        ]

        assert (
            context.workspace
            == tmp_path
        )

        assert (
            context.work_dir
            == "."
        )

        assert (
            transport.returncode
            is None
        )

    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_platform_transport_adds_newline():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=[
                "gdb",
                "--interpreter=mi2",
            ],
            workspace="workspace",
        )
    )

    try:
        await transport.send(
            "1-exec-run"
        )

        await transport.send(
            "2-exec-continue\n"
        )

        assert session.inputs == [
            "1-exec-run\n",
            "2-exec-continue\n",
        ]

    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_platform_transport_receives_output():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=["gdb"],
            workspace="workspace",
        )
    )

    try:
        context.emit(
            '1^done,bkpt={number="1"}',
            "stdout",
        )

        context.emit(
            "warning message",
            "stderr",
        )

        first = (
            await transport.receive()
        )

        second = (
            await transport.receive()
        )

        assert first.stream == "stdout"

        assert first.text == (
            '1^done,bkpt={number="1"}'
        )

        assert (
            second.stream
            == "stderr"
        )

        assert (
            second.text
            == "warning message"
        )

    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_platform_transport_waits_for_process():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=["gdb"],
            workspace="workspace",
        )
    )

    session.finish(0)

    assert (
        await transport.wait()
        == 0
    )

    assert (
        transport.returncode
        == 0
    )


@pytest.mark.asyncio
async def test_receive_detects_process_exit():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=["gdb"],
            workspace="workspace",
        )
    )

    session.finish(0)

    with pytest.raises(
        GdbTransportClosed
    ) as error:
        await asyncio.wait_for(
            transport.receive(),
            timeout=1,
        )

    assert (
        error.value.returncode
        == 0
    )


@pytest.mark.asyncio
async def test_send_rejects_closed_process():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=["gdb"],
            workspace="workspace",
        )
    )

    session.finish(7)

    with pytest.raises(
        GdbTransportClosed
    ) as error:
        await transport.send(
            "1-exec-run"
        )

    assert (
        error.value.returncode
        == 7
    )


@pytest.mark.asyncio
async def test_transport_close_terminates_process():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=["gdb"],
            workspace="workspace",
        )
    )

    await transport.close()

    assert (
        session.terminate_calls
        == 1
    )

    assert (
        transport.returncode
        == -9
    )


@pytest.mark.asyncio
async def test_send_rejects_invalid_command():
    session = (
        FakeInteractiveProcessSession()
    )

    context = (
        FakeManagedTaskContext(
            session
        )
    )

    transport = (
        await PlatformGdbTransport.open(
            context=context,
            command=["gdb"],
            workspace="workspace",
        )
    )

    try:
        with pytest.raises(
            ValueError,
            match=(
                "GDB command "
                "must not be empty"
            ),
        ):
            await transport.send("")

        with pytest.raises(
            TypeError,
            match=(
                "GDB command "
                "must be a string"
            ),
        ):
            await transport.send(
                None  # type: ignore[arg-type]
            )

    finally:
        await transport.close()