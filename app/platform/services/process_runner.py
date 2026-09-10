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
INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES = 1024 * 1024
_CREATE_SUSPENDED = 0x00000004


@dataclass(slots=True)
class ProcessResult:
    exit_code: int
    elapsed_ms: int
    affinity_applied: bool = False


class _ProcessRunnerInteractiveSession:
    def __init__(
        self,
        runner: "ProcessRunner",
        process: asyncio.subprocess.Process,
        process_group: int | None,
        readers: list[asyncio.Task[None]],
    ) -> None:
        self._runner = runner
        self._process = process
        self._process_group = process_group
        self._readers = readers
        self._write_lock = asyncio.Lock()
        self._termination_lock = asyncio.Lock()
        self._termination_task: asyncio.Task[None] | None = None
        self._process_wait_task = asyncio.create_task(process.wait())
        self._completion_task = asyncio.create_task(self._wait_and_cleanup())

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def write(self, data: str) -> None:
        if not isinstance(data, str):
            raise TypeError("interactive process input must be a string")
        async with self._write_lock:
            stdin = self._process.stdin
            if (
                stdin is None
                or stdin.is_closing()
                or self._process.returncode is not None
            ):
                raise RuntimeError("interactive process stdin is closed")
            stdin.write(data.encode())
            await stdin.drain()

    async def wait(self) -> int:
        return await asyncio.shield(self._completion_task)

    async def terminate(self) -> None:
        async with self._termination_lock:
            if self._termination_task is None:
                self._termination_task = asyncio.create_task(self._terminate_and_wait())
            termination_task = self._termination_task

        cancelled = False
        while not termination_task.done():
            try:
                await asyncio.shield(termination_task)
            except asyncio.CancelledError:
                cancelled = True
        termination_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _terminate_and_wait(self) -> None:
        if self._process.returncode is None:
            await self._runner._terminate_process_group(
                self._process,
                self._process_group,
            )
        await asyncio.shield(self._completion_task)

    async def _wait_and_cleanup(self) -> int:
        exit_code: int | None = None
        failure: BaseException | None = None
        failure_traceback: TracebackType | None = None
        try:
            exit_code = await self._wait_for_process_and_output()
        except BaseException as exc:
            failure = exc
            failure_traceback = exc.__traceback__

        stdin = self._process.stdin
        if stdin is not None:
            stdin.close()
        cleanup_cancelled = False
        cleanup_failure: BaseException | None = None
        try:
            cleanup_cancelled = await self._runner._finish_process_cleanup(
                self._process,
                self._process_group,
                self._readers,
            )
        except BaseException as exc:
            cleanup_failure = exc
        await asyncio.gather(self._process_wait_task, return_exceptions=True)
        if stdin is not None:
            try:
                await stdin.wait_closed()
            except OSError:
                pass

        if failure is not None:
            if cleanup_failure is not None:
                failure.add_note(f"process cleanup failed: {cleanup_failure!r}")
            raise failure.with_traceback(failure_traceback)
        if cleanup_failure is not None:
            raise cleanup_failure
        if cleanup_cancelled:
            raise asyncio.CancelledError
        assert exit_code is not None
        return exit_code

    async def _wait_for_process_and_output(self) -> int:
        active_readers = set(self._readers)
        while not self._process_wait_task.done():
            done, _ = await asyncio.wait(
                {self._process_wait_task, *active_readers},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for reader in done.intersection(active_readers):
                active_readers.remove(reader)
                self._raise_output_failure(reader)

        await asyncio.gather(*self._readers, return_exceptions=True)
        for reader in self._readers:
            self._raise_output_failure(reader)
        return self._process_wait_task.result()

    @staticmethod
    def _raise_output_failure(reader: asyncio.Task[None]) -> None:
        try:
            reader.result()
        except asyncio.CancelledError as exc:
            raise AppError("interactive process output reader was cancelled") from exc
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                f"interactive process output handling failed: {exc}"
            ) from exc


