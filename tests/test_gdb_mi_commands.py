import pytest

from app.modules.co_debug.debug.mi_commands import (
    MiCommandBuilder,
)
from app.modules.co_debug.debug.models import (
    DebugBreakpoint,
    DebugSessionState,
    DebugSessionStateModel,
)
from app.modules.co_debug.debug.transport import (
    GdbTransportMessage,
)


def test_command_builder_assigns_independent_tokens():
    builder = MiCommandBuilder()

    breakpoint_command = (
        builder.break_insert(
            "main.c:25"
        )
    )

    run_command = (
        builder.exec_run()
    )

    continue_command = (
        builder.exec_continue()
    )

    assert (
        breakpoint_command.token
        == 1
    )

    assert (
        breakpoint_command.text
        == '1-break-insert "main.c:25"'
    )

    assert run_command.token == 2

    assert (
        run_command.text
        == "2-exec-run"
    )

    assert (
        continue_command.token
        == 3
    )

    assert (
        continue_command.text
        == "3-exec-continue"
    )


def test_two_command_builders_have_independent_tokens():
    first = MiCommandBuilder()

    second = MiCommandBuilder()

    assert (
        first.exec_run().text
        == "1-exec-run"
    )

    assert (
        first.exec_next().text
        == "2-exec-next"
    )

    assert (
        second.exec_run().text
        == "1-exec-run"
    )

    assert (
        second.exec_step().text
        == "2-exec-step"
    )


def test_break_insert_supports_options():
    builder = MiCommandBuilder()

    command = builder.break_insert(
        "src/main file.c:10",
        temporary=True,
        disabled=True,
        condition='value == "test"',
    )

    assert command.text == (
        '1-break-insert '
        '-t '
        '-d '
        '-c "value == \\"test\\"" '
        '"src/main file.c:10"'
    )


def test_breakpoint_control_commands():
    builder = MiCommandBuilder()

    delete_command = (
        builder.break_delete("3")
    )

    enable_command = (
        builder.break_enable("4")
    )

    disable_command = (
        builder.break_disable("5")
    )

    assert (
        delete_command.text
        == "1-break-delete 3"
    )

    assert (
        enable_command.text
        == "2-break-enable 4"
    )

    assert (
        disable_command.text
        == "3-break-disable 5"
    )


def test_execution_commands():
    builder = MiCommandBuilder()

    arguments = (
        builder.exec_arguments(
            [
                "--name",
                "hello world",
            ]
        )
    )

    run = builder.exec_run()

    next_command = (
        builder.exec_next()
    )

    step = builder.exec_step()

    continue_command = (
        builder.exec_continue()
    )

    interrupt = (
        builder.exec_interrupt()
    )

    exit_command = (
        builder.gdb_exit()
    )

    assert arguments.text == (
        '1-exec-arguments '
        '"--name" '
        '"hello world"'
    )

    assert (
        run.text
        == "2-exec-run"
    )

    assert (
        next_command.text
        == "3-exec-next"
    )

    assert (
        step.text
        == "4-exec-step"
    )

    assert (
        continue_command.text
        == "5-exec-continue"
    )

    assert (
        interrupt.text
        == "6-exec-interrupt"
    )

    assert (
        exit_command.text
        == "7-gdb-exit"
    )


def test_inspection_commands():
    builder = MiCommandBuilder()

    frames = (
        builder.stack_list_frames()
    )

    expression = (
        builder.data_evaluate_expression(
            "counter + 1"
        )
    )

    assert (
        frames.text
        == "1-stack-list-frames"
    )

    assert expression.text == (
        '2-data-evaluate-expression '
        '"counter + 1"'
    )


def test_command_builder_escapes_values():
    builder = MiCommandBuilder()

    command = builder.break_insert(
        'src\\main"name.c:7'
    )

    assert command.text == (
        '1-break-insert '
        '"src\\\\main\\"name.c:7"'
    )


def test_empty_required_values_are_rejected():
    builder = MiCommandBuilder()

    with pytest.raises(
        ValueError,
        match="location must not be empty",
    ):
        builder.break_insert("")

    with pytest.raises(
        ValueError,
        match="breakpoint_number must not be empty",
    ):
        builder.break_delete("")

    with pytest.raises(
        ValueError,
        match="expression must not be empty",
    ):
        builder.data_evaluate_expression("")


def test_invalid_first_token_is_rejected():
    with pytest.raises(
        ValueError,
        match="first_token must be greater than 0",
    ):
        MiCommandBuilder(
            first_token=0
        )


def test_debug_session_state_model_starts_clean():
    state = (
        DebugSessionStateModel()
    )

    assert (
        state.state
        == DebugSessionState.STARTING
    )

    assert (
        state.breakpoints
        == {}
    )

    assert (
        state.stop_reason
        is None
    )

    assert (
        state.current_file
        is None
    )

    assert (
        state.current_line
        is None
    )


def test_debug_session_state_can_hold_breakpoint():
    state = (
        DebugSessionStateModel()
    )

    breakpoint = DebugBreakpoint(
        number="1",
        location="main.c:7",
        file="main.c",
        line=7,
        function="main",
    )

    state.breakpoints[
        breakpoint.number
    ] = breakpoint

    state.state = (
        DebugSessionState.STOPPED
    )

    assert (
        state.breakpoints["1"]
        == breakpoint
    )

    assert (
        state.state
        == DebugSessionState.STOPPED
    )


def test_transport_message_keeps_stream_and_text():
    message = GdbTransportMessage(
        stream="stdout",
        text='1^done',
    )

    assert (
        message.stream
        == "stdout"
    )

    assert (
        message.text
        == "1^done"
    )