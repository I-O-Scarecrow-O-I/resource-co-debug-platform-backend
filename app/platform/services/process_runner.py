import asyncio
import ctypes
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import CancellationRequested

LogCallback = Callable[[str, str], None]
CancelCheck = Callable[[], bool]
ProcessStartedCallback = Callable[[asyncio.subprocess.Process], None]


@dataclass(slots=True)
class ProcessResult:
    exit_code: int
    elapsed_ms: int
    affinity_applied: bool = False


class ProcessRunner:
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
        try:
            exit_code = await self._wait_with_control(
                process,
                wait_task,
                timeout_seconds,
                is_cancelled,
                process_group,
            )
        except asyncio.CancelledError:
            await self._terminate_process_group(process, process_group)
            raise
        finally:
            await self._cleanup_process_group(process, process_group)
            await self._drain_readers(readers)
            self._close_process_group(process_group)

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return ProcessResult(
            exit_code=exit_code,
            elapsed_ms=elapsed_ms,
            affinity_applied=affinity_applied,
        )

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

