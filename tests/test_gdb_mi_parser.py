import pytest

from app.modules.co_debug.debug.mi_parser import (
    MiParseError,
    parse_mi_line,
)
from app.modules.co_debug.debug.models import (
    MiRecordKind,
)


def test_parse_simple_done_result():
    record = parse_mi_line(
        "1^done"
    )

    assert (
        record.kind
        == MiRecordKind.RESULT
    )

    assert record.token == 1

    assert (
        record.message_class
        == "done"
    )

    assert record.payload == {}

    assert record.is_done is True

    assert (
        record.is_error
        is False
    )


def test_parse_error_result():
    record = parse_mi_line(
        '5^error,msg="Cannot insert breakpoint"'
    )

    assert (
        record.kind
        == MiRecordKind.RESULT
    )

    assert record.token == 5

    assert (
        record.message_class
        == "error"
    )

    assert record.is_error is True

    assert record.payload == {
        "msg": (
            "Cannot insert breakpoint"
        )
    }


def test_parse_breakpoint_result():
    record = parse_mi_line(
        '1^done,bkpt={'
        'number="1",'
        'type="breakpoint",'
        'disp="keep",'
        'enabled="y",'
        'addr="0x0000000000401126",'
        'func="main",'
        'file="main.c",'
        'fullname="/tmp/main.c",'
        'line="7",'
        'thread-groups=["i1"]'
        '}'
    )

    assert (
        record.kind
        == MiRecordKind.RESULT
    )

    assert record.token == 1

    breakpoint = (
        record.payload[
            "bkpt"
        ]
    )

    assert (
        breakpoint["number"]
        == "1"
    )

    assert (
        breakpoint["enabled"]
        == "y"
    )

    assert (
        breakpoint["func"]
        == "main"
    )

    assert (
        breakpoint["file"]
        == "main.c"
    )

    assert (
        breakpoint["fullname"]
        == "/tmp/main.c"
    )

    assert (
        breakpoint["line"]
        == "7"
    )

    assert (
        breakpoint[
            "thread-groups"
        ]
        == [
            "i1",
        ]
    )


def test_parse_running_result():
    record = parse_mi_line(
        "2^running"
    )

    assert (
        record.kind
        == MiRecordKind.RESULT
    )

    assert record.token == 2

    assert (
        record.message_class
        == "running"
    )

    assert (
        record.is_running
        is True
    )


def test_parse_async_running():
    record = parse_mi_line(
        '*running,thread-id="all"'
    )

    assert (
        record.kind
        == MiRecordKind.EXEC_ASYNC
    )

    assert (
        record.message_class
        == "running"
    )

    assert (
        record.is_running
        is True
    )

    assert record.payload == {
        "thread-id": "all"
    }


def test_parse_breakpoint_hit():
    record = parse_mi_line(
        '*stopped,'
        'reason="breakpoint-hit",'
        'disp="keep",'
        'bkptno="1",'
        'frame={'
        'addr="0x0000000000401126",'
        'func="main",'
        'args=[],'
        'file="main.c",'
        'fullname="/tmp/main.c",'
        'line="7"'
        '},'
        'thread-id="1",'
        'stopped-threads="all",'
        'core="0"'
    )

    assert (
        record.kind
        == MiRecordKind.EXEC_ASYNC
    )

    assert (
        record.message_class
        == "stopped"
    )

    assert (
        record.is_stopped
        is True
    )

    assert (
        record.payload[
            "reason"
        ]
        == "breakpoint-hit"
    )

    assert (
        record.payload[
            "bkptno"
        ]
        == "1"
    )

    frame = (
        record.payload[
            "frame"
        ]
    )

    assert (
        frame["func"]
        == "main"
    )

    assert (
        frame["file"]
        == "main.c"
    )

    assert (
        frame["line"]
        == "7"
    )

    assert (
        frame["args"]
        == []
    )


def test_parse_program_exit():
    record = parse_mi_line(
        '*stopped,'
        'reason="exited-normally"'
    )

    assert (
        record.kind
        == MiRecordKind.EXEC_ASYNC
    )

    assert (
        record.message_class
        == "stopped"
    )

    assert (
        record.payload[
            "reason"
        ]
        == "exited-normally"
    )


def test_parse_console_stream():
    record = parse_mi_line(
        '~"hello world\\n"'
    )

    assert (
        record.kind
        == MiRecordKind.CONSOLE_STREAM
    )

    assert record.token is None

    assert (
        record.payload
        == "hello world\n"
    )


