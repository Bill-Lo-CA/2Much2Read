import io

import pytest

from two_read_runtime import progress


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_elapsed_progress_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = TtyBuffer()
    monkeypatch.setattr(progress.sys, "stderr", stderr)

    def fail() -> None:
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError, match="failed"):
        progress.run_with_elapsed("job", fail, interval=0.001)

    assert "job failed in" in stderr.getvalue()
