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

    # token 从正整数开始，避免无编号异步记录与命令等待表混用。
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

        # 位置不能为空；实际符号与源码解析由 GDB 完成。
        self._require_value(
            location,
            "location",
        )

        # 固定选项与用户提供的位置或条件分开；用户文本先编码为 MI 字符串。
        arguments: list[str] = []

        if temporary:
            arguments.append("-t")

        if disabled:
            arguments.append("-d")

        # 条件是一个 MI 字符串参数，空格和引号不应拆成其他选项。
        # 未传条件时不发送条件选项，空条件不能伪装成一个有效表达式。
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

        # 位置也按单个参数编码，以支持含空格的源码路径。
        # 位置放在选项之后；引号让含空格的源路径仍作为同一个参数。
        arguments.append(
            self._quote(location)
        )

        return self._build(
            "-break-insert",
            arguments,
        )

    # 这些断点更新命令使用 GDB 返回的编号，不用源码行号代替。
    def break_delete(
        self,
        breakpoint_number: str,
    ) -> MiCommand:
        # 后续断点命令复用 GDB 返回的编号，而不再用源位置匹配。
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

    # 逐个引用 inferior 参数，保留带空格的值与参数边界。
    def exec_arguments(
        self,
        arguments: list[str],
    ) -> MiCommand:
        """
        设置被调试程序参数。

        例如：

            1-exec-arguments "--name" "hello world"
        """

        # 每个 argv 单独编码，保留空参数及包含空格的参数边界。
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

    # next 跨过函数调用，step 可以进入；二者由不同的 MI 操作表达。
    # next 在调用处越过函数调用；需要进入被调函数时使用 step。
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

    # 中断只构造发送请求；目标是否暂停需要另外观察会话事件。
    # 发送控制请求而不是等待现场，暂停通知由 MI 读取任务处理。
    def exec_interrupt(
        self,
    ) -> MiCommand:
        return self._build(
            "-exec-interrupt"
        )

    # 打开异步模式后，运行中仍可经 MI 发送中断。
    # 异步控制允许目标运行期间发送 MI 请求；它与全停配置不冲突。
    def gdb_set_mi_async(
        self,
    ) -> MiCommand:
        return self._build(
            "-gdb-set",
            ["mi-async", "on"],
        )

    # 设置全停模式，让后续状态处理可按会话解释暂停。
    # 禁止 non-stop 后，线程停止语义以同一 inferior 的全停模式为准。
    def gdb_set_all_stop(
        self,
    ) -> MiCommand:
        return self._build(
            "-gdb-set",
            ["non-stop", "off"],
        )

    def stack_list_frames(
        self,
    ) -> MiCommand:
        return self._build(
            "-stack-list-frames"
        )

    # 只要求简单变量的值，避免为大型复合对象展开额外输出。
    # 请求 simple values，复杂变量不要求在此接口展开整个对象。
    def stack_list_variables(
        self,
    ) -> MiCommand:
        return self._build(
            "-stack-list-variables",
            ["--simple-values"],
        )

    def thread_info(
        self,
    ) -> MiCommand:
        return self._build(
            "-thread-info"
        )

    # 线程选择接收的是 GDB 线程 ID，与系统线程号分开。
    # 选择的是 GDB 内部线程编号，系统 TID 不能替代。
    def thread_select(
        self,
        thread_id: str,
    ) -> MiCommand:
        self._require_value(
            thread_id,
            "thread_id",
        )
        return self._build(
            "-thread-select",
            [thread_id],
        )

    # 表达式作为一段 MI 文本发送，求值语义属于调试器当前上下文。
    # 表达式按单个 MI 参数编码，由 GDB 在当前帧上下文求值。
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

    # 构造结果同时提供线上命令文本与数值 token，供发送方匹配响应。
    def _build(
        self,
        operation: str,
        arguments: list[str] | None = None,
    ) -> MiCommand:
        # 在构造时领取单调编号，使响应可与这个会话中的具体命令配对。
        # 编号不作为平台任务标识。
        token = self._next_token

        self._next_token += 1

        text = (
            f"{token}{operation}"
        )

        # 调用者在传入参数前处理各参数的引用语义，此处只组装命令。
        # 可选参数已按各自语法组装，此处只连接 MI 命令字段，不走 shell。
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

        # 先处理反斜杠，再编码引号和控制字符，避免新产生的转义被二次处理。
        # 先处理原有反斜杠，再编码引号和控制字符。
        # 这一顺序避免把新生成的转义再转义一次。
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
    # 这里仅拒绝空值；编号存在性及运行状态由会话或调试器确认。
    def _require_value(
        value: str,
        name: str,
    ) -> None:
        # 这里只拒绝空值；字段语法和适用状态由上层或 GDB 校验。
        if not value:
            raise ValueError(
                f"{name} must not be empty"
            )
