from __future__ import annotations

import pytest

from two_read_runtime.tailnet import TailnetServer


class _Quiet(TailnetServer):
    def __init__(self) -> None:
        """No socket: this only exercises how an error in a handler is reported."""


@pytest.mark.parametrize("error", [BrokenPipeError(), ConnectionResetError(), ConnectionAbortedError()])
def test_a_client_that_left_is_not_a_fault(capsys: pytest.CaptureFixture[str], error: ConnectionError) -> None:
    try:
        raise error
    except ConnectionError:
        _Quiet().handle_error(None, ("100.64.0.1", 1))  # type: ignore[arg-type]

    assert capsys.readouterr().err == ""


def test_anything_else_is_still_reported(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        raise ValueError("a real fault")
    except ValueError:
        _Quiet().handle_error(None, ("100.64.0.1", 1))  # type: ignore[arg-type]

    reported = capsys.readouterr().err
    assert "a real fault" in reported
    assert "100.64.0.1" in reported
