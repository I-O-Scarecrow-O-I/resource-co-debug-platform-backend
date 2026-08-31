import asyncio
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


class ProcessRunner:
    async def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_seconds: int,
        on_log: LogCallback,
        is_cancelled: CancelCheck,
        on_process_started: ProcessStartedCallback | None = None,
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
        finally:
            await asyncio.gather(*readers, return_exceptions=True)

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return ProcessResult(exit_code=exit_code, elapsed_ms=elapsed_ms)

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
