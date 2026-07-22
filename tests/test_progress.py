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


def test_live_progress_keeps_timer_above_terminal_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = TtyBuffer()
    monkeypatch.setattr(progress.sys, "stderr", stderr)

    result = progress.run_with_live_progress("job", lambda report: (report("extracting newsletter"), "done")[1])

    assert result == "done"
    assert "\033[2J" in stderr.getvalue()
    assert "\033[2;" in stderr.getvalue()
    assert "extracting newsletter" in stderr.getvalue()
    assert "job finished in" in stderr.getvalue()
    assert "\033[r" in stderr.getvalue()


def test_live_progress_is_silent_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = io.StringIO()
    monkeypatch.setattr(progress.sys, "stderr", stderr)
    messages: list[str] = []

    def operation(report: progress.StatusReporter) -> str:
        report("not shown")
        messages.append("finished")
        return "done"

    assert progress.run_with_live_progress("job", operation) == "done"
    assert messages == ["finished"]
    assert stderr.getvalue() == ""
