import asyncio
import os
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
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
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
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            await asyncio.gather(*readers, return_exceptions=True)

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
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if is_cancelled():
                process.kill()
                await process.wait()
                raise CancellationRequested()

            done, _ = await asyncio.wait({asyncio.ensure_future(wait_task)}, timeout=0.2)
            if done:
                return done.pop().result()

            if time.monotonic() >= deadline:
                process.kill()
                await process.wait()
                raise TimeoutError(f"command timed out after {timeout_seconds} seconds")

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

