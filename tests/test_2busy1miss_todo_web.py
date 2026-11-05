from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.parse import urlencode

import pytest
from conftest import directory_digest

from two_busy_one_miss import todo_web, todos
from two_busy_one_miss.config import Settings
from two_busy_one_miss.storage import Database
from two_busy_one_miss.todo_web import TodoPageServer
from two_read_runtime.tailnet import interpreter_root

# A Wednesday.
TODAY = date(2026, 11, 4)
YESTERDAY = TODAY - timedelta(days=1)
LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")
FORM = "application/x-www-form-urlencoded"
ONLY_FROM_THE_PAGE = todo_web._document("<p>Changes can only come from this page.</p>")
UNAVAILABLE = todo_web._document('<p role="alert">The to-do list can\'t be reached right now. Reload in a moment.</p>')


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(todos, "_today", lambda _settings, now: now.date() if now else TODAY)
    reminders_path = tmp_path / "reminders.yaml"
    reminders_path.write_text("timezone: America/Toronto\ncalendars:\n  - id: primary\n", encoding="utf-8")
    return Settings(
        reminders_config_path=reminders_path,
        database_path=tmp_path / "reminders.sqlite3",
        lock_path=tmp_path / "reminders.lock",
    )


def _running(server: TodoPageServer) -> Iterator[TodoPageServer]:
    # The default interval is half a second, which is what every shutdown below would wait.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def page(settings: Settings) -> Iterator[TodoPageServer]:
    yield from _running(TodoPageServer(("127.0.0.1", 0), settings, clients=LOOPBACK))


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Message
    text: str


def send(page: TodoPageServer, method: str, path: str, headers: dict[str, str], body: bytes = b"") -> Reply:
    host, port = page.server_address[:2]
    with closing(http.client.HTTPConnection(str(host), port, timeout=5)) as connection:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return Reply(response.status, response.headers, response.read().decode("utf-8"))


def get(page: TodoPageServer, path: str = "/") -> Reply:
    return send(page, "GET", path, {"Host": page.site})


def form_headers(page: TodoPageServer, body: bytes) -> dict[str, str]:
    return {"Host": page.site, "Origin": f"http://{page.site}", "Content-Type": FORM, "Content-Length": str(len(body))}


def post_body(page: TodoPageServer, body: bytes) -> Reply:
    return send(page, "POST", "/toggle", form_headers(page, body), body)


def post(page: TodoPageServer, todo_id: int, day: date = TODAY, change: str = "done") -> Reply:
    return post_body(page, urlencode({"id": todo_id, "day": day.isoformat(), "change": change}).encode())


def add(settings: Settings, title: str = "伏地挺身", on: str | None = None, first_day: date = YESTERDAY) -> int:
    return todos.add_todo(settings, title, on, now=datetime(first_day.year, first_day.month, first_day.day)).id


def completions(settings: Settings) -> list[tuple[int, str]]:
    if not settings.database_path.exists():
        return []
    with closing(sqlite3.connect(settings.database_path)) as connection:
        return connection.execute("SELECT todo_id, day FROM todo_completions ORDER BY day").fetchall()


def undone(settings: Settings, day: date) -> list[str]:
    database = Database(settings.database_path)
    try:
        return database.agenda_todos(day)[0]
    finally:
        database.close()


def test_the_page_lists_what_is_due_on_each_day_and_nothing_else(page: TodoPageServer, settings: Settings) -> None:
    add(settings, "每天的事")
    add(settings, "只有今天", "wed")
    add(settings, "只有昨天", "tue")
    add(settings, "這兩天都沒有", "sat")
    add(settings, "今天才加的", first_day=TODAY)
    removed = add(settings, "已移除")
    todos.remove_todo(settings, removed)

    reply = get(page)

    assert reply.status == 200
    today, yesterday = reply.text.split("Yesterday")
    assert "Today · Wed 11-04" in today
    assert "Yesterday" not in today
    assert yesterday.startswith(" · Tue 11-03")
    assert [title for title in ("每天的事", "只有今天", "今天才加的") if title in today] == ["每天的事", "只有今天", "今天才加的"]
    assert "只有昨天" not in today
    assert "每天的事" in yesterday
    assert "只有昨天" in yesterday
    assert "只有今天" not in yesterday
    assert "今天才加的" not in yesterday
    assert "這兩天都沒有" not in reply.text
    assert "已移除" not in reply.text


