"""The to-do page: today's and yesterday's to-dos, ticked off from a browser over Tailscale.

The page has no password, because a link carrying one would sit in Discord's history for good. It
stays private by only being there for the devices on the tailnet:

- It will not listen on anything but a Tailscale address, and it closes a connection from outside
  the tailnet before reading a byte of it.
- It answers only to its own address in the Host header, so that a website cannot point a name of
  its own at this address and read the list through the browser of someone on the tailnet.
- A change must carry this page's own Origin, so that a website open in that browser cannot submit
  one. A change is a POST, so following a link can never make one.

A change can only tick or untick, and only on the two days the page shows. Nothing here adds,
renames or removes a to-do, and deploy/systemd/2busy1miss-todo-web.service runs this without the
environment file or any credential, so what a flaw here could reach is this list.
"""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import logging
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs

from two_read_runtime.tailnet import TAILNET, TailnetServer

from . import todos
from .config import Settings
from .rules import period_start
from .todos import TodoChangeResult, TodoListResult, TodoView

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8766
# The largest form the page itself submits is well under this.
MAX_BODY_BYTES = 256
READ_TIMEOUT_SECONDS = 10
ROUTES = {"GET": "/", "POST": "/toggle"}
CHANGES: dict[str, Callable[[Settings, int, date], TodoChangeResult]] = {
    "done": todos.complete_todo,
    "undo": todos.reopen_todo,
}

STYLE = """
:root {
  color-scheme: light dark;
  --ground: #eef2ef;
  --surface: #ffffff;
  --ink: #18201c;
  --muted: #58655e;
  --line: #cbd4ce;
  --accent: #1f6b4a;
  --on-accent: #ffffff;
  --alert: #9a3412;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground: #131715;
    --surface: #1d2320;
    --ink: #e6ebe8;
    --muted: #97a39c;
    --line: #38423d;
    --accent: #6cc79c;
    --on-accent: #0d1a13;
    --alert: #f4a582;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font: 17px/1.4 system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
}
main { display: grid; gap: 28px; max-width: 34rem; margin: 0 auto; padding: 24px 16px 48px; }
section { display: grid; gap: 8px; }
h2 {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin: 0 0 4px;
  color: var(--muted);
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.count { font-variant-numeric: tabular-nums; }
p { margin: 0; color: var(--muted); }
p[role="alert"] { color: var(--alert); font-weight: 600; }
form { margin: 0; }
button {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  min-height: 56px;
  padding: 12px 16px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
button:hover { border-color: var(--accent); }
button:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.mark {
  display: grid;
  flex: none;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 2px solid var(--muted);
  border-radius: 6px;
  color: var(--on-accent);
  font-size: 16px;
  font-weight: 700;
  line-height: 1;
}
.done .mark { border-color: var(--accent); background: var(--accent); }
.done .mark::after { content: "\\2713"; }
.done .title { color: var(--muted); text-decoration: line-through; }
.period {
  margin-left: auto;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
"""

