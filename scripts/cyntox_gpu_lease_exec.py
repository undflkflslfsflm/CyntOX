from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

from oslab.gpu_lease import GpuLease, default_gpu_lease_path

DEFAULT_TIMEOUT_SECONDS = 1800.0


def _timeout_seconds() -> float:
    raw = os.environ.get("CYNTOX_GPU_LEASE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if value < 0 or value != value or value == float("inf"):
        return DEFAULT_TIMEOUT_SECONDS
    return min(value, DEFAULT_TIMEOUT_SECONDS)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "--" or len(arguments) == 1:
        print("usage: cyntox_gpu_lease_exec.py -- <command> [args ...]", file=sys.stderr)
        return 64

    lease = GpuLease(default_gpu_lease_path(), timeout=_timeout_seconds())
    try:
        lease.acquire()
    except TimeoutError:
        print(
            "CyntOX GPU remained busy; the direct generation process was not started.",
            file=sys.stderr,
        )
        return 75
    try:
        try:
            process = subprocess.Popen(arguments[1:])  # noqa: S603 - argv comes from launcher
        except OSError as error:
            print(f"Could not start the direct CyntOX process: {error}", file=sys.stderr)
            return 126
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                return process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                return 130
    finally:
        lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