def test_the_page_says_so_when_nothing_is_due(page: TodoPageServer, settings: Settings) -> None:
    add(settings, "週六的事", "sat")

    reply = get(page)

    assert reply.status == 200
    assert reply.text == todo_web._document("<section><h2><span>Today · Wed 11-04</span></h2><p>Nothing due today.</p></section>")


def test_the_page_is_exactly_this(page: TodoPageServer, settings: Settings) -> None:
    open_id = add(settings, "跑步", "wed", first_day=TODAY)
    done_id = add(settings, "喝水", "wed", first_day=TODAY)
    todos.complete_todo(settings, done_id, TODAY)
    mark = '<span class="mark" aria-hidden="true"></span>'

    assert get(page).text == todo_web._document(
        '<section><h2><span>Today · Wed 11-04</span><span class="count">1/2</span></h2>'
        '<form method="post" action="/toggle">'
        f'<input type="hidden" name="id" value="{open_id}">'
        '<input type="hidden" name="day" value="2026-11-04">'
        '<input type="hidden" name="change" value="done">'
        f'<button type="submit" aria-pressed="false">{mark}<span class="title">跑步</span></button></form>'
        '<form method="post" action="/toggle" class="done">'
        f'<input type="hidden" name="id" value="{done_id}">'
        '<input type="hidden" name="day" value="2026-11-04">'
        '<input type="hidden" name="change" value="undo">'
        f'<button type="submit" aria-pressed="true">{mark}<span class="title">喝水</span></button></form>'
        "</section>"
    )


def test_every_page_starts_with_the_same_head() -> None:
    assert todo_web._document("<p>x</p>") == (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="icon" href="data:,">'
        f"<title>To do</title><style>{todo_web.STYLE}</style></head><body><main><p>x</p></main></body></html>"
    )


