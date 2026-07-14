from pathlib import Path

import pytest

from weflow_maibot_bridge.process_lock import ProcessLock


def test_process_lock_rejects_second_instance(tmp_path: Path) -> None:
    first = ProcessLock(tmp_path / "bridge.lock")
    second = ProcessLock(tmp_path / "bridge.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="另一个 Bridge"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