class ProcessRunner:
    async def open_interactive_process(
        self,
        command: list[str],
        cwd: Path,
        on_output: LogCallback,
    ) -> InteractiveProcessSession:
        if not command:
            raise ValueError("command must not be empty")
        if not cwd.is_dir():
            raise ValueError("cwd must be an existing directory")

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
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES,
                **process_options,
            )
            process_group = self._create_process_group(process.pid)
            if os.name == "nt":
                if process_group is None:
                    raise AppError(
                        "failed to assign interactive process to Windows job object"
                    )
                self._resume_windows_process(process.pid)
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
            return _ProcessRunnerInteractiveSession(
                self,
                process,
                process_group,
                readers,
            )
        except BaseException as exc:
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
        if not command:
            raise ValueError("command must not be empty")

        started = time.perf_counter()
        process_options = (
            {"start_new_session": True}
            if os.name == "posix"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        process: asyncio.subprocess.Process | None = None
        process_group: int | None = None
        readers: list[asyncio.Task[None]] = []
        exit_code: int | None = None
        affinity_applied = False
        failure: BaseException | None = None
        failure_traceback: TracebackType | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
            process_group = self._create_process_group(process.pid)
            if on_process_started:
                on_process_started(process)
            affinity_applied = self._apply_cpu_affinity(process.pid, cpu_core, on_log)

            readers = [
                asyncio.create_task(self._stream_output(process.stdout, "stdout", on_log)),
                asyncio.create_task(self._stream_output(process.stderr, "stderr", on_log)),
            ]
            wait_task = asyncio.create_task(process.wait())
            exit_code = await self._wait_with_control(
                process,
                wait_task,
                timeout_seconds,
                is_cancelled,
                process_group,
            )
        except BaseException as exc:
            failure = exc
            failure_traceback = exc.__traceback__

        cleanup_cancelled = False
        cleanup_failure: BaseException | None = None
        if process is not None:
            try:
                cleanup_cancelled = await self._finish_process_cleanup(
                    process,
                    process_group,
                    readers,
                )
            except BaseException as exc:
                cleanup_failure = exc

        if failure is not None:
            if cleanup_failure is not None:
                failure.add_note(f"process cleanup failed: {cleanup_failure!r}")
            raise failure.with_traceback(failure_traceback)
        if cleanup_failure is not None:
            raise cleanup_failure
        if cleanup_cancelled:
            raise asyncio.CancelledError

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        assert exit_code is not None
        return ProcessResult(
            exit_code=exit_code,
            elapsed_ms=elapsed_ms,
            affinity_applied=affinity_applied,
        )

    async def _finish_process_cleanup(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
        readers: list[asyncio.Task[None]],
    ) -> bool:
        cleanup_task = asyncio.create_task(
            self._cleanup_process(process, process_group, readers)
        )
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
        return cancelled

    async def _cleanup_process(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
        readers: list[asyncio.Task[None]],
    ) -> None:
        try:
            if process.returncode is None:
                await self._terminate_process_group(process, process_group)
            else:
                await self._cleanup_process_group(process, process_group)
        finally:
            try:
                await self._drain_readers(readers)
            finally:
                self._close_process_group(process_group)

    def _apply_cpu_affinity(
        self,
        process_id: int,
        cpu_core: int | None,
        on_log: LogCallback,
    ) -> bool:
        if cpu_core is None:
            return False

        set_affinity = getattr(os, "sched_setaffinity", None)
        if set_affinity is None:
            on_log(
                f"CPU affinity for core {cpu_core} is unavailable on this platform",
                "system",
            )
            return False

        try:
            set_affinity(process_id, {cpu_core})
        except OSError as exc:
            on_log(f"failed to bind process to core {cpu_core}: {exc}", "system")
            return False

        on_log(f"process bound to CPU core {cpu_core}", "system")
        return True

    async def _wait_with_control(
        self,
        process: asyncio.subprocess.Process,
        wait_task: asyncio.Task[int],
        timeout_seconds: int,
        is_cancelled: CancelCheck,
        process_group: int | None,
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if is_cancelled():
                await self._terminate_process_group(process, process_group)
                raise CancellationRequested()

            done, _ = await asyncio.wait({asyncio.ensure_future(wait_task)}, timeout=0.2)
            if done:
                return done.pop().result()

            if time.monotonic() >= deadline:
                await self._terminate_process_group(process, process_group)
                raise TimeoutError(f"command timed out after {timeout_seconds} seconds")

    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
    ) -> None:
        if process.returncode is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
        elif process_group is not None:
            self._terminate_windows_job(process_group)
        else:
            process.kill()
        await process.wait()

    async def _cleanup_process_group(
        self,
        process: asyncio.subprocess.Process,
        process_group: int | None,
    ) -> None:
        if process.returncode is None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        elif process_group is not None:
            self._terminate_windows_job(process_group)

    def _create_process_group(self, process_id: int) -> int | None:
        if os.name != "nt":
            return None

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
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None

        class LargeInteger(ctypes.Structure):
            _fields_ = [("quad_part", ctypes.c_longlong)]

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

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", BasicLimitInformation),
                ("io_info", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        limits = ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(handle)
            return None

        process_handle = kernel32.OpenProcess(0x0501, False, process_id)
        if not process_handle or not kernel32.AssignProcessToJobObject(handle, process_handle):
            if process_handle:
                kernel32.CloseHandle(process_handle)
            kernel32.CloseHandle(handle)
            return None
        kernel32.CloseHandle(process_handle)
        handle_value = handle.value if hasattr(handle, "value") else handle
        return int(handle_value)

    def _terminate_windows_job(self, process_group: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject(process_group, 1)

    def _resume_windows_process(self, process_id: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        handle_type = ctypes.c_void_p
        kernel32.OpenProcess.restype = handle_type
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [handle_type]
        ntdll.NtResumeProcess.argtypes = [handle_type]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        process_handle = kernel32.OpenProcess(0x0800, False, process_id)
        if not process_handle:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "failed to open suspended interactive process")
        try:
            status = ntdll.NtResumeProcess(process_handle)
        finally:
            kernel32.CloseHandle(process_handle)
        if status != 0:
            raise OSError(status, "failed to resume suspended interactive process")

    def _close_process_group(self, process_group: int | None) -> None:
        if process_group is not None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(process_group)

    async def _drain_readers(self, readers: list[asyncio.Task[None]]) -> None:
        try:
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=1)
        except TimeoutError:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def _stream_output(
        self,
        stream: asyncio.StreamReader | None,
        name: str,
        on_log: LogCallback,
    ) -> None:
        if stream is None:
            return
        while line := await stream.readline():
            on_log(line.decode(errors="replace").rstrip(), name)

    async def _stream_interactive_output(
        self,
        stream: asyncio.StreamReader | None,
        name: str,
        on_output: LogCallback,
    ) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except ValueError as exc:
                raise AppError(
                    "interactive process output record exceeds "
                    f"{INTERACTIVE_OUTPUT_RECORD_LIMIT_BYTES} bytes"
                ) from exc
            if not line:
                return
            on_output(line.decode(errors="replace").rstrip("\r\n"), name)