def test_the_pages_own_form_posted_back_works_on_the_real_clock(
    page: TodoPageServer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    todo_id = todos.add_todo(settings, "喝水", None).id
    form = re.search(r"<form[^>]*>(.*?)<button", get(page).text)
    assert form is not None
    fields = dict(re.findall(r'name="(\w+)" value="([^"]+)"', form.group(1)))
    assert fields["id"] == str(todo_id)
    assert fields["change"] == "done"

    assert post_body(page, urlencode(fields).encode()).status == 303

    assert completions(settings) == [(todo_id, fields["day"])]


def test_reading_the_page_creates_and_changes_nothing(page: TodoPageServer, settings: Settings, tmp_path: Path) -> None:
    assert get(page).status == 200
    assert not settings.database_path.exists()

    add(settings)
    before = directory_digest(tmp_path)
    assert get(page).status == 200
    assert directory_digest(tmp_path) == before


def test_a_title_cannot_become_markup(page: TodoPageServer, settings: Settings) -> None:
    add(settings, '<script>alert("x")</script> & co')

    text = get(page).text

    assert "<script" not in text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co" in text


def test_ticking_records_the_day_and_returns_to_the_page(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings)
    assert 'value="done"' in get(page).text.split("Yesterday")[0]

    reply = post(page, todo_id)

    assert reply.status == 303
    assert reply.headers["Location"] == "/"
    assert completions(settings) == [(todo_id, TODAY.isoformat())]
    today = get(page).text.split("Yesterday")[0]
    assert 'aria-pressed="true"' in today
    assert 'value="undo"' in today
    assert '<span class="count">1/1</span>' in today


def test_ticking_twice_leaves_it_ticked(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings)

    assert post(page, todo_id).status == 303
    assert post(page, todo_id).status == 303

    assert completions(settings) == [(todo_id, TODAY.isoformat())]


def test_unticking_takes_the_day_back(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings)
    post(page, todo_id)

    assert post(page, todo_id, change="undo").status == 303

    assert completions(settings) == []
    assert 'aria-pressed="false"' in get(page).text


def test_ticking_yesterday_clears_it_from_undone_only(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings)

    assert post(page, todo_id, YESTERDAY).status == 303

    assert undone(settings, YESTERDAY) == []
    assert undone(settings, TODAY) == ["伏地挺身"]


@pytest.mark.parametrize("day", [TODAY - timedelta(days=2), TODAY + timedelta(days=1)])
@pytest.mark.parametrize("change", ["done", "undo"])
def test_only_the_two_days_shown_can_change(page: TodoPageServer, settings: Settings, day: date, change: str) -> None:
    todo_id = add(settings, first_day=TODAY - timedelta(days=5))
    # So that an undo refused here has something it would otherwise have taken back.
    if change == "undo" and day < TODAY:
        todos.complete_todo(settings, todo_id, day)

    before = completions(settings)
    reply = post(page, todo_id, day, change)

    assert reply.status == 400
    assert '<main><p role="alert">Only today and yesterday can be changed here.</p>' in reply.text
    assert completions(settings) == before


def test_a_to_do_not_due_that_day_is_refused_with_the_reason(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings, "週六的事", "sat")

    reply = post(page, todo_id)

    assert reply.status == 400
    assert f'<main><p role="alert">to-do {todo_id} is not due on 2026-11-04; it falls on sat</p>' in reply.text
    assert completions(settings) == []


@pytest.mark.parametrize("todo_id", [999, 0, -1, 2**63, 10**40])
def test_an_unknown_to_do_is_refused_with_the_reason(page: TodoPageServer, settings: Settings, todo_id: int) -> None:
    add(settings)

    reply = post(page, todo_id)

    assert reply.status == 400
    assert f'<main><p role="alert">no to-do with id {todo_id}</p>' in reply.text


def test_a_refusal_still_shows_the_list_so_the_next_tap_is_there(page: TodoPageServer, settings: Settings) -> None:
    add(settings, "每天的事")

    assert "每天的事" in post(page, 999).text


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"junk",
        b"id=1&day=2026-11-04",
        b"id=1&change=done",
        b"day=2026-11-04&change=done",
        b"id=1&day=2026-11-04&change=done&extra=1",
        b"id=1&id=2&day=2026-11-04",
        b"id=1&day=2026-11-04&change=done&change=undo",
        b"id=1&day=2026-11-04&change=remove",
        b"id=one&day=2026-11-04&change=done",
        b"id=1&day=tomorrow&change=done",
        b"id=1&day=2026-11-04&change=done&",
        b"id=\xff&day=2026-11-04&change=done",
    ],
)
def test_anything_but_the_pages_own_form_changes_nothing(page: TodoPageServer, settings: Settings, body: bytes) -> None:
    add(settings)

    reply = post_body(page, body)

    assert reply.status == 400
    assert '<main><p role="alert">That change could not be read.</p>' in reply.text
    assert completions(settings) == []


