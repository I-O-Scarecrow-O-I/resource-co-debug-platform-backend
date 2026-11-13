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


# 结果与异步记录共享载荷语法，但不同标记由上层分别用于请求配对和状态推进。
_RECORD_KIND_BY_MARKER = {
    "^": MiRecordKind.RESULT,
    "*": MiRecordKind.EXEC_ASYNC,
    "+": MiRecordKind.STATUS_ASYNC,
    "=": MiRecordKind.NOTIFY_ASYNC,
}


# 流记录保留各自的来源，控制台、被调试程序和诊断输出不能按结果处理。
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

    # 保留原始记录供诊断，解析视图单独去掉外围空白。
    # 只移除行终止符以保留原始缩进；裁剪副本专供语法识别。
    raw = line.rstrip(
        "\r\n"
    )

    text = raw.strip()

    # 空行仍保留原文形状，状态机不将它视作握手或命令应答。
    if not text:
        return MiRecord(
            kind=MiRecordKind.UNKNOWN,
            raw=raw,
        )

    # 提示符只标识协议边界，不是某条命令的结果。
    # 提示符没有命令号或业务负载，供会话结束首次握手。
    if text == "(gdb)":
        return MiRecord(
            kind=MiRecordKind.PROMPT,
            raw=raw,
        )

    # 只在标记前识别十进制 token；无 token 的异步通知仍可进入状态处理。
    # 仅前缀连续数字可能作为 token；没有后续协议标记仍当普通输出。
    position = 0

    while (
        position < len(text)
        and text[position].isdigit()
    ):
        position += 1

    # 编号只从已读取的数字前缀形成；无编号记录仍可参与异步状态处理。
    token = (
        int(text[:position])
        if position > 0
        else None
    )

    # 截断的 token 不足以确定记录类型，保留原文而不伪造结果。
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

    # 先按记录标记分流，再按对应载荷语法解析。
    # 流记录和控制记录分别解析，前者不被解释为状态键值表。
    if marker in _STREAM_KIND_BY_MARKER:
        return _parse_stream_record(
            raw=raw,
            token=token,
            marker=marker,
            body=body,
        )

    # 控制记录共享结构解析入口，调用方根据标记决定结果与异步事件的去向。
    if marker in _RECORD_KIND_BY_MARKER:
        return _parse_result_or_async_record(
            raw=raw,
            token=token,
            marker=marker,
            body=body,
        )

    # 未知标记保留在日志可见范围内，不冒充任何已识别的事件。
    # 未知前缀保留原文；已识别标记后出现格式错误则显式报告。
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
    # 流记录必须携带一个引号字符串，后续不允许再拼接其他值。
    # 识别为流后必须有引号包围的负载，缺失不能降级为普通文本。
    stripped_body = body.lstrip()

    if not stripped_body.startswith('"'):
        raise MiParseError(
            "MI stream record must contain a string"
        )

    # 字符串负载复用通用值解析以获得一致的转义处理，但仍限定顶层只能是文本。
    parser = _MiValueParser(
        body
    )

    value = parser.parse_value()

    # 完整校验负载尾部，避免把拼接或截断内容当成一条合法流记录。
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
    # 第一段是结果或通知类别，余下部分才是键值对。
    # 首逗号前为消息类，嵌套逗号留给值解析器。
    # 没有字段的应答也会产生空字典。
    message_class, separator, remainder = (
        body.partition(",")
    )

    # 没有消息类别的协议记录不具备状态语义，因此拒绝继续解释其负载。
    if not message_class:
        raise MiParseError(
            "MI record class must not be empty"
        )

    payload: dict[
        str,
        Any,
    ] = {}

    # 无逗号时允许空载荷，例如只有命令成功类别的响应。
    if separator:
        parser = _MiValueParser(
            remainder
        )

        payload = (
            parser.parse_results()
        )

        parser.ensure_finished()

    # 标记映射保留记录类型，不能仅凭相同消息类将异步通知误配为结果。
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

    # 游标由嵌套解析共享，各层函数只消费自己负责的边界。
    def __init__(
        self,
        text: str,
    ) -> None:
        # 文本与游标属于一条记录；嵌套容器解析结束后沿用同一个游标继续读取。
        self.text = text
        self.position = 0

    # 根结果和元组复用键值语法；元组终止符留给调用方消费。
    def parse_results(
        self,
        *,
        end_character: str | None = None,
    ) -> dict[str, Any]:
        results: dict[
            str,
            Any,
        ] = {}

        # 元组复用根字段解析逻辑，外层负责消费闭合符。
        # 结果映射中同名键保留最后一个值。
        self._skip_whitespace()

        while not self._finished():
            if (
                end_character is not None
                and self._peek()
                == end_character
            ):
                break

            # 每个结果必须先读到可识别的名字，再解析赋值符后的值；缺名不是空键。
            name = (
                self._parse_name()
            )

            self._skip_whitespace()

            self._expect("=")

            value = (
                self.parse_value()
            )

            # 结果对象按键索引；同名键被后出现的值覆盖。
            # 需要保留重复键的列表项使用独立对象表示。
            results[
                name
            ] = value

            self._skip_whitespace()

            # 顶层读到输入末尾即可结束；嵌套元组仍要由外层确认闭合符。
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

    # 由首字符选择字符串、元组或列表；其余值按兼容文本读取。
    def parse_value(
        self,
    ) -> Any:
        # 引号、元组和列表按首字符分派；其他标量保留文本，数字语义由使用方决定。
        self._skip_whitespace()

        # 已经请求读取一个值时遇到输入尾不是空值；缺失负载应被记录为格式错误。
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

    # 顶层解析后要求无残留，避免只接受有效前缀就更新调试状态。
    def ensure_finished(
        self,
    ) -> None:
        # 要求根负载没有剩余内容；局部递归解析则保留容器终止符。
        self._skip_whitespace()

        if not self._finished():
            raise MiParseError(
                "unexpected trailing MI data"
            )

    # 元组由花括号包围，空元组也是一个合法对象。
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

        # 解析成员时把闭合符留给本层检查，不接受只包含若干合法字段的截断元组。
        result = self.parse_results(
            end_character="}",
        )

        self._expect("}")

        return result

    # 列表可以包含值或带名字的结果项，始终保留原有顺序。
    def _parse_list(
        self,
    ) -> list[Any]:
        self._expect("[")

        self._skip_whitespace()

        result: list[
            Any
        ] = []

        # 空列表走早返回，但仍推进游标越过闭合符供父结构继续使用。
        if (
            not self._finished()
            and self._peek() == "]"
        ):
            self.position += 1

            return result

        # 列表允许裸值和命名结果混排。
        # 先试探键值对，失败后回退到原游标解析一个值。
        while True:
            self._skip_whitespace()

            # 用游标快照区分结果项和普通值，试探失败后必须从同一位置回读。
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

            # 命名结果存为独立的单键条目，保留顺序及重复名称，例如多个 frame。
            else:
                # 每个具名项独立存储，不能合并成字典而丢失重复的名字。
                name, value = (
                    result_item
                )

                result.append(
                    {
                        name: value,
                    }
                )

            self._skip_whitespace()

            # 没有结束方括号的输出不能被作为完整数组交付。
            # 输入耗尽不代表列表闭合，缺括号时不能返回部分解析结果。
            if self._finished():
                raise MiParseError(
                    "unterminated MI list"
                )

            # 成功读到闭合符才退出列表循环，逗号或尾部结束都不能替代闭合。
            if self._peek() == "]":
                self.position += 1

                break

            self._expect(",")

        return result

    # 试读失败不消费输入，普通值分支仍可完整解析该项。
    def _try_parse_result(
        self,
    ) -> tuple[
        str,
        Any,
    ] | None:
        # 试探阶段发现没有等号或解析异常时恢复位置，交给其他值形式。
        saved_position = (
            self.position
        )

        try:
            name = (
                self._parse_name()
            )

            # 键名与等号之间允许空白；无等号时试探结果放弃，游标恢复。
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

        # 试探阶段出现结构错误可回到值解析分支；上层仍会检查剩余输入。
        except MiParseError:
            self.position = (
                saved_position
            )

            return None

    # 名称允许 GDB 常用的连字符和点；遇到分隔符时由上层处理。
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

        # 未消耗任何名称字符就报格式错误，避免结果循环在同一位置重复读。
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

    # 兼容未加引号的值，但不能吞掉外层列表或元组的结束符。
    def _parse_bare_value(
        self,
    ) -> str:
        # 标量读到分隔符为止，外层消费分隔符；缺值与引号包围的空串不同。
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

        # 未加引号且去空白后没有内容不是合法值；合法空字符串应使用引号。
        if not value:
            raise MiParseError(
                "expected MI value"
            )

        return value

    # 逐字符反转义；未转义的结束引号才封闭当前字符串。
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

            # 普通字符原样收录，只有反斜杠进入转义路径。
            if character != "\\":
                result.append(
                    character
                )

                continue

            # 字符串在反斜杠后截断属于不完整记录，不能补出一个字符。
            # 孤立的反斜杠属于未完成的转义，与可识别边界的未知转义不同。
            if self._finished():
                raise MiParseError(
                    "unterminated MI escape sequence"
                )

            escaped = (
                self._peek()
            )

            self.position += 1

            # 用显式映射保留 GDB 输出中的控制字符语义，不让 Python 解释整行文本。
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

            # 数字转义消费自己的有效前缀，结束后回到同一字符串扫描。
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
            # 未知转义采用兼容输出，不能据此推断 GDB 发出的协议已经丢失边界。
            result.append(
                escaped
            )

        # 文本耗尽仍未找到结束引号时，已收集的内容不会作为完整字符串返回。
        raise MiParseError(
            "unterminated MI string"
        )

    # 十六进制转义连续消费十六进制数字，无数字时保留字面标记。
    def _parse_hex_escape(
        self,
    ) -> str:
        # 十六进制转义取连续的十六进制数字，没有数字时保留标记字母。
        start = (
            self.position
        )

        while (
            not self._finished()
            and self._peek().lower()
            in "0123456789abcdef"
        ):
            self.position += 1

        # 十六进制标记后没有数字时兼容为字面字符，游标不吞后续非数字文本。
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

    # 首位八进制数字已由调用方消费，再读至多两位以免吞掉后续文本。
    def _parse_octal_escape(
        self,
        first_digit: str,
    ) -> str:
        # 八进制首位已被消费，最多再取两位，余下字符留给主扫描。
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

    # 由当前结构验证预期分隔符，截断和错误符号走同一格式异常。
    def _expect(
        self,
        character: str,
    ) -> None:
        # 检查结构符之前可以跨过空白；成功后只消费当前期望的一个符号。
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

    # peek 只读取当前字符，不移动游标；调用前必须确保尚未到输入尾。
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