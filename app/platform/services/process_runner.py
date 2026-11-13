import asyncio
import ctypes
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from app.core.errors import AppError, CancellationRequested
from app.platform.services.task_execution import InteractiveProcessSession

LogCallback = Callable[[str, str], None]
CancelCheck = Callable[[], bool]
ProcessStartedCallback = Callable[[asyncio.subprocess.Process], None]
# 交互输出限制针对单条协议记录，不限制会话总共可以产生多少日志。
INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES = 1024 * 1024
# Windows 交互进程在入 Job 之前保持挂起。
# 否则进程可以在平台完成托管前已经派生子进程，失去统一回收边界。
_CREATE_SUSPENDED = 0x00000004


@dataclass(slots=True)
# 退出码、墙钟耗时与亲和性实际设置分别记录，计划核心不等于 OS 已绑定。
class ProcessResult:
    exit_code: int
    elapsed_ms: int
    affinity_applied: bool = False


# 交互对象拥有输入、读管道和回收任务；模块只通过会话契约操作。
class _ProcessRunnerInteractiveSession:
    def __init__(
        self,
        runner: "ProcessRunner",
        process: asyncio.subprocess.Process,
        process_group: int | None,
        readers: list[asyncio.Task[None]],
    ) -> None:
        # 会话接管已经启动的进程、读任务和平台分组资源。
        # 调用方拿到的是这个生命周期的接口，不另行管理底层管道。
        self._runner = runner
        self._process = process
        self._process_group = process_group
        self._readers = readers
        # 写锁保护输入顺序，终止锁仅保护共享终止任务的创建。
        # 关闭可以与命令写入竞争，写入路径仍需检查管道状态。
        self._write_lock = asyncio.Lock()
        # 终止锁只用于合并主动关闭请求；输入写入用另一把锁。
        # 不让等待进程退出的调用长期占住发送命令所用的锁。
        self._termination_lock = asyncio.Lock()
        self._termination_task: asyncio.Task[None] | None = None
        # 进程退出观察和清理完成是两种事件。
        # 外部 wait 等的是后者，确保输出也已经处理。
        self._process_wait_task = asyncio.create_task(process.wait())
        # 关闭与正常退出都等待同一个 completion task。
        # 它比进程 wait 多涵盖输出与句柄的收尾，不能用裸退出码替代。
        self._completion_task = asyncio.create_task(self._wait_and_cleanup())

    @property
    # 这是底层进程当时的退出码，可能早于读取和清理完成。
    # 需要资源已经释放的调用方应等待 wait。
    def returncode(self) -> int | None:
        return self._process.returncode

    # 串行写入命令文本，避免两条并发协议命令在管道中交织。
    async def write(self, data: str) -> None:
        # 拒绝不属于协议接口的字节或对象，不隐式做文本格式化。
        # 结束符由调用方的交互协议决定，写入入口不自动补行。
        if not isinstance(data, str):
            raise TypeError("interactive process input must be a string")
        async with self._write_lock:
            # 取管道和检查其状态都在写锁内；这是发送前的有效性检查。
            # 对端仍可能在 drain 时退出，因此发送不能保证成功。
            stdin = self._process.stdin
            # 进程可能在上一条命令后退出；在真正写入前重新检查管道状态。
            if (
                stdin is None
                or stdin.is_closing()
                or self._process.returncode is not None
            ):
                raise RuntimeError("interactive process stdin is closed")
            # drain 提供背压，不能把把数据放入本地缓冲当作写入已经完成。
            stdin.write(data.encode())
            # drain 提供管道背压，避免把命令无限堆积在本地缓冲。
            # 返回只说明写入阶段完成，不是 GDB 的业务回执。
            await stdin.drain()

    # 屏蔽观察方取消，让等待者超时不破坏共享的进程完成任务。
    async def wait(self) -> int:
        return await asyncio.shield(self._completion_task)

    # 多个终止请求复用一个清理任务，底层进程只需要结束一次。
    async def terminate(self) -> None:
        async with self._termination_lock:
            # 第一个请求启动终止工作；后来的请求只等待同一个结果。
            if self._termination_task is None:
                self._termination_task = asyncio.create_task(self._terminate_and_wait())
            termination_task = self._termination_task

        # 清理在调用方取消时仍要继续，完成后再恢复原取消语义。
        cancelled = False
        while not termination_task.done():
            try:
                await asyncio.shield(termination_task)
            except asyncio.CancelledError:
                cancelled = True
        # 先读取真实终止结果。
        # 如果清理本身失败，不能用先前收到的取消掩盖故障。
        termination_task.result()
        if cancelled:
            raise asyncio.CancelledError

    # 终止主进程后还等待统一完成任务。
    # 返回意味着同组进程及读写管道都走过收尾。
    async def _terminate_and_wait(self) -> None:
        # 主进程仍活着时先终止进程组。
        # 已经退出时直接等待正常清理路径处理仍存活的子进程。
        if self._process.returncode is None:
            await self._runner._terminate_process_group(
                self._process,
                self._process_group,
            )
        await asyncio.shield(self._completion_task)

    # 同时观察进程和输出管道，收尾要关闭 stdin 以及任务所属进程组。
    async def _wait_and_cleanup(self) -> int:
        # 退出码与异常分开保存。
        # 即使输出处理失败，也继续关闭 stdin 并回收进程树，而不是提前退出。
        exit_code: int | None = None
        failure: BaseException | None = None
        failure_traceback: TracebackType | None = None
        # 从输出通道提前失败和正常退出走同一个回收入口；只有后者可交付有效退出码。
        try:
            exit_code = await self._wait_for_process_and_output()
        except BaseException as exc:
            failure = exc
            failure_traceback = exc.__traceback__

        # 关掉 stdin 让进程看到输入终止。
        # 等进程与管道收尾后再等待 writer 完全关闭。
        stdin = self._process.stdin
        # 关闭本端写入管道，告知子进程没有更多交互输入。
        # 后续仍要等待它的退出及读端的收尾。
        if stdin is not None:
            stdin.close()
        cleanup_cancelled = False
        cleanup_failure: BaseException | None = None
        try:
            # 资源回收可能失败，也可能只在等待过程中遇到调用方取消。
            # 这两种情况分别留到最后决定应如何报告。
            cleanup_cancelled = await self._runner._finish_process_cleanup(
                self._process,
                self._process_group,
                self._readers,
            )
        except BaseException as exc:
            cleanup_failure = exc
        # 取回退出等待任务的结果或异常，避免其生命周期脱离交互会话。
        await asyncio.gather(self._process_wait_task, return_exceptions=True)
        # 管道的关闭等待和进程等待分别完成。
        # 对端断开时的 OSError 不覆盖原来的业务错误或退出状态。
        if stdin is not None:
            try:
                # 写端可能随子进程退出先被对方断开；关闭阶段的管道错误不替换先前故障。
                await stdin.wait_closed()
            except OSError:
                pass

        # 优先传播最早的进程或读输出异常，清理异常只作为补充线索。
        if failure is not None:
            if cleanup_failure is not None:
                # 保留首个执行错误及原 traceback。
                # 附带清理故障用于诊断资源状态，不让它替换导致此次任务失败的原因。
                failure.add_note(f"process cleanup failed: {cleanup_failure!r}")
            raise failure.with_traceback(failure_traceback)
        if cleanup_failure is not None:
            raise cleanup_failure
        if cleanup_cancelled:
            raise asyncio.CancelledError
        # 只有主流程和清理均未失败时才要求存在退出码。
        # 交互输出错误不能被进程恰好零退出掩盖。
        assert exit_code is not None
        return exit_code

    # 进程还在运行时也观察 reader 失败，协议超长等问题无需等待进程自然退出。
    async def _wait_for_process_and_output(self) -> int:
        # 进程尚未退出时同时观察输出 reader。
        # 若协议输出处理先失败，不必等进程自然结束才暴露故障。
        active_readers = set(self._readers)
        while not self._process_wait_task.done():
            done, _ = await asyncio.wait(
                {self._process_wait_task, *active_readers},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 已结束的 reader 从下一轮等待集合移除。
            # 如果它因输出处理失败结束，则立即向完成任务传播。
            for reader in done.intersection(active_readers):
                active_readers.remove(reader)
                self._raise_output_failure(reader)

        # 进程已退出时仍要消费完成的输出读取结果。
        # 最后一批记录可能带来解析或输出故障，不能因退出码已存在而略过。
        await asyncio.gather(*self._readers, return_exceptions=True)
        # 退出后再确认各输出任务已经完成，避免尾部协议记录未交付。
        for reader in self._readers:
            self._raise_output_failure(reader)
        return self._process_wait_task.result()

    @staticmethod
    # 保留已经归类的平台错误，并把其他 reader 异常转换成可报告的进程错误。
    def _raise_output_failure(reader: asyncio.Task[None]) -> None:
        try:
            reader.result()
        # reader 的非预期取消属于输出失败。
        # 这与调用者主动取消某一次 wait 不同。
        except asyncio.CancelledError as exc:
            raise AppError("interactive process output reader was cancelled") from exc
        # 已有 AppError 保留原错误类别。
        # 未知输出处理异常才统一成进程输出错误，避免调用方误作底层正常退出。
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                f"interactive process output handling failed: {exc}"
            ) from exc