@pytest.mark.parametrize("origin", [None, "null", "http://evil.example", "https://{site}", "http://{site}.evil.example"])
def test_a_change_from_anywhere_but_the_page_is_refused(page: TodoPageServer, settings: Settings, origin: str | None) -> None:
    todo_id = add(settings)
    body = urlencode({"id": todo_id, "day": TODAY.isoformat(), "change": "done"}).encode()
    headers = form_headers(page, body)
    del headers["Origin"]
    if origin is not None:
        headers["Origin"] = origin.format(site=page.site)

    reply = send(page, "POST", "/toggle", headers, body)

    assert reply.status == 403
    assert reply.text == ONLY_FROM_THE_PAGE
    assert completions(settings) == []


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("host", [None, "attacker.example", "attacker.example:{port}", "127.0.0.1"])
def test_an_address_other_than_its_own_is_refused(
    page: TodoPageServer, settings: Settings, method: str, host: str | None
) -> None:
    todo_id = add(settings, "別人看不到")
    body = urlencode({"id": todo_id, "day": TODAY.isoformat(), "change": "done"}).encode()
    headers = form_headers(page, body)
    del headers["Host"]
    if host is not None:
        headers["Host"] = host.format(port=page.server_address[1])

    reply = send(page, method, "/" if method == "GET" else "/toggle", headers, body if method == "POST" else b"")

    assert reply.status == 421
    assert reply.text == todo_web._document("<p>This page only answers at its own address.</p>")
    assert completions(settings) == []


@pytest.mark.parametrize("path", ["/toggle", "/?x=1", "/todos", "/favicon.ico", "/../reminders.sqlite3"])
def test_a_get_for_anything_but_the_page_finds_nothing(page: TodoPageServer, path: str) -> None:
    reply = get(page, path)

    assert reply.status == 404
    assert reply.text == todo_web._document("<p>There is nothing here.</p>")


@pytest.mark.parametrize("path", ["/", "/toggle?id=1", "/remove"])
def test_a_post_to_anywhere_but_the_form_finds_nothing(page: TodoPageServer, settings: Settings, path: str) -> None:
    todo_id = add(settings)
    body = urlencode({"id": todo_id, "day": TODAY.isoformat(), "change": "done"}).encode()

    assert send(page, "POST", path, form_headers(page, body), body).status == 404
    assert completions(settings) == []


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_no_other_method_is_answered(page: TodoPageServer, settings: Settings, method: str) -> None:
    todo_id = add(settings)
    body = urlencode({"id": todo_id, "day": TODAY.isoformat(), "change": "done"}).encode()

    assert send(page, method, "/toggle", form_headers(page, body), body).status == 501
    assert completions(settings) == []


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "multipart/form-data; boundary=x"])
def test_another_content_type_is_refused(page: TodoPageServer, settings: Settings, content_type: str) -> None:
    todo_id = add(settings)
    body = urlencode({"id": todo_id, "day": TODAY.isoformat(), "change": "done"}).encode()

    reply = send(page, "POST", "/toggle", form_headers(page, body) | {"Content-Type": content_type}, body)

    assert reply.status == 415
    assert reply.text == ONLY_FROM_THE_PAGE
    assert completions(settings) == []


def test_a_form_charset_is_still_the_form(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings)
    body = urlencode({"id": todo_id, "day": TODAY.isoformat(), "change": "done"}).encode()

    reply = send(page, "POST", "/toggle", form_headers(page, body) | {"Content-Type": f"{FORM}; charset=UTF-8"}, body)

    assert reply.status == 303


@pytest.mark.parametrize(
    ("length", "status"),
    [(None, 411), ("many", 411), ("-1", 413), (str(todo_web.MAX_BODY_BYTES + 1), 413), ("99999999999", 413)],
)
def test_a_body_of_no_stated_or_too_great_a_length_is_not_read(page: TodoPageServer, length: str | None, status: int) -> None:
    headers = form_headers(page, b"") | {"Content-Length": length or ""}
    if length is None:
        del headers["Content-Length"]

    reply = send(page, "POST", "/toggle", headers)

    assert reply.status == status
    assert reply.text == ONLY_FROM_THE_PAGE


def test_a_body_exactly_at_the_cap_is_read(page: TodoPageServer) -> None:
    assert post_body(page, b"x" * todo_web.MAX_BODY_BYTES).status == 400


def test_the_longest_real_form_fits_the_cap_on_a_body() -> None:
    longest = urlencode({"id": 2**63 - 1, "day": "2026-11-04", "change": "done"}).encode()

    assert len(longest) * 2 <= todo_web.MAX_BODY_BYTES


