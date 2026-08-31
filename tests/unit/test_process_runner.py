from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

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