HEADERS = (
    ("Cache-Control", "no-store"),
    (
        "Content-Security-Policy",
        "default-src 'none'; "
        # The page's one stylesheet and nothing else. A browser reads this hash as base64 only, and
        # drops the whole stylesheet over any other encoding.
        f"style-src 'sha256-{base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()}'; "
        # For the empty icon in the page's head, which saves a request for /favicon.ico.
        "img-src data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ),
    # Not no-referrer: that makes a browser send `Origin: null` on the page's own forms, which the
    # Origin check would then refuse.
    ("Referrer-Policy", "same-origin"),
    ("X-Content-Type-Options", "nosniff"),
)


@dataclass(frozen=True)
class _Answer:
    status: HTTPStatus
    body: str = ""
    location: str | None = None


def _document(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="icon" href="data:,">'
        f"<title>To do</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def _row(todo: TodoView, day: date) -> str:
    state = ' class="done"' if todo.done else ""
    period = "" if todo.period == "day" else f'<span class="period">{html.escape(todo.period)}</span>'
    return (
        f'<form method="post" action="/toggle"{state}>'
        f'<input type="hidden" name="id" value="{todo.id}">'
        f'<input type="hidden" name="day" value="{day.isoformat()}">'
        f'<input type="hidden" name="change" value="{"undo" if todo.done else "done"}">'
        f'<button type="submit" aria-pressed="{"true" if todo.done else "false"}">'
        f'<span class="mark" aria-hidden="true"></span><span class="title">{html.escape(todo.title)}</span>{period}'
        "</button></form>"
    )


def _section(heading: str, listing: TodoListResult, *, when_empty: str | None) -> str:
    due = [todo for todo in listing.todos if todo.due]
    if not due and when_empty is None:
        return ""
    done = sum(todo.done for todo in due)
    count = f'<span class="count">{done}/{len(due)}</span>' if due else ""
    title = f"<h2><span>{heading} · {listing.day:%a %m-%d}</span>{count}</h2>"
    rows = "".join(_row(todo, listing.day) for todo in due) or f"<p>{when_empty}</p>"
    return f"<section>{title}{rows}</section>"


def _unavailable(error: Exception) -> _Answer:
    # Only the type: a ValidationError over the reminders file quotes the value it rejected.
    logger.warning("the to-do list could not be read or changed (%s)", type(error).__name__)
    return _Answer(
        HTTPStatus.SERVICE_UNAVAILABLE,
        _document('<p role="alert">The to-do list can\'t be reached right now. Reload in a moment.</p>'),
    )


def _show(settings: Settings, status: HTTPStatus = HTTPStatus.OK, notice: str | None = None) -> _Answer:
    try:
        today = todos.list_todos(settings)
        yesterday = todos.list_todos(settings, today.day - timedelta(days=1))
    except (OSError, ValueError, sqlite3.Error) as error:
        return _unavailable(error)
    # A weekly or monthly to-do is the same one today as yesterday, until its period has run out.
    ended = [todo for todo in yesterday.todos if period_start(todo.period, yesterday.day) != period_start(todo.period, today.day)]
    yesterday = yesterday.model_copy(update={"todos": ended})
    parts = [
        f'<p role="alert">{html.escape(notice)}</p>' if notice else "",
        _section("Today", today, when_empty="Nothing due today."),
        _section("Yesterday", yesterday, when_empty=None),
    ]
    return _Answer(status, _document("".join(parts)))


def _parsed_change(body: bytes) -> tuple[int, date, str]:
    """The fields of the page's own form, or ValueError over anything else."""
    fields = parse_qs(body.decode("ascii"), strict_parsing=True)
    if set(fields) != {"id", "day", "change"} or any(len(values) != 1 for values in fields.values()):
        raise ValueError
    change = fields["change"][0]
    if change not in CHANGES:
        raise ValueError
    return int(fields["id"][0]), date.fromisoformat(fields["day"][0]), change


def _apply(settings: Settings, body: bytes) -> _Answer:
    try:
        todo_id, day, change = _parsed_change(body)
    except ValueError:
        return _show(settings, HTTPStatus.BAD_REQUEST, "That change could not be read.")
    try:
        today = todos._today(settings, None)
    except (OSError, ValueError) as error:
        return _unavailable(error)
    if day not in {today, today - timedelta(days=1)}:
        return _show(settings, HTTPStatus.BAD_REQUEST, "Only today and yesterday can be changed here.")
    try:
        CHANGES[change](settings, todo_id, day)
    except ValueError as error:
        return _show(settings, HTTPStatus.BAD_REQUEST, str(error))
    except (OSError, sqlite3.Error) as error:
        return _unavailable(error)
    # So that reloading the page shows it again rather than offering to resubmit the form.
    return _Answer(HTTPStatus.SEE_OTHER, location="/")


class _Handler(BaseHTTPRequestHandler):
    server: TodoPageServer
    server_version = "2busy1miss"
    sys_version = ""
    timeout = READ_TIMEOUT_SECONDS

    def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches on
        self._send(self._refusal() or _show(self.server.settings))

    def do_POST(self) -> None:  # noqa: N802 - the name http.server dispatches on
        self._send(self._refusal() or self._change())

    def _refusal(self) -> _Answer | None:
        site = self.server.site
        if self.headers.get("Host") != site:
            return _Answer(HTTPStatus.MISDIRECTED_REQUEST, _document("<p>This page only answers at its own address.</p>"))
        if self.path != ROUTES[self.command]:
            return _Answer(HTTPStatus.NOT_FOUND, _document("<p>There is nothing here.</p>"))
        if self.command == "POST" and self.headers.get("Origin") != f"http://{site}":
            return _Answer(HTTPStatus.FORBIDDEN, _document("<p>Changes can only come from this page.</p>"))
        return None

    def _change(self) -> _Answer:
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            return _Answer(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, _document("<p>Changes can only come from this page.</p>"))
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return _Answer(HTTPStatus.LENGTH_REQUIRED, _document("<p>Changes can only come from this page.</p>"))
        if not 0 <= length <= MAX_BODY_BYTES:
            return _Answer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _document("<p>Changes can only come from this page.</p>"))
        return _apply(self.server.settings, self.rfile.read(length))

    def _send(self, answer: _Answer) -> None:
        body = answer.body.encode("utf-8")
        self.send_response(answer.status)
        if answer.location is not None:
            self.send_header("Location", answer.location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in HEADERS:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class TodoPageServer(TailnetServer):
    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        *,
        clients: ipaddress.IPv4Network = TAILNET,
    ) -> None:
        self.settings = settings
        self.clients = clients
        super().__init__(address, _Handler)
        host, port = self.server_address[:2]
        self.site = f"{host!s}:{port}"

    def verify_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> bool:
        return ipaddress.ip_address(client_address[0]) in self.clients


def serve(settings: Settings, host: str, port: int) -> None:
    if ipaddress.ip_address(host) not in TAILNET:
        raise ValueError(f"{host} is not a Tailscale address; the to-do page only listens on one")
    with TodoPageServer((host, port), settings) as server:
        logger.warning("the to-do page is at http://%s/", server.site)
        server.serve_forever()