def test_every_answer_carries_the_protective_headers(page: TodoPageServer, settings: Settings) -> None:
    todo_id = add(settings)
    answers = [
        get(page),
        post(page, todo_id),
        post(page, 999),
        get(page, "/nowhere"),
        send(page, "GET", "/", {"Host": "attacker.example"}),
        send(page, "POST", "/toggle", {"Host": page.site, "Origin": "http://evil.example"}),
    ]

    assert [answer.status for answer in answers] == [200, 303, 400, 404, 421, 403]
    for answer in answers:
        assert answer.headers["Cache-Control"] == "no-store"
        assert answer.headers["X-Content-Type-Options"] == "nosniff"
        # no-referrer would turn the page's own Origin into `null` and so refuse its own forms.
        assert answer.headers["Referrer-Policy"] == "same-origin"
        assert answer.headers["Content-Type"] == "text/html; charset=utf-8"
        assert answer.headers["Content-Length"] == str(len(answer.text.encode()))
        policy = answer.headers["Content-Security-Policy"]
        for directive in ("default-src 'none'", "form-action 'self'", "base-uri 'none'", "frame-ancestors 'none'"):
            assert directive in policy
        assert "python" not in answer.headers["Server"].lower()


def test_the_style_policy_lets_through_exactly_the_pages_style(page: TodoPageServer) -> None:
    reply = get(page)
    style = re.search(r"<style>(.*)</style>", reply.text, re.DOTALL)
    assert style is not None
    digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()

    # In the encoding a browser requires: Chrome drops the stylesheet over a hexadecimal hash.
    assert f"style-src 'sha256-{digest}';" in reply.headers["Content-Security-Policy"]


def test_the_policy_allows_the_pages_empty_icon(page: TodoPageServer) -> None:
    reply = get(page)

    assert '<link rel="icon" href="data:,">' in reply.text
    assert "img-src data:;" in reply.headers["Content-Security-Policy"]


def test_a_connection_from_outside_the_tailnet_is_closed_unanswered(settings: Settings) -> None:
    todo_id = add(settings, "別人看不到")
    # The default clients, and this test's connection coming from 127.0.0.1.
    for server in _running(TodoPageServer(("127.0.0.1", 0), settings)):
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            post(server, todo_id)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            get(server)
    assert completions(settings) == []


@pytest.mark.parametrize("client", ["100.64.0.1", "100.89.225.36", "100.127.255.255"])
def test_the_tailnet_range_is_let_in(settings: Settings, client: str) -> None:
    for server in _running(TodoPageServer(("127.0.0.1", 0), settings)):
        assert server.verify_request(None, (client, 50000))  # type: ignore[arg-type]


@pytest.mark.parametrize("client", ["100.63.255.255", "100.128.0.0", "192.168.30.180", "127.0.0.1", "8.8.8.8"])
def test_an_address_beside_the_tailnet_range_is_kept_out(settings: Settings, client: str) -> None:
    for server in _running(TodoPageServer(("127.0.0.1", 0), settings)):
        assert not server.verify_request(None, (client, 50000))  # type: ignore[arg-type]