def test_parse_target_stream():
    record = parse_mi_line(
        '@"program output\\n"'
    )

    assert (
        record.kind
        == MiRecordKind.TARGET_STREAM
    )

    assert (
        record.payload
        == "program output\n"
    )


def test_parse_log_stream():
    record = parse_mi_line(
        '&"warning: \\"value\\"\\n"'
    )

    assert (
        record.kind
        == MiRecordKind.LOG_STREAM
    )

    assert (
        record.payload
        == 'warning: "value"\n'
    )


def test_parse_notify_async_record():
    record = parse_mi_line(
        '=thread-created,'
        'id="1",'
        'group-id="i1"'
    )

    assert (
        record.kind
        == MiRecordKind.NOTIFY_ASYNC
    )

    assert (
        record.message_class
        == "thread-created"
    )

    assert record.payload == {
        "id": "1",
        "group-id": "i1",
    }


def test_parse_status_async_record():
    record = parse_mi_line(
        '+download,'
        'section=".text",'
        'section-size="100"'
    )

    assert (
        record.kind
        == MiRecordKind.STATUS_ASYNC
    )

    assert (
        record.message_class
        == "download"
    )

    assert record.payload == {
        "section": ".text",
        "section-size": "100",
    }


def test_parse_stack_list():
    record = parse_mi_line(
        '3^done,stack=['
        'frame={'
        'level="0",'
        'func="main",'
        'file="main.c",'
        'line="7"'
        '},'
        'frame={'
        'level="1",'
        'func="helper",'
        'file="helper.c",'
        'line="15"'
        '}'
        ']'
    )

    assert (
        record.kind
        == MiRecordKind.RESULT
    )

    stack = (
        record.payload[
            "stack"
        ]
    )

    assert stack == [
        {
            "frame": {
                "level": "0",
                "func": "main",
                "file": "main.c",
                "line": "7",
            }
        },
        {
            "frame": {
                "level": "1",
                "func": "helper",
                "file": "helper.c",
                "line": "15",
            }
        },
    ]


def test_parse_simple_value_list():
    record = parse_mi_line(
        '1^done,values=['
        '"first",'
        '"second",'
        '"third"'
        ']'
    )

    assert (
        record.payload[
            "values"
        ]
        == [
            "first",
            "second",
            "third",
        ]
    )


def test_parse_empty_tuple():
    record = parse_mi_line(
        "1^done,data={}"
    )

    assert (
        record.payload[
            "data"
        ]
        == {}
    )


def test_parse_empty_list():
    record = parse_mi_line(
        "1^done,data=[]"
    )

    assert (
        record.payload[
            "data"
        ]
        == []
    )


def test_parse_string_escape_sequences():
    record = parse_mi_line(
        '~"line1\\n'
        'line2\\t'
        '\\"quoted\\"'
        '\\\\path"'
    )

    assert record.payload == (
        'line1\n'
        'line2\t'
        '"quoted"'
        '\\path'
    )


def test_parse_gdb_prompt():
    record = parse_mi_line(
        "(gdb)"
    )

    assert (
        record.kind
        == MiRecordKind.PROMPT
    )

    assert record.token is None

    assert (
        record.message_class
        is None
    )


def test_empty_line_is_unknown():
    record = parse_mi_line(
        "\n"
    )

    assert (
        record.kind
        == MiRecordKind.UNKNOWN
    )


def test_unrecognized_output_is_unknown():
    record = parse_mi_line(
        "unexpected ordinary output"
    )

    assert (
        record.kind
        == MiRecordKind.UNKNOWN
    )

    assert (
        record.raw
        == "unexpected ordinary output"
    )


def test_invalid_stream_record_raises():
    with pytest.raises(
        MiParseError,
        match=(
            "MI stream record "
            "must contain a string"
        ),
    ):
        parse_mi_line(
            "~something"
        )


def test_unterminated_tuple_raises():
    with pytest.raises(
        MiParseError
    ):
        parse_mi_line(
            '1^done,data={'
            'name="test"'
        )


def test_unterminated_string_raises():
    with pytest.raises(
        MiParseError,
        match="unterminated MI string",
    ):
        parse_mi_line(
            '~"hello'
        )
def test_prompt_accepts_trailing_whitespace():
    record = parse_mi_line("(gdb) ")

    assert (
        record.kind
        == MiRecordKind.PROMPT
    )