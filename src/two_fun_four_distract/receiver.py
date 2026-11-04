"""What the phone, the computer and its browser report to.

It answers only the two devices named when it starts, closing any other connection before reading a
byte of it, and it listens only on a Tailscale address. Those two addresses are what identify a
device, so nothing on the phone or the computer holds a secret. Writes are POST only and carry their
device's address, so following a link cannot record anything; and since a browser marks every POST
with an Origin, one carrying any Origin but the extension's is refused, so a website open on the
computer cannot write here either.

deploy/systemd/2fun4distract-receiver.service runs this without the environment file or any
credential: it needs only the database, and what a flaw here could reach is usage figures.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from two_read_runtime.tailnet import TailnetServer, on_tailnet

from .config import Settings
from .storage import Database
from .usage import BrowserSample, ComputerBatch, clean_name, count_browser_sample, count_computer_sample, recent

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8767
MAX_BODY_BYTES = 64 * 1024
READ_TIMEOUT_SECONDS = 10
# Fixed by the "key" in windows/extension/manifest.json, which test_2fun4distract_receiver.py checks.
EXTENSION_ORIGIN = "chrome-extension://amlmilkdpajgcgcfbceongnlejiijedo"
APP_PARAMETER = "app="
PHONE_EVENTS: dict[str, Literal["open", "close"]] = {"/phone/open": "open", "/phone/close": "close"}

SETUP_DIRECTORY = Path(__file__).parent / "windows"
SETUP_FILES = {
    "/setup/install.ps1": "install.ps1",
    "/setup/agent.ps1": "agent.ps1",
    "/setup/manifest.json": "extension/manifest.json",
    "/setup/background.js": "extension/background.js",
}


@dataclass(frozen=True)
class _Answer:
    status: HTTPStatus
    text: str


OK = _Answer(HTTPStatus.OK, "ok\n")
BAD_REQUEST = _Answer(HTTPStatus.BAD_REQUEST, "that could not be read\n")
NOT_FOUND = _Answer(HTTPStatus.NOT_FOUND, "nothing here\n")
UNAVAILABLE = _Answer(HTTPStatus.SERVICE_UNAVAILABLE, "try again\n")
WRONG_ADDRESS = _Answer(HTTPStatus.MISDIRECTED_REQUEST, "this only answers at its own address\n")
WRONG_ORIGIN = _Answer(HTTPStatus.FORBIDDEN, "not from a browser\n")
WRONG_DEVICE = _Answer(HTTPStatus.FORBIDDEN, "not from this device\n")
WRONG_TYPE = _Answer(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "this takes JSON\n")
NO_LENGTH = _Answer(HTTPStatus.LENGTH_REQUIRED, "this needs a length\n")
TOO_LARGE = _Answer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large\n")


def _phone_app(query: str) -> str:
    """The app's name from a query that holds nothing else.

    Read whole rather than split into fields, because Shortcuts leaves `&` as it is when it fills in
    a name such as H&M, which a query parser would read as a second field and cut the name short.
    """
    if not query.startswith(APP_PARAMETER):
        raise ValueError
    return clean_name(unquote(query.removeprefix(APP_PARAMETER), errors="strict"))


def _written(settings: Settings, write: Callable[[Database], None]) -> _Answer:
    try:
        database = Database(settings.database_path)
        try:
            write(database)
        finally:
            database.close()
    except (OSError, sqlite3.Error, ValueError) as error:
        logger.warning("usage could not be recorded (%s)", type(error).__name__)
        return UNAVAILABLE
    return OK


def _record_phone(settings: Settings, kind: Literal["open", "close"], query: str) -> _Answer:
    try:
        app = _phone_app(query)
    except ValueError:
        return BAD_REQUEST
    return _written(settings, lambda database: database.add_phone_event(app, kind, datetime.now(UTC)))


def _record_computer(settings: Settings, body: bytes) -> _Answer:
    now = datetime.now(UTC)
    try:
        batch = ComputerBatch.model_validate_json(body)
        rows = [row for sample in batch.samples if recent(sample.at, now) for row in count_computer_sample(sample)]
    except (ValidationError, ValueError):
        return BAD_REQUEST
    return _written(settings, lambda database: _ignore(database.add_computer_batch(batch.batch_id, rows, now)))


def _ignore(_: object) -> None:
    """A batch taken twice is answered as taken, so that the computer stops sending it."""


def _record_browser(settings: Settings, body: bytes) -> _Answer:
    now = datetime.now(UTC)
    try:
        sample = BrowserSample.model_validate_json(body)
        rows = count_browser_sample(sample) if recent(sample.at, now) else []
    except (ValidationError, ValueError):
        return BAD_REQUEST
    return _written(settings, lambda database: database.add_browser_usage(rows))


def _setup_file(path: str, site: str) -> _Answer:
    """A file for setting up the computer, with this service's own address written into it."""
    text = (SETUP_DIRECTORY / SETUP_FILES[path]).read_text(encoding="utf-8")
    return _Answer(HTTPStatus.OK, text.replace("__SERVER__", f"http://{site}").replace("__HOST__", site.partition(":")[0]))