def test_a_list_that_cannot_be_read_says_so_without_the_reason(
    page: TodoPageServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unreadable(*_: object) -> None:
        raise ValueError("a value the configuration held")

    monkeypatch.setattr(todos, "list_todos", unreadable)

    with caplog.at_level(logging.WARNING, logger=todo_web.__name__):
        reply = get(page)

    assert reply.status == 503
    assert reply.text == UNAVAILABLE
    assert caplog.messages == ["the to-do list could not be read or changed (ValueError)"]


def test_a_clock_that_cannot_be_read_says_so(
    page: TodoPageServer, settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    todo_id = add(settings)

    def unreadable(*_: object) -> None:
        raise ValueError("a value the configuration held")

    monkeypatch.setattr(todos, "_today", unreadable)

    with caplog.at_level(logging.WARNING, logger=todo_web.__name__):
        reply = post(page, todo_id)

    assert reply.status == 503
    assert reply.text == UNAVAILABLE
    assert caplog.messages == ["the to-do list could not be read or changed (ValueError)"]
    assert completions(settings) == []


def test_a_database_that_stays_locked_says_so(
    page: TodoPageServer, settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    todo_id = add(settings)

    def locked(*_: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setitem(todo_web.CHANGES, "done", locked)

    with caplog.at_level(logging.WARNING, logger=todo_web.__name__):
        reply = post(page, todo_id)

    assert reply.status == 503
    assert reply.text == UNAVAILABLE
    assert caplog.messages == ["the to-do list could not be read or changed (OperationalError)"]


def test_the_page_serves_two_requests_at_once(page: TodoPageServer, settings: Settings) -> None:
    add(settings)
    # A request still being sent must not hold up the one after it.
    host, port = page.server_address[:2]
    with closing(http.client.HTTPConnection(str(host), port, timeout=5)) as stalled:
        stalled.connect()
        stalled.putrequest("GET", "/", skip_host=True, skip_accept_encoding=True)

        assert get(page).status == 200


def test_serve_listens_where_it_is_told_and_lets_in_the_tailnet_only(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served: list[TodoPageServer] = []
    monkeypatch.setattr(todo_web, "TAILNET", LOOPBACK)
    monkeypatch.setattr(TodoPageServer, "serve_forever", lambda server: served.append(server))

    with caplog.at_level(logging.WARNING, logger=todo_web.__name__):
        todo_web.serve(settings, "127.0.0.1", 0)

    assert len(served) == 1
    assert served[0].settings is settings
    assert served[0].server_address[0] == "127.0.0.1"
    assert served[0].clients == ipaddress.IPv4Network("100.64.0.0/10")
    assert caplog.messages == [f"the to-do page is at http://{served[0].site}/"]


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "192.168.30.180", "100.63.255.255", "100.128.0.0", "::1"])
def test_serve_refuses_an_address_outside_the_tailnet_by_name(settings: Settings, host: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(host)} is not a Tailscale address"):
        todo_web.serve(settings, host, 0)


def test_interpreter_root_of_a_uv_managed_python(tmp_path: Path) -> None:
    installs = tmp_path / "uv" / "python"
    real = installs / "cpython-3.13.14-linux-x86_64-gnu"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.13").touch()
    (installs / "cpython-3.13-linux-x86_64-gnu").symlink_to(real)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(installs / "cpython-3.13-linux-x86_64-gnu" / "bin" / "python3.13")

    assert interpreter_root(str(venv / "python"), str(real)) == str(installs)


def test_interpreter_root_follows_a_standard_venvs_links_out_of_the_environment(tmp_path: Path) -> None:
    install = tmp_path / "pyenv" / "versions" / "3.12.1"
    (install / "bin").mkdir(parents=True)
    (install / "bin" / "python3").touch()
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").symlink_to(install / "bin" / "python3")
    (venv / "python").symlink_to("python3")

    assert interpreter_root(str(venv / "python"), str(install)) == str(install)


def test_interpreter_root_reads_a_relative_link(tmp_path: Path) -> None:
    install = tmp_path / "python"
    (install / "bin").mkdir(parents=True)
    (install / "bin" / "python3").touch()
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(Path("..") / ".." / "python" / "bin" / "python3")

    assert interpreter_root(str(venv / "python"), str(install)) == str(install)


def test_interpreter_root_of_the_system_python_is_the_system(tmp_path: Path) -> None:
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to("/usr/bin/python3.12")

    assert interpreter_root(str(venv / "python"), "/usr") == "/usr"


def test_interpreter_root_of_a_copied_python_is_its_installation(tmp_path: Path) -> None:
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").touch()

    assert interpreter_root(str(venv / "python"), "/opt/python") == "/opt/python"
