from __future__ import annotations

from app.modules.co_debug.debug.models import (
    MiCommand,
)


class MiCommandBuilder:
    """
    GDB/MI 命令生成器。

    每个 GdbMiSession 都拥有自己独立的 Builder。

    因此 B8 多会话时：

        Session A:
            token 1, 2, 3 ...

        Session B:
            token 1, 2, 3 ...

    两个 GDB 进程之间互不影响。
    """

    def __init__(
        self,
        first_token: int = 1,
    ) -> None:
        if first_token < 1:
            raise ValueError(
                "first_token must be greater than 0"
            )

        self._next_token = first_token

    def break_insert(
        self,
        location: str,
        *,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
    ) -> MiCommand:
        """
        设置断点。

        示例：

            1-break-insert "main.c:25"

        条件断点：

            1-break-insert -c "i == 3" "main.c:25"
        """

        self._require_value(
            location,
            "location",
        )

        arguments: list[str] = []

        if temporary:
            arguments.append("-t")

        if disabled:
            arguments.append("-d")

        if condition is not None:
            self._require_value(
                condition,
                "condition",
            )

            arguments.extend(
                [
                    "-c",
                    self._quote(condition),
                ]
            )

        arguments.append(
            self._quote(location)
        )

        return self._build(
            "-break-insert",
            arguments,
        )

    def break_delete(
        self,
        breakpoint_number: str,
    ) -> MiCommand:
        self._require_value(
            breakpoint_number,
            "breakpoint_number",
        )

        return self._build(
            "-break-delete",
            [
                breakpoint_number,
            ],
        )

    def break_enable(
        self,
        breakpoint_number: str,
    ) -> MiCommand:
        self._require_value(
            breakpoint_number,
            "breakpoint_number",
        )

        return self._build(
            "-break-enable",
            [
                breakpoint_number,
            ],
        )

    def break_disable(
        self,
        breakpoint_number: str,
    ) -> MiCommand:
        self._require_value(
            breakpoint_number,
            "breakpoint_number",
        )

        return self._build(
            "-break-disable",
            [
                breakpoint_number,
            ],
        )

    def exec_arguments(
        self,
        arguments: list[str],
    ) -> MiCommand:
        """
        设置被调试程序参数。

        例如：

            1-exec-arguments "--name" "hello world"
        """

        return self._build(
            "-exec-arguments",
            [
                self._quote(argument)
                for argument in arguments
            ],
        )

    def exec_run(
        self,
    ) -> MiCommand:
        return self._build(
            "-exec-run"
        )

    def exec_continue(
        self,
    ) -> MiCommand:
        return self._build(
            "-exec-continue"
        )

    def exec_next(
        self,
    ) -> MiCommand:
        return self._build(
            "-exec-next"
        )

    def exec_step(
        self,
    ) -> MiCommand:
        return self._build(
            "-exec-step"
        )

    def exec_interrupt(
        self,
    ) -> MiCommand:
        return self._build(
            "-exec-interrupt"
        )

    def stack_list_frames(
        self,
    ) -> MiCommand:
        return self._build(
            "-stack-list-frames"
        )

    def data_evaluate_expression(
        self,
        expression: str,
    ) -> MiCommand:
        self._require_value(
            expression,
            "expression",
        )

        return self._build(
            "-data-evaluate-expression",
            [
                self._quote(expression),
            ],
        )

    def gdb_exit(
        self,
    ) -> MiCommand:
        return self._build(
            "-gdb-exit"
        )

    def _build(
        self,
        operation: str,
        arguments: list[str] | None = None,
    ) -> MiCommand:
        token = self._next_token

        self._next_token += 1

        text = (
            f"{token}{operation}"
        )

        if arguments:
            text += (
                " "
                + " ".join(arguments)
            )

        return MiCommand(
            token=token,
            text=text,
        )

    @staticmethod
    def _quote(
        value: str,
    ) -> str:
        """
        GDB/MI 使用 C 风格字符串。
        """

        escaped = (
            value
            .replace(
                "\\",
                "\\\\",
            )
            .replace(
                '"',
                '\\"',
            )
            .replace(
                "\n",
                "\\n",
            )
            .replace(
                "\r",
                "\\r",
            )
            .replace(
                "\t",
                "\\t",
            )
        )

        return (
            f'"{escaped}"'
        )

    @staticmethod
    def _require_value(
        value: str,
        name: str,
    ) -> None:
        if not value:
            raise ValueError(
                f"{name} must not be empty"
            )