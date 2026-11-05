"""What a service answering only the devices on the tailnet needs, whichever tool it belongs to."""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from http.server import ThreadingHTTPServer
from typing import Any

TAILNET = ipaddress.IPv4Network("100.64.0.0/10")


class TailnetServer(ThreadingHTTPServer):
    """An HTTP server whose clients may leave without waiting for the answer."""

    def handle_error(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        # A browser closing mid-answer is ordinary, and a traceback over it would bury real faults.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def on_tailnet(address: str) -> bool:
    """Whether this is a Tailscale address, and False for anything that is not an address at all."""
    try:
        return ipaddress.ip_address(address) in TAILNET
    except ValueError:
        return False


def interpreter_root(executable: str, base_prefix: str) -> str:
    """The one directory holding every step from a virtual environment's python to the real one.

    The units' sandbox hides home, so an interpreter installed there has to be bound back in. The
    installation alone is not enough: uv links a virtual environment to cpython-3.13-linux-.../bin,
    itself a link to cpython-3.13.14-linux-..., and the first of those has to be there too.
    """
    link = executable
    while os.path.islink(link) and os.path.dirname(link) == os.path.dirname(executable):
        link = os.path.normpath(os.path.join(os.path.dirname(link), os.readlink(link)))
    return os.path.commonpath([base_prefix, base_prefix if link == executable else link])
