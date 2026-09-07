from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from oslab.gpu_lease import GpuLease, default_gpu_lease_path


@asynccontextmanager
async def gpu_generation_lease(
    *, enabled: bool = True, timeout: float = 60.0
) -> AsyncIterator[None]:
    """Serialize inference across processes without blocking the event loop.

    Cancellation waits for a pending filesystem acquisition to finish and then
    releases it. This prevents an abandoned background thread from acquiring a
    lease after its coroutine has already gone away.
    """
    if not enabled:
        yield
        return
    if timeout < 0:
        raise ValueError("GPU generation lease timeout must be non-negative")

    lease = GpuLease(default_gpu_lease_path(), timeout=timeout)
    abandoned = threading.Event()

    def acquire_guarded() -> None:
        lease.acquire()
        if abandoned.is_set():
            lease.release()

    acquire_task = asyncio.create_task(asyncio.to_thread(acquire_guarded))
    try:
        await asyncio.shield(acquire_task)
    except BaseException:
        abandoned.set()
        # Usually the acquisition is already close to completing. Waiting here
        # keeps cancellation deterministic; the guarded worker/callback remain
        # as a final safety net if loop shutdown delivers another cancellation.
        with contextlib.suppress(BaseException):
            await asyncio.shield(acquire_task)
        if acquire_task.done():
            with contextlib.suppress(Exception):
                acquire_task.result()
            await asyncio.to_thread(lease.release)
        else:
            # The worker thread owns final cleanup if the caller's event loop
            # closes before acquisition completes.
            acquire_task.add_done_callback(lambda _task: lease.release())
        raise
    try:
        yield
    finally:
        await asyncio.to_thread(lease.release)
