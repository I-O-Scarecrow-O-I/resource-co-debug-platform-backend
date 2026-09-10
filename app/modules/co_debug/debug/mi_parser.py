from __future__ import annotations

from typing import Any

from app.modules.co_debug.debug.models import (
    MiRecord,
    MiRecordKind,
)


class MiParseError(ValueError):
    """
    GDB/MI 输出格式无法正常解析。
    """


_RECORD_KIND_BY_MARKER = {
    "^": MiRecordKind.RESULT,
    "*": MiRecordKind.EXEC_ASYNC,
    "+": MiRecordKind.STATUS_ASYNC,
    "=": MiRecordKind.NOTIFY_ASYNC,
}


_STREAM_KIND_BY_MARKER = {
    "~": MiRecordKind.CONSOLE_STREAM,
    "@": MiRecordKind.TARGET_STREAM,
    "&": MiRecordKind.LOG_STREAM,
}


def parse_mi_line(
    line: str,
) -> MiRecord:
    """
    解析一行 GDB/MI 输出。

    常见格式：

        1^done
        2^running

        *running,thread-id="all"

        *stopped,
        reason="breakpoint-hit",
        frame={...}

        ~"console output\\n"

        =thread-created,id="1"

        (gdb)
    """

    raw = line.rstrip(
        "\r\n"
    )

    text = raw.strip()

    if not text:
        return MiRecord(
            kind=MiRecordKind.UNKNOWN,
            raw=raw,
        )

    if text == "(gdb)":
        return MiRecord(
            kind=MiRecordKind.PROMPT,
            raw=raw,
        )

    position = 0

    while (
        position < len(text)
        and text[position].isdigit()
    ):
        position += 1

    token = (
        int(text[:position])
        if position > 0
        else None
    )

    if position >= len(text):
        return MiRecord(
            kind=MiRecordKind.UNKNOWN,
            raw=raw,
            token=token,
        )

    marker = text[position]

    body = text[
        position + 1:
    ]

    if marker in _STREAM_KIND_BY_MARKER:
        return _parse_stream_record(
            raw=raw,
            token=token,
            marker=marker,
            body=body,
        )

    if marker in _RECORD_KIND_BY_MARKER:
        return _parse_result_or_async_record(
            raw=raw,
            token=token,
            marker=marker,
            body=body,
        )

    return MiRecord(
        kind=MiRecordKind.UNKNOWN,
        raw=raw,
        token=token,
    )


def _parse_stream_record(
    *,
    raw: str,
    token: int | None,
    marker: str,
    body: str,
) -> MiRecord:
    stripped_body = body.lstrip()

    if not stripped_body.startswith('"'):
        raise MiParseError(
            "MI stream record must contain a string"
        )

    parser = _MiValueParser(
        body
    )

    value = parser.parse_value()

    parser.ensure_finished()

    if not isinstance(
        value,
        str,
    ):
        raise MiParseError(
            "MI stream record must contain a string"
        )

    return MiRecord(
        kind=_STREAM_KIND_BY_MARKER[
            marker
        ],
        raw=raw,
        token=token,
        payload=value,
    )


def _parse_result_or_async_record(
    *,
    raw: str,
    token: int | None,
    marker: str,
    body: str,
) -> MiRecord:
    message_class, separator, remainder = (
        body.partition(",")
    )

    if not message_class:
        raise MiParseError(
            "MI record class must not be empty"
        )

    payload: dict[
        str,
        Any,
    ] = {}

    if separator:
        parser = _MiValueParser(
            remainder
        )

        payload = (
            parser.parse_results()
        )

        parser.ensure_finished()

    return MiRecord(
        kind=_RECORD_KIND_BY_MARKER[
            marker
        ],
        raw=raw,
        token=token,
        message_class=message_class,
        payload=payload,
    )