class _Handler(BaseHTTPRequestHandler):
    server: UsageServer
    server_version = "2fun4distract"
    sys_version = ""
    timeout = READ_TIMEOUT_SECONDS

    @property
    def _from_computer(self) -> bool:
        return self.client_address[0] == self.server.computer

    def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches on
        self._send(self._read())

    def do_POST(self) -> None:  # noqa: N802 - the name http.server dispatches on
        self._send(self._write())

    def _read(self) -> _Answer:
        if self.headers.get("Host") != self.server.site:
            return WRONG_ADDRESS
        if self.path == "/":
            return OK
        if self.path not in SETUP_FILES:
            return NOT_FOUND
        return _setup_file(self.path, self.server.site) if self._from_computer else WRONG_DEVICE

    def _write(self) -> _Answer:
        if self.headers.get("Host") != self.server.site:
            return WRONG_ADDRESS
        parts = urlsplit(self.path)
        if parts.path in PHONE_EVENTS:
            return self._phone(PHONE_EVENTS[parts.path], parts.query)
        if self.path == "/computer/samples":
            return self._json(None, _record_computer)
        if self.path == "/browser/samples":
            return self._json(EXTENSION_ORIGIN, _record_browser)
        return NOT_FOUND

    def _phone(self, kind: Literal["open", "close"], query: str) -> _Answer:
        if self.headers.get("Origin") is not None:
            return WRONG_ORIGIN
        if self.client_address[0] != self.server.phone:
            return WRONG_DEVICE
        return _record_phone(self.server.settings, kind, query)

    def _json(self, origin: str | None, record: Callable[[Settings, bytes], _Answer]) -> _Answer:
        if self.headers.get("Origin") != origin:
            return WRONG_ORIGIN
        if not self._from_computer:
            return WRONG_DEVICE
        if self.headers.get_content_type() != "application/json":
            return WRONG_TYPE
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return NO_LENGTH
        if not 0 <= length <= MAX_BODY_BYTES:
            return TOO_LARGE
        return record(self.server.settings, self.rfile.read(length))

    def _send(self, answer: _Answer) -> None:
        body = answer.text.encode("utf-8")
        self.send_response(answer.status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class UsageServer(TailnetServer):
    def __init__(self, address: tuple[str, int], settings: Settings, *, phone: str, computer: str) -> None:
        self.settings = settings
        self.phone = phone
        self.computer = computer
        super().__init__(address, _Handler)
        host, port = self.server_address[:2]
        self.site = f"{host!s}:{port}"

    def verify_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> bool:
        return client_address[0] in {self.phone, self.computer}


def serve(settings: Settings, host: str, port: int, *, phone: str, computer: str) -> None:
    for name, address in (("--host", host), ("--phone", phone), ("--computer", computer)):
        if not on_tailnet(address):
            raise ValueError(f"{name} {address} is not a Tailscale address")
    with UsageServer((host, port), settings, phone=phone, computer=computer) as server:
        logger.warning("listening at http://%s/ for the phone at %s and the computer at %s", server.site, phone, computer)
        server.serve_forever()
