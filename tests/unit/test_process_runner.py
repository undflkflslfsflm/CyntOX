# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from oslab import process_runner
from oslab.process_runner import SafeProcessRunner


def test_timeout_kills_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived.txt"
    child = f"import time,pathlib; time.sleep(1.0); pathlib.Path({str(marker)!r}).write_text('bad')"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "time.sleep(30)"
    )
    result = asyncio.run(
        SafeProcessRunner().run([sys.executable, "-c", parent], cwd=tmp_path, timeout=0.2)
    )
    assert result.timed_out
    time.sleep(1.2)
    assert not marker.exists()


def test_output_is_redacted(tmp_path: Path) -> None:
    code = "print('api_key=visible-secret')"
    result = asyncio.run(
        SafeProcessRunner().run([sys.executable, "-c", code], cwd=tmp_path, timeout=5)
    )
    assert "visible-secret" not in result.stdout
    assert "[REDACTED]" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows no-Job-Object fallback")
def test_timeout_reaps_pipe_inheriting_child_after_parent_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "detached-child-survived.txt"
    child = f"import time,pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('bad')"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"

    class UnavailableJob:
        def __init__(self) -> None:
            raise OSError("simulated Job Object denial")

    monkeypatch.setattr(process_runner, "_WindowsKillOnCloseJob", UnavailableJob)
    started = time.monotonic()
    result = asyncio.run(
        SafeProcessRunner().run([sys.executable, "-c", parent], cwd=tmp_path, timeout=0.2)
    )

    assert result.timed_out
    assert time.monotonic() - started < 10
    time.sleep(3.2)
    assert not marker.exists()
