import io
import sys
from collections.abc import Callable

import pytest

from two_read_runtime import progress
from two_read_runtime.logs import configure_logging


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_elapsed_progress_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = TtyBuffer()
    monkeypatch.setattr("two_read_runtime.progress.sys.stderr", stderr)

    def fail() -> None:
        raise RuntimeError("failed")

    with pytest.raises(RuntimeError, match="failed"):
        progress.run_with_elapsed("job", fail, interval=0.001)

    assert "job failed in" in stderr.getvalue()


def test_live_progress_keeps_timer_above_terminal_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = TtyBuffer()
    monkeypatch.setattr("two_read_runtime.progress.sys.stderr", stderr)

    def job(report: Callable[[str], None]) -> str:
        report("extracting newsletter")
        return "done"

    result = progress.run_with_live_progress("job", job)

    assert result == "done"
    assert "\033[2J" in stderr.getvalue()
    assert "\033[2;" in stderr.getvalue()
    assert "extracting newsletter" in stderr.getvalue()
    assert "job finished in" in stderr.getvalue()
    assert "\r\033[Kjob finished in" in stderr.getvalue()
    assert "\033[r" in stderr.getvalue()


def test_live_progress_neutralizes_control_characters_in_untrusted_status_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # Status lines carry Gmail subjects and Hacker News titles verbatim. U+009B is the C1 CSI and
    # most terminals act on it exactly as they act on ESC [, so a title alone could clear the screen
    # and repaint it. Both introducers have to be inert, and the words around them still readable.
    stderr = TtyBuffer()
    monkeypatch.setattr("two_read_runtime.progress.sys.stderr", stderr)
    title = "hn-1: resolving \x9b2J\x1b[31mtake over the terminal"

    progress.run_with_live_progress("job", lambda report: report(title))

    output = stderr.getvalue()
    assert "\x9b" not in output
    assert "\x1b[31m" not in output
    assert "hn-1: resolving  2J [31mtake over the terminal" in output


FORGED = "hn-1: resolving\nWARNING two_much_two_read: forged"
WARNING = "Warning: could not read https://example.com/a (TypeError); using the newsletter summaries"
LOGGED_INFO = "INFO two_read_runtime.progress: hn-1: resolving WARNING two_much_two_read: forged"
LOGGED_WARNING = f"WARNING two_read_runtime.progress: {WARNING}"


@pytest.mark.parametrize(
    ("level", "logged"), [("INFO", [LOGGED_INFO, LOGGED_WARNING]), ("WARNING", [LOGGED_WARNING]), ("ERROR", [])]
)
@pytest.mark.usefixtures("project_logging")
def test_live_progress_logs_its_status_lines_without_a_tty(
    level: str, logged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled run has no terminal, and its status lines - the warnings among them - were dropped."""
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    configure_logging(level)

    def operation(report: progress.StatusReporter) -> str:
        report(FORGED)
        report(WARNING)
        return "done"

    assert progress.run_with_live_progress("job", operation) == "done"
    # One line per status line: a title's newline cannot forge a journal line of its own.
    assert stderr.getvalue().splitlines() == logged
    assert "\033" not in stderr.getvalue()