class _MiValueParser:
    """
    解析 GDB/MI 中的 value / tuple / list / result。

    这里只负责：

        name="value"

        name={
            ...
        }

        name=[
            ...
        ]
    """

    def __init__(
        self,
        text: str,
    ) -> None:
        self.text = text
        self.position = 0

    def parse_results(
        self,
        *,
        end_character: str | None = None,
    ) -> dict[str, Any]:
        results: dict[
            str,
            Any,
        ] = {}

        self._skip_whitespace()

        while not self._finished():
            if (
                end_character is not None
                and self._peek()
                == end_character
            ):
                break

            name = (
                self._parse_name()
            )

            self._skip_whitespace()

            self._expect("=")

            value = (
                self.parse_value()
            )

            results[
                name
            ] = value

            self._skip_whitespace()

            if self._finished():
                break

            if (
                end_character is not None
                and self._peek()
                == end_character
            ):
                break

            self._expect(",")

            self._skip_whitespace()

        return results

    def parse_value(
        self,
    ) -> Any:
        self._skip_whitespace()

        if self._finished():
            raise MiParseError(
                "unexpected end of MI value"
            )

        character = self._peek()

        if character == '"':
            return (
                self._parse_c_string()
            )

        if character == "{":
            return (
                self._parse_tuple()
            )

        if character == "[":
            return (
                self._parse_list()
            )

        return (
            self._parse_bare_value()
        )

    def ensure_finished(
        self,
    ) -> None:
        self._skip_whitespace()

        if not self._finished():
            raise MiParseError(
                "unexpected trailing MI data"
            )

    def _parse_tuple(
        self,
    ) -> dict[str, Any]:
        self._expect("{")

        self._skip_whitespace()

        if (
            not self._finished()
            and self._peek() == "}"
        ):
            self.position += 1

            return {}

        result = self.parse_results(
            end_character="}",
        )

        self._expect("}")

        return result

    def _parse_list(
        self,
    ) -> list[Any]:
        self._expect("[")

        self._skip_whitespace()

        result: list[
            Any
        ] = []

        if (
            not self._finished()
            and self._peek() == "]"
        ):
            self.position += 1

            return result

        while True:
            self._skip_whitespace()

            saved_position = (
                self.position
            )

            result_item = (
                self._try_parse_result()
            )

            if result_item is None:
                self.position = (
                    saved_position
                )

                result.append(
                    self.parse_value()
                )

            else:
                name, value = (
                    result_item
                )

                result.append(
                    {
                        name: value,
                    }
                )

            self._skip_whitespace()

            if self._finished():
                raise MiParseError(
                    "unterminated MI list"
                )

            if self._peek() == "]":
                self.position += 1

                break

            self._expect(",")

        return result

    def _try_parse_result(
        self,
    ) -> tuple[
        str,
        Any,
    ] | None:
        saved_position = (
            self.position
        )

        try:
            name = (
                self._parse_name()
            )

            self._skip_whitespace()

            if (
                self._finished()
                or self._peek()
                != "="
            ):
                self.position = (
                    saved_position
                )

                return None

            self.position += 1

            value = (
                self.parse_value()
            )

            return (
                name,
                value,
            )

        except MiParseError:
            self.position = (
                saved_position
            )

            return None

    def _parse_name(
        self,
    ) -> str:
        self._skip_whitespace()

        start = (
            self.position
        )

        while not self._finished():
            character = self._peek()

            if (
                character.isalnum()
                or character
                in "_-."
            ):
                self.position += 1

                continue

            break

        if (
            start
            == self.position
        ):
            raise MiParseError(
                "expected MI result name"
            )

        return self.text[
            start:self.position
        ]

    def _parse_bare_value(
        self,
    ) -> str:
        start = (
            self.position
        )

        while not self._finished():
            if self._peek() in (
                ",",
                "]",
                "}",
            ):
                break

            self.position += 1

        value = (
            self.text[
                start:self.position
            ].strip()
        )

        if not value:
            raise MiParseError(
                "expected MI value"
            )

        return value

    def _parse_c_string(
        self,
    ) -> str:
        self._expect('"')

        result: list[
            str
        ] = []

        while not self._finished():
            character = (
                self._peek()
            )

            self.position += 1

            if character == '"':
                return "".join(
                    result
                )

            if character != "\\":
                result.append(
                    character
                )

                continue

            if self._finished():
                raise MiParseError(
                    "unterminated MI escape sequence"
                )

            escaped = (
                self._peek()
            )

            self.position += 1

            escape_map = {
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "\\": "\\",
                '"': '"',
                "a": "\a",
                "b": "\b",
                "f": "\f",
                "v": "\v",
            }

            if (
                escaped
                in escape_map
            ):
                result.append(
                    escape_map[
                        escaped
                    ]
                )

                continue

            if escaped == "x":
                result.append(
                    self._parse_hex_escape()
                )

                continue

            if (
                escaped
                in "01234567"
            ):
                result.append(
                    self._parse_octal_escape(
                        escaped
                    )
                )

                continue

            # 对未知转义不过度报错，
            # 保留转义后的字符。
            result.append(
                escaped
            )

        raise MiParseError(
            "unterminated MI string"
        )

    def _parse_hex_escape(
        self,
    ) -> str:
        start = (
            self.position
        )

        while (
            not self._finished()
            and self._peek().lower()
            in "0123456789abcdef"
        ):
            self.position += 1

        if (
            start
            == self.position
        ):
            return "x"

        value = int(
            self.text[
                start:self.position
            ],
            16,
        )

        return chr(
            value
        )

    def _parse_octal_escape(
        self,
        first_digit: str,
    ) -> str:
        digits = [
            first_digit
        ]

        for _ in range(2):
            if (
                self._finished()
                or self._peek()
                not in "01234567"
            ):
                break

            digits.append(
                self._peek()
            )

            self.position += 1

        value = int(
            "".join(digits),
            8,
        )

        return chr(
            value
        )

    def _expect(
        self,
        character: str,
    ) -> None:
        self._skip_whitespace()

        if (
            self._finished()
            or self._peek()
            != character
        ):
            raise MiParseError(
                f"expected '{character}'"
            )

        self.position += 1

    def _skip_whitespace(
        self,
    ) -> None:
        while (
            not self._finished()
            and self._peek().isspace()
        ):
            self.position += 1

    def _peek(
        self,
    ) -> str:
        return self.text[
            self.position
        ]

    def _finished(
        self,
    ) -> bool:
        return (
            self.position
            >= len(self.text)
        )