class ProcessRunner:
    # 交互模式保留三路管道，生命周期由返回的会话对象统一维护。
    async def open_interactive_process(
        self,
        command: list[str],
        cwd: Path,
        on_output: LogCallback,
    ) -> InteractiveProcessSession:
        # 输入检查发生在取得系统资源之前，非法命令不会创建部分会话。
        if not command:
            raise ValueError("command must not be empty")
        # 交互进程启动前要求 cwd 已存在，避免把目录准备失败混入调试协议握手。
        if not cwd.is_dir():
            raise ValueError("cwd must be an existing directory")

        # POSIX 为进程树建立独立会话；Windows 先暂停子进程以便设置 Job Object。
        process_options = (
            {"start_new_session": True}
            if os.name == "posix"
            else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | _CREATE_SUSPENDED
            }
        )
        process: asyncio.subprocess.Process | None = None
        process_group: int | None = None
        readers: list[asyncio.Task[None]] = []
        # 从拿到进程对象到成功构建会话之间，资源仍由启动函数负责。
        # 发生错误或取消时先尝试回收，不返回半初始化会话。
        try:
            # 在工作目录中直接启动 argv 指定的程序，使用独立管道接收协议和诊断。
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES,
                **process_options,
            )
            # Windows 交互进程必须先成功纳入 Job，再恢复执行。
            # 否则 GDB 可能先启动尚未受控的被调试进程。
            # POSIX 分组由新会话建立，Windows 则需要拿到 Job 句柄。
            # 两个平台下的分组身份不能按同一种值解释。
            process_group = self._create_process_group(process.pid)
            # 交互路径拿不到 Windows Job 句柄时不继续运行。
            # 普通单次命令仍有主进程终止回退，二者的启动策略不同。
            if os.name == "nt":
                if process_group is None:
                    # 挂起状态下注册失败就进入清理，不能先恢复再靠进程组补救。
                    raise AppError(
                        "failed to assign interactive process to Windows job object"
                    )
                self._resume_windows_process(process.pid)
            # 标准输出和标准错误各自消费，以免一条管道填满阻塞调试协议。
            # 两个输出通道分别启动读取任务。
            # 这样阻塞在某一个通道的读取不会令另一个管道无人消费。
            readers = [
                asyncio.create_task(
                    self._stream_interactive_output(
                        process.stdout,
                        "stdout",
                        on_output,
                    )
                ),
                asyncio.create_task(
                    self._stream_interactive_output(
                        process.stderr,
                        "stderr",
                        on_output,
                    )
                ),
            ]
            # 初始化完成后才把进程、组和 readers 的所有权交给会话。
            return _ProcessRunnerInteractiveSession(
                self,
                process,
                process_group,
                readers,
            )
        # 部分初始化失败也可能已经有子进程；用通用清理逻辑释放已建立的资源。
        # 只清理已经拿到句柄的进程。
        # 清理异常附到原错误上，保留初始化失败发生的位置。
        except BaseException as exc:
            # 若连进程对象都没有创建成功，就没有由本次调用拥有的进程可回收。
            if process is not None:
                try:
                    await self._finish_process_cleanup(
                        process,
                        process_group,
                        readers,
                    )
                except BaseException as cleanup_error:
                    exc.add_note(f"process cleanup failed: {cleanup_error!r}")
            raise

    # 单次执行按 argv 启动，不经过 shell；平台调用者负责校验命令来源。
    # 本入口的 stdout/stderr 走日志接口；需要发命令或消费精确协议记录的调用者应打开交互会话。
    async def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int,
        on_log: LogCallback,
        is_cancelled: CancelCheck,
        on_process_started: ProcessStartedCallback | None = None,
        cpu_core: int | None = None,
    ) -> ProcessResult:
        # 命令已由上层准备成 argv。
        # 这里不通过 shell 解释空格、重定向或管道，也不接受空的程序位置。
        if not command:
            raise ValueError("command must not be empty")

        # 墙钟计时从启动前开始，因此包含启动和回收的开销。
        started = time.perf_counter()
        # 普通非交互进程不需要挂起再恢复。
        # 平台支持时仍尽量为它建立可回收的进程树范围。
        process_options = (
            {"start_new_session": True}
            if os.name == "posix"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        process: asyncio.subprocess.Process | None = None
        process_group: int | None = None
        readers: list[asyncio.Task[None]] = []
        # 未取得真实退出码的路径不生成正常 ProcessResult。
        # 超时、取消与启动异常走相同清理入口，但保留各自的错误。
        exit_code: int | None = None
        affinity_applied = False
        failure: BaseException | None = None
        failure_traceback: TracebackType | None = None
        # 运行失败和资源清理分别记录；从启动完成后任何步骤出错都需要收尾。
        try:
            # 日志读取为 stdout 与 stderr 各留管道。
            # 调用方传入的日志回调使用通道名区分交错输出。
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
            # 先登记可用于回收的分组，再调用上层启动钩子。
            # 钩子失败时仍由当前方法回收已存在的进程。
            process_group = self._create_process_group(process.pid)
            # 先通知启动控制器已取得句柄，等待中的取消可以继续安全处理。
            if on_process_started:
                on_process_started(process)
            # 亲和性是可选执行能力，无法设置时仍可运行并明确返回未生效。
            affinity_applied = self._apply_cpu_affinity(process.pid, cpu_core, on_log)

            # 并行读取两路管道并将流名交给日志回调；退出等待不负责消费输出。
            readers = [
                asyncio.create_task(self._stream_output(process.stdout, "stdout", on_log)),
                asyncio.create_task(self._stream_output(process.stderr, "stderr", on_log)),
            ]
            # 退出等待独立于 stdout 和 stderr，控制循环只观察进程与取消预算。
            wait_task = asyncio.create_task(process.wait())
            # wait_task 仅表示主进程退出；控制循环也要周期性检查任务取消和截止时刻。
            # 输出与剩余分组资源由随后阶段处理。
            exit_code = await self._wait_with_control(
                process,
                wait_task,
                timeout_seconds,
                is_cancelled,
                process_group,
            )
        # 暂存执行异常及栈，再统一回收进程资源，避免异常路径跳过清理。
        except BaseException as exc:
            failure = exc
            failure_traceback = exc.__traceback__

        # 无论是否得到退出码，只要创建过进程就进入清理。
        # 成功退出的命令也可能遗留持有管道的后代。
        cleanup_cancelled = False
        cleanup_failure: BaseException | None = None
        # 即使进程已退出也要清理同组进程和读管道。
        if process is not None:
            try:
                cleanup_cancelled = await self._finish_process_cleanup(
                    process,
                    process_group,
                    readers,
                )
            # 清理异常单独存放，允许执行错误与清理诊断同时存在。
            except BaseException as exc:
                cleanup_failure = exc

        # 原始错误优先；没有原始错误时才传播清理失败或清理期间收到的取消。
        if failure is not None:
            if cleanup_failure is not None:
                failure.add_note(f"process cleanup failed: {cleanup_failure!r}")
            raise failure.with_traceback(failure_traceback)
        # 若此前执行成功，清理错误就是本次调用的失败原因。
        # 不能在资源回收不完整时仍发布普通成功结果。
        if cleanup_failure is not None:
            raise cleanup_failure
        if cleanup_cancelled:
            raise asyncio.CancelledError

        # 耗时在清理后取值，不等同于被调试程序自身的计时。
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        assert exit_code is not None
        return ProcessResult(
            exit_code=exit_code,
            elapsed_ms=elapsed_ms,
            affinity_applied=affinity_applied,
        )

    # 将清理放在独立任务内，以免取消执行协程时同时取消回收逻辑。
    async def _finish_process_cleanup(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
        readers: list[asyncio.Task[None]],
    ) -> bool:
        # 清理放到独立任务，保留其明确完成信号。
        # 外层可以记录取消请求，但不能把同一个取消直接传给清理任务。
        cleanup_task = asyncio.create_task(
            self._cleanup_process(process, process_group, readers)
        )
        cancelled = False
        # 多次外部取消只改变最终返回语义；不会打断子任务释放资源。
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
        return cancelled

    # 根据主进程状态决定终止或仅收尾；无论结果都处理管道和组句柄。
    async def _cleanup_process(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
        readers: list[asyncio.Task[None]],
    ) -> None:
        # 主进程已结束与未结束分别处理。
        # 后一种还需等待退出；前一种重点检查独立分组里的遗留成员。
        try:
            # 仍运行的主进程需要终止后等待。
            # 主进程先退出时改按其组清理遗留成员。
            if process.returncode is None:
                await self._terminate_process_group(process, process_group)
            else:
                await self._cleanup_process_group(process, process_group)
        finally:
            # 外层组清理即使抛异常也要收束 reader；句柄释放还受下一层 finally 保护。
            try:
                await self._drain_readers(readers)
            finally:
                # 即使排空输出超时或抛错，也要关闭 Job 句柄。
                # 该句柄是 Windows 进程组资源的最后一个所有权边界。
                self._close_process_group(process_group)

    # 只在支持设置亲和性的系统尝试绑核，日志提示与返回值反映实际能力。
    def _apply_cpu_affinity(
        self,
        process_id: int,
        cpu_core: int | None,
        on_log: LogCallback,
    ) -> bool:
        if cpu_core is None:
            return False

        # 没有亲和性接口时给出可定位系统日志；不制造虚假的绑核成功。
        set_affinity = getattr(os, "sched_setaffinity", None)
        if set_affinity is None:
            on_log(
                f"CPU affinity for core {cpu_core} is unavailable on this platform",
                "system",
            )
            return False

        # 计划的核心选择只是绑核请求，设置失败仍可继续测量命令。
        # 返回值区分实际绑定和仅逻辑分配，比较报告不能用核心号替代它。
        try:
            set_affinity(process_id, {cpu_core})
        # 即便核心列表此前通过验证，系统仍可能拒绝设置。
        # 调度结果保留实际失败，不阻止该任务执行。
        except OSError as exc:
            on_log(f"failed to bind process to core {cpu_core}: {exc}", "system")
            return False

        on_log(f"process bound to CPU core {cpu_core}", "system")
        return True

    # 复用同一个退出等待任务并定期检查平台取消，用单调时钟计算截止。
    async def _wait_with_control(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int],
        timeout_seconds: int,
        is_cancelled: CancelCheck,
        process_group: int | None,
    ) -> int:
        # 截止时间使用单调时钟，系统时间调整不会改变超时判断。
        # 这个计时从控制循环开始，与整个运行结果的计时起点不同。
        deadline = time.monotonic() + timeout_seconds
        # 等待分片使外部取消可及时被观察。
        # 本轮已拿到进程退出结果时直接返回该结果。
        while True:
            # 收到取消先回收进程树再抛统一取消异常，避免终态先于资源回收。
            if is_cancelled():
                await self._terminate_process_group(process, process_group)
                raise CancellationRequested()

            # 短等待不取消主进程等待任务，下一轮继续观察同一对象。
            # 这样取消检查不会为每次轮询新建一个 OS 等待者。
            done, _ = await asyncio.wait({asyncio.ensure_future(wait_task)}, timeout=0.2)
            if done:
                # 观察到进程已结束就使用该次退出结果，不再把同一轮判断为超时。
                return done.pop().result()

            # 超时也结束进程组；不能仅停止等待而留下继续运行的命令。
            if time.monotonic() >= deadline:
                await self._terminate_process_group(process, process_group)
                raise TimeoutError(f"command timed out after {timeout_seconds} seconds")

    # 优先按系统组语义终止子树；没有组句柄时回退到主进程。
    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
    ) -> None:
        # 主进程已退出时由后续组清理分支处理，不再次等待同一个终止动作。
        if process.returncode is not None:
            return
        # POSIX 优先处理新会话中的进程组。
        # 只有组信号因一般系统错误失败时才退回杀主进程，不能据此承诺所有后代已退出。
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            # POSIX 进程组可能已经结束；查不到组不是新的任务错误。
            # 其他组信号错误改为尽力终止主进程。
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
        # 存在 Job 时对整组下发终止；只有缺少分组句柄时才退回主进程。
        # 交互启动在更早阶段会拒绝这个缺少 Job 的情况。
        elif process_group is not None:
            self._terminate_windows_job(process_group)
        else:
            process.kill()
        # 发出终止请求后仍需等待主进程确认退出。
        # 只有发送信号成功不能作为工作区已经可回收的证据。
        await process.wait()

    # 主进程正常退出不代表它启动的后继进程已退出，仍需尝试清理组。
    # 即使子进程继承管道后没有随主程序退出，也要在排空之前先尝试清理进程组。
    async def _cleanup_process_group(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
    ) -> None:
        # 此辅助函数处理的是主进程已经退出后的遗留成员。
        # 仍在运行的进程需要使用带等待的终止入口。
        if process.returncode is None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        # Windows 使用本次运行持有的 Job 句柄，不按可复用的进程编号查找其它进程。
        elif process_group is not None:
            self._terminate_windows_job(process_group)

    # Windows 使用 Job Object 管理子进程树；POSIX 在启动时已经创建会话。
    def _create_process_group(self, process_id: int) -> int | None:
        # 这个分组辅助只建立 Windows Job。
        # POSIX 直接按新进程会话的 PID 发送组信号，不依赖一个 Windows 风格句柄。
        if os.name != "nt":
            return None

        # 原生句柄按指针宽度声明，避免在 64 位进程中把句柄截成普通整数。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle_type = ctypes.c_void_p
        kernel32.CreateJobObjectW.restype = handle_type
        kernel32.OpenProcess.restype = handle_type
        kernel32.CloseHandle.argtypes = [handle_type]
        kernel32.SetInformationJobObject.argtypes = [
            handle_type,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [handle_type, handle_type]
        # Job 句柄由当前方法创建。
        # 任何配置或绑定失败都要在返回 None 前关闭它，成功时才把所有权交出去。
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None

        # 这些结构映射 Win32 Job 限制的 ABI。
        # 改变字段类型或顺序会使系统解释出不同的长度与标志。
        class LargeInteger(ctypes.Structure):
            _fields_ = [("quad_part", ctypes.c_longlong)]

        # 结构体按 Windows ABI 定义，指针宽度字段使用与宿主位数匹配的 ctypes 类型。
        # 这些布局用于传给系统调用，不是项目的业务配置项。
        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", LargeInteger),
                ("per_job_user_time_limit", LargeInteger),
                ("limit_flags", ctypes.c_uint32),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", ctypes.c_uint32),
                ("affinity", ctypes.c_void_p),
                ("priority_class", ctypes.c_uint32),
                ("scheduling_class", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [("values", ctypes.c_ulonglong * 6)]

        # 扩展结构包含系统需要的基础限制、I/O 和内存字段。
        # 即使只设置一个标志，也要传入完整结构及准确大小。
        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", BasicLimitInformation),
                ("io_info", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        # 打开关闭即终止的 Job 限制，防止遗留进程继续占用工作区。
        limits = ExtendedLimitInformation()
        # 最后一个 Job 句柄关闭时触发组回收，作为主动终止之外的资源保障。
        limits.basic_limit_information.limit_flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 系统未接受回收限制时关闭新建句柄。
        # 不把未正确配置的 Job 交给后续执行流程。
        if not kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            # 无法设定所需的关闭语义就不保留该 Job。
            # 调用者可按交互和非交互模式分别处理无法托管。
            kernel32.CloseHandle(handle)
            return None

        # 把主进程加入 Job 后关闭短期进程句柄，组句柄由执行生命周期接管。
        process_handle = kernel32.OpenProcess(0x0501, False, process_id)
        if not process_handle or not kernel32.AssignProcessToJobObject(handle, process_handle):
            if process_handle:
                kernel32.CloseHandle(process_handle)
            kernel32.CloseHandle(handle)
            return None
        # 成功加入后短期进程句柄已经不再需要，只有 Job 句柄跨越运行阶段。
        kernel32.CloseHandle(process_handle)
        # 成功分组后只返回 Job 句柄。
        # 临时用于分配进程的进程句柄已经在本层释放。
        handle_value = handle.value if hasattr(handle, "value") else handle
        return int(handle_value)

    # 终止一组不等于关闭组句柄。
    # 句柄会在更外层清理路径统一释放，即使主进程已经退出。
    def _terminate_windows_job(self, process_group: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject(process_group, 1)

    # 恢复挂起的交互进程；失败视为启动失败，调用方回收已创建资源。
    def _resume_windows_process(self, process_id: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        handle_type = ctypes.c_void_p
        kernel32.OpenProcess.restype = handle_type
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [handle_type]
        ntdll.NtResumeProcess.argtypes = [handle_type]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        # 恢复进程需要单独取得操作句柄；无论恢复结果都立即释放。
        process_handle = kernel32.OpenProcess(0x0800, False, process_id)
        if not process_handle:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "failed to open suspended interactive process")
        # 恢复使用临时打开的进程句柄。
        # 不论恢复结果如何都先关闭它，再由启动调用者处理失败。
        try:
            status = ntdll.NtResumeProcess(process_handle)
        finally:
            kernel32.CloseHandle(process_handle)
        if status != 0:
            raise OSError(status, "failed to resume suspended interactive process")

    # 只释放本次运行创建的 Windows 组句柄，POSIX 不保存同类句柄。
    def _close_process_group(self, process_group: int | None) -> None:
        # 只有 Windows 的分组资源需要在这里关闭句柄。
        # POSIX 的进程组标识不是一个待释放的文件或系统句柄。
        if process_group is not None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(process_group)

    # 管道排空有单独的短预算，防止遗留写端令清理永久等待。
    async def _drain_readers(self, readers: list[asyncio.Task[None]]) -> None:
        try:
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=1)
        # 排空预算用完后取消未结束的 reader，并等它们实际退出。
        except TimeoutError:
            # 排空期限是清理的额外预算。
            # 达到期限后取消剩余读任务并等待它们结束，以免清理结束后仍向任务日志写入。
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    # 普通日志以行为单位并容忍无效编码，不把解码问题当作命令失败。
    async def _stream_output(
        self,
        stream: asyncio.StreamReader | None,
        name: str,
        on_log: LogCallback,
    ) -> None:
        # 没有管道的调用不提供日志行，避免访问空的 StreamReader。
        # 正常非交互输出按行提交，不保留末尾空白。
        if stream is None:
            return
        # 普通日志剥离行尾和尾部空白，输出通道标识仍保持独立。
        while line := await stream.readline():
            on_log(line.decode(errors="replace").rstrip(), name)

    # 交互输出保留协议所需空白，只剥离行终止符。
    async def _stream_interactive_output(
        self,
        stream: asyncio.StreamReader | None,
        name: str,
        on_output: LogCallback,
    ) -> None:
        if stream is None:
            return
        # 交互读取按协议记录逐行交付。
        # 超长行视为协议处理故障，不能静默截断后继续解释残片。
        while True:
            try:
                line = await stream.readline()
            # 单条记录超过 reader 限制时报告可定位错误，而不是交付截断的协议帧。
            except ValueError as exc:
                raise AppError(
                    "interactive process output record exceeds "
                    f"{INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES} bytes"
                ) from exc
            # EOF 是读取任务的正常结束条件。
            # 它不直接决定进程结果，退出码和资源收尾仍由会话等待路径核实。
            if not line:
                return
            on_output(line.decode(errors="replace").rstrip("\r\n"), name)

