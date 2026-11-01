from __future__ import annotations

import logging
import sys

# Only this project's own loggers. The root logger, and every library under it, stays where Python
# leaves it - warnings and above - because httpx logs each request's full URL at INFO, and a Discord
# webhook URL carries its token.
PROJECT_LOGGERS = ("two_much_two_read", "two_read_runtime")
FORMAT = "%(levelname)s %(name)s: %(message)s"


class _StderrHandler(logging.Handler):
    """Writes to whatever sys.stderr is when a record arrives, as Python's last-resort handler does.

    A StreamHandler keeps the stream it was made with; one made while a test runner had swapped
    stderr out would go on writing to that closed buffer after the runner put the real one back.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


_handler = _StderrHandler()
_handler.setFormatter(logging.Formatter(FORMAT))


def configure_logging(level: str) -> None:
    """Send this project's log records at level and above to stderr - the journal, under systemd."""
    for name in PROJECT_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if _handler not in logger.handlers:
            logger.addHandler(_handler)
