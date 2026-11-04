from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import directory_digest
from typer.testing import CliRunner

from two_fun_four_distract import cli, config, receiver
from two_fun_four_distract.config import Settings
from two_fun_four_distract.receiver import EXTENSION_ORIGIN, SETUP_FILES, UsageServer
from two_fun_four_distract.storage import Database
from two_fun_four_distract.usage import MAX_SAMPLE_SECONDS
from two_read_runtime.tailnet import on_tailnet

# The test's own connections come from loopback, so loopback plays each device in turn.
LOOPBACK = "127.0.0.1"
ELSEWHERE = "100.100.100.100"
JSON = "application/json"
WINDOWS = Path(receiver.__file__).parent / "windows"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(config, "settings_env_file", lambda: tmp_path / "no-such.env")
    return Settings(database_path=tmp_path / "usage.sqlite3", lock_path=tmp_path / "usage.lock")


def running(server: UsageServer) -> Iterator[UsageServer]:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def both(settings: Settings) -> Iterator[UsageServer]:
    """Loopback as the phone and the computer at once, for what does not depend on which it is."""
    yield from running(UsageServer((LOOPBACK, 0), settings, phone=LOOPBACK, computer=LOOPBACK))


@pytest.fixture
def phone_only(settings: Settings) -> Iterator[UsageServer]:
    yield from running(UsageServer((LOOPBACK, 0), settings, phone=LOOPBACK, computer=ELSEWHERE))


@pytest.fixture
def computer_only(settings: Settings) -> Iterator[UsageServer]:
    yield from running(UsageServer((LOOPBACK, 0), settings, phone=ELSEWHERE, computer=LOOPBACK))


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    text: str


def send(server: UsageServer, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b"") -> Reply:
    host, port = server.server_address[:2]
    with closing(http.client.HTTPConnection(str(host), port, timeout=5)) as connection:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in ({"Host": server.site, "Content-Length": str(len(body))} | (headers or {})).items():
            if value:
                connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return Reply(response.status, dict(response.getheaders()), response.read().decode("utf-8"))


def rows(settings: Settings, query: str) -> list[tuple[object, ...]]:
    if not settings.database_path.exists():
        return []
    with closing(sqlite3.connect(settings.database_path)) as connection:
        return connection.execute(query).fetchall()


def phone_events(settings: Settings) -> list[tuple[object, ...]]:
    return rows(settings, "SELECT app,kind FROM phone_events ORDER BY id")


def computer_usage(settings: Settings) -> list[tuple[object, ...]]:
    return rows(settings, "SELECT app,focused_seconds,visible_seconds FROM computer_usage ORDER BY minute,app")


def browser_usage(settings: Settings) -> list[tuple[object, ...]]:
    return rows(settings, "SELECT domain,focused_seconds,visible_seconds FROM browser_usage ORDER BY minute,domain")


def now_text(delta: timedelta = timedelta()) -> str:
    """Far enough into a recent minute that a sample of up to 45 seconds stays inside it."""
    moment = (datetime.now(UTC) - timedelta(minutes=2)).replace(second=45) + delta
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def batch(*processes: str, batch_id: str | None = None, at: str | None = None) -> bytes:
    return json.dumps(
        {
            "batch_id": batch_id or str(uuid.uuid4()),
            "samples": [
                {
                    "at": at or now_text(),
                    "seconds": 15,
                    "idle_seconds": 0,
                    "locked": False,
                    "entries": [
                        {"monitor": 1, "process": process, "percent": 50.0, "foreground": index == 0}
                        for index, process in enumerate(processes)
                    ],
                }
            ],
        }
    ).encode()


def sites(*domains: str, at: str | None = None) -> bytes:
    return json.dumps(
        {
            "at": at or now_text(),
            "seconds": 30,
            "entries": [{"domain": domain, "focused": index == 0, "visible": True} for index, domain in enumerate(domains)],
        }
    ).encode()


def post_batch(server: UsageServer, body: bytes) -> Reply:
    return send(server, "POST", "/computer/samples", {"Content-Type": JSON}, body)


def post_sites(server: UsageServer, body: bytes) -> Reply:
    return send(server, "POST", "/browser/samples", {"Content-Type": JSON, "Origin": EXTENSION_ORIGIN}, body)


def test_the_phone_records_an_open_and_a_close(phone_only: UsageServer, settings: Settings) -> None:
    assert send(phone_only, "POST", f"/phone/open?app={quote('世界迷霧')}").text == "ok\n"
    assert send(phone_only, "POST", f"/phone/close?app={quote('世界迷霧')}").status == 200

    assert phone_events(settings) == [("世界迷霧", "open"), ("世界迷霧", "close")]


@pytest.mark.parametrize(
    ("query", "app"),
    [
        ("app=H&M", "H&M"),
        ("app=H%26M", "H&M"),
        ("app=YouTube%20Music", "YouTube Music"),
        ("app=C%2B%2B", "C++"),
        ("app=a+b", "a+b"),
    ],
)
def test_a_phone_app_name_is_read_whole(phone_only: UsageServer, settings: Settings, query: str, app: str) -> None:
    assert send(phone_only, "POST", f"/phone/open?{query}").status == 200

    assert phone_events(settings) == [(app, "open")]


@pytest.mark.parametrize(
    "path",
    ["/phone/open", "/phone/open?", "/phone/open?app=", "/phone/open?name=A", "/phone/open?app=%00", "/phone/open?app=%ff"],
)
def test_a_phone_event_naming_no_app_is_refused(phone_only: UsageServer, settings: Settings, path: str) -> None:
    assert send(phone_only, "POST", path).status == 400
    assert phone_events(settings) == []


def test_following_a_link_records_nothing(both: UsageServer, settings: Settings) -> None:
    assert send(both, "GET", "/phone/open?app=A").status == 404
    assert phone_events(settings) == []


@pytest.mark.parametrize("origin", ["null", "http://evil.example", EXTENSION_ORIGIN])
def test_a_browser_cannot_record_a_phone_event(phone_only: UsageServer, settings: Settings, origin: str) -> None:
    assert send(phone_only, "POST", "/phone/open?app=A", {"Origin": origin}).status == 403
    assert phone_events(settings) == []


def test_the_computer_cannot_record_a_phone_event(computer_only: UsageServer, settings: Settings) -> None:
    assert send(computer_only, "POST", "/phone/open?app=A").status == 403
    assert phone_events(settings) == []


def test_the_phone_cannot_record_the_computers_samples(phone_only: UsageServer, settings: Settings) -> None:
    assert post_batch(phone_only, batch("Code")).status == 403
    assert post_sites(phone_only, sites("a.com")).status == 403
    assert computer_usage(settings) == []
    assert browser_usage(settings) == []


def test_the_computer_records_samples(computer_only: UsageServer, settings: Settings) -> None:
    assert post_batch(computer_only, batch("Code", "Discord")).status == 200

    assert computer_usage(settings) == [("Code", 15, 15), ("Discord", 0, 15)]


def sample_of(at: str, process: str, *, seconds: int = 15, locked: bool = False) -> dict[str, object]:
    entries = [{"monitor": 1, "process": process, "percent": 50.0, "foreground": True}]
    return {"at": at, "seconds": seconds, "idle_seconds": 0, "locked": locked, "entries": entries}


def screen_usage(settings: Settings) -> list[tuple[object, ...]]:
    return rows(settings, "SELECT visible_seconds FROM screen_usage ORDER BY minute")


def test_apps_following_each_other_in_a_minute_still_add_up_on_screen(computer_only: UsageServer, settings: Settings) -> None:
    at = (datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    samples = [sample_of(at, "Code"), sample_of(at, "Code"), sample_of(at, "Notepad"), sample_of(at, "Notepad")]
    body = json.dumps({"batch_id": str(uuid.uuid4()), "samples": samples}).encode()

    assert post_batch(computer_only, body).status == 200

    assert computer_usage(settings) == [("Code", 30, 30), ("Notepad", 30, 30)]
    assert screen_usage(settings) == [(60,)]


def test_apps_showing_at_once_count_once_on_screen(computer_only: UsageServer, settings: Settings) -> None:
    assert post_batch(computer_only, batch("Code", "Discord")).status == 200

    assert screen_usage(settings) == [(15,)]


def test_a_sample_counts_for_the_time_it_says_passed(computer_only: UsageServer, settings: Settings) -> None:
    samples = [sample_of(now_text(), "Code", seconds=40)]
    body = json.dumps({"batch_id": str(uuid.uuid4()), "samples": samples}).encode()

    assert post_batch(computer_only, body).status == 200

    assert computer_usage(settings) == [("Code", 40, 40)]
    assert screen_usage(settings) == [(40,)]


def test_time_held_up_by_sending_is_kept_across_the_minute(computer_only: UsageServer, settings: Settings) -> None:
    minute = (datetime.now(UTC) - timedelta(minutes=2)).replace(second=0, microsecond=0)
    moments = [(10, 25), (25, 15), (40, 15), (55, 15)]
    samples = [
        sample_of((minute + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%SZ"), "Code", seconds=seconds)
        for second, seconds in moments
    ]
    body = json.dumps({"batch_id": str(uuid.uuid4()), "samples": samples}).encode()

    assert post_batch(computer_only, body).status == 200

    assert computer_usage(settings) == [("Code", 15, 15), ("Code", 55, 55)]
    assert screen_usage(settings) == [(15,), (55,)]


def test_a_locked_or_old_sample_is_no_time_on_screen(computer_only: UsageServer, settings: Settings) -> None:
    samples = [sample_of(now_text(), "Code", locked=True), sample_of(now_text(-timedelta(days=15)), "Code")]
    body = json.dumps({"batch_id": str(uuid.uuid4()), "samples": samples}).encode()

    assert post_batch(computer_only, body).status == 200

    assert screen_usage(settings) == []


def test_the_agents_longest_sample_is_one_the_receiver_takes() -> None:
    agent = (WINDOWS / "agent.ps1").read_text(encoding="utf-8")

    assert f"$MaxSampleSeconds = {MAX_SAMPLE_SECONDS}\n" in agent


def test_a_batch_sent_twice_counts_once_and_is_still_answered_as_taken(computer_only: UsageServer, settings: Settings) -> None:
    body = batch("Code")

    assert post_batch(computer_only, body).status == 200
    assert post_batch(computer_only, body).status == 200

    assert computer_usage(settings) == [("Code", 15, 15)]
    assert screen_usage(settings) == [(15,)]


def test_a_minute_of_an_app_cannot_be_worth_more_than_a_minute(computer_only: UsageServer, settings: Settings) -> None:
    moment = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(5):
        assert post_batch(computer_only, batch("Code", at=at)).status == 200

    assert computer_usage(settings) == [("Code", 60, 60)]


@pytest.mark.parametrize("delta", [timedelta(days=15), timedelta(hours=-1)])
def test_a_sample_from_too_long_ago_or_from_the_future_is_taken_but_not_counted(
    computer_only: UsageServer, settings: Settings, delta: timedelta
) -> None:
    assert post_batch(computer_only, batch("Code", at=now_text(-delta))).status == 200

    assert computer_usage(settings) == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"junk",
        b"[]",
        b"\xff",
        json.dumps({"batch_id": "not-a-uuid", "samples": []}).encode(),
        json.dumps({"batch_id": str(uuid.uuid4()), "samples": []}).encode(),
        json.dumps(json.loads(batch("Code")) | {"extra": 1}).encode(),
        batch("   "),
        batch("x" * 300),
    ],
)
def test_a_batch_that_is_not_the_agents_records_nothing(computer_only: UsageServer, settings: Settings, body: bytes) -> None:
    assert post_batch(computer_only, body).status == 400
    assert computer_usage(settings) == []


def test_the_extension_records_sites(computer_only: UsageServer, settings: Settings) -> None:
    assert post_sites(computer_only, sites("www.YouTube.com", "github.com")).status == 200

    assert browser_usage(settings) == [("github.com", 0, 30), ("youtube.com", 30, 30)]


@pytest.mark.parametrize("origin", ["", "null", "http://evil.example", "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
def test_only_the_extension_can_record_sites(computer_only: UsageServer, settings: Settings, origin: str) -> None:
    body = sites("a.com")

    assert send(computer_only, "POST", "/browser/samples", {"Content-Type": JSON, "Origin": origin}, body).status == 403
    assert browser_usage(settings) == []


def test_a_website_cannot_record_the_computers_samples(computer_only: UsageServer, settings: Settings) -> None:
    headers = {"Content-Type": JSON, "Origin": "http://evil.example"}

    assert send(computer_only, "POST", "/computer/samples", headers, batch("Code")).status == 403
    assert computer_usage(settings) == []


@pytest.mark.parametrize("body", [b"junk", sites("a b"), sites("a.com/path"), sites("user@a.com")])
def test_sites_that_are_more_than_a_domain_record_nothing(computer_only: UsageServer, settings: Settings, body: bytes) -> None:
    assert post_sites(computer_only, body).status == 400
    assert browser_usage(settings) == []


def test_old_sites_are_taken_but_not_counted(computer_only: UsageServer, settings: Settings) -> None:
    assert post_sites(computer_only, sites("a.com", at=now_text(-timedelta(days=15)))).status == 200
    assert browser_usage(settings) == []


@pytest.mark.parametrize("path", ["/computer/samples", "/browser/samples"])
@pytest.mark.parametrize("content_type", ["", "text/plain", "application/x-www-form-urlencoded"])
def test_samples_must_say_they_are_json(computer_only: UsageServer, path: str, content_type: str) -> None:
    origin = {"Origin": EXTENSION_ORIGIN} if path.startswith("/browser") else {}

    assert send(computer_only, "POST", path, {"Content-Type": content_type} | origin, b"{}").status == 415


@pytest.mark.parametrize(("length", "status"), [("", 411), ("many", 411), ("-1", 413), (str(receiver.MAX_BODY_BYTES + 1), 413)])
def test_a_body_of_no_stated_or_too_great_a_length_is_not_read(computer_only: UsageServer, length: str, status: int) -> None:
    assert send(computer_only, "POST", "/computer/samples", {"Content-Type": JSON, "Content-Length": length}).status == status


def test_a_body_exactly_at_the_cap_is_read(computer_only: UsageServer) -> None:
    assert post_batch(computer_only, b"x" * receiver.MAX_BODY_BYTES).status == 400


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("host", ["", "attacker.example", "127.0.0.1"])
def test_an_address_other_than_its_own_is_refused(both: UsageServer, settings: Settings, method: str, host: str) -> None:
    path = "/" if method == "GET" else "/phone/open?app=A"
    reply = send(both, method, path, {"Host": host})

    assert reply.status == 421
    assert phone_events(settings) == []


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_no_other_method_is_answered(both: UsageServer, settings: Settings, method: str) -> None:
    assert send(both, method, "/phone/open?app=A").status == 501
    assert phone_events(settings) == []


@pytest.mark.parametrize("path", ["/nowhere", "/phone", "/computer/samples?x=1", "/browser/samples?x=1"])
def test_a_post_to_anywhere_else_finds_nothing(both: UsageServer, path: str) -> None:
    assert send(both, "POST", path, {"Content-Type": JSON}, b"{}").status == 404


@pytest.mark.parametrize("path", ["/nowhere", "/computer/samples", "/setup/", "/setup/../config.py", "/?x=1"])
def test_a_get_for_anything_else_finds_nothing(both: UsageServer, path: str) -> None:
    assert send(both, "GET", path).status == 404


def test_the_root_says_it_is_there(both: UsageServer) -> None:
    assert send(both, "GET", "/").text == "ok\n"


def test_every_answer_carries_the_protective_headers(both: UsageServer) -> None:
    answers = [
        send(both, "GET", "/"),
        send(both, "GET", "/nowhere"),
        send(both, "POST", "/phone/open"),
        send(both, "GET", "/", {"Host": "x"}),
    ]

    assert [answer.status for answer in answers] == [200, 404, 400, 421]
    for answer in answers:
        assert answer.headers["Cache-Control"] == "no-store"
        assert answer.headers["X-Content-Type-Options"] == "nosniff"
        assert answer.headers["Content-Type"] == "text/plain; charset=utf-8"
        assert answer.headers["Content-Length"] == str(len(answer.text.encode()))
        assert "python" not in answer.headers["Server"].lower()


def test_a_connection_from_neither_device_is_closed_unanswered(settings: Settings) -> None:
    for server in running(UsageServer((LOOPBACK, 0), settings, phone=ELSEWHERE, computer="100.100.100.101")):
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            send(server, "POST", "/phone/open?app=A")
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            send(server, "GET", "/")
    assert phone_events(settings) == []


@pytest.mark.parametrize(("path", "marker"), [(path, name) for path, name in SETUP_FILES.items()])
def test_the_computer_can_fetch_its_setup_files(computer_only: UsageServer, path: str, marker: str) -> None:
    reply = send(computer_only, "GET", path)

    assert reply.status == 200
    assert "__SERVER__" not in reply.text
    assert "__HOST__" not in reply.text
    source = (WINDOWS / marker).read_text(encoding="utf-8")
    assert reply.text == source.replace("__SERVER__", f"http://{computer_only.site}").replace("__HOST__", LOOPBACK)


def test_the_phone_cannot_fetch_the_setup_files(phone_only: UsageServer) -> None:
    for path in SETUP_FILES:
        assert send(phone_only, "GET", path).status == 403


def test_a_store_that_cannot_be_written_says_so_without_the_reason(
    computer_only: UsageServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def locked(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("a value that must not show")

    monkeypatch.setattr(Database, "add_computer_batch", locked)
    with caplog.at_level(logging.WARNING, logger=receiver.__name__):
        reply = post_batch(computer_only, batch("Code"))

    assert reply.status == 503
    assert reply.text == "try again\n"
    assert caplog.messages == ["usage could not be recorded (OperationalError)"]


def test_a_failed_write_leaves_the_batch_to_be_sent_again(
    computer_only: UsageServer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = batch("Code")
    original = Database.add_computer_batch

    def fails(*_: object, **__: object) -> bool:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(Database, "add_computer_batch", fails)
    assert post_batch(computer_only, body).status == 503
    monkeypatch.setattr(Database, "add_computer_batch", original)

    assert post_batch(computer_only, body).status == 200
    assert computer_usage(settings) == [("Code", 15, 15)]


def test_answering_the_root_creates_nothing(both: UsageServer, settings: Settings, tmp_path: Path) -> None:
    before = directory_digest(tmp_path)

    assert send(both, "GET", "/").status == 200

    assert directory_digest(tmp_path) == before
    assert not settings.database_path.exists()


def test_old_batch_ids_are_forgotten_but_recent_ones_kept(settings: Settings) -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    database = Database(settings.database_path)
    try:
        assert database.add_computer_batch("old", [], now - timedelta(days=30, seconds=1))
        assert database.add_computer_batch("kept", [], now - timedelta(days=30))
        assert database.add_computer_batch("new", [], now)
        ids = [str(row[0]) for row in database.connection.execute("SELECT id FROM computer_batches ORDER BY id")]
    finally:
        database.close()

    assert ids == ["kept", "new"]


def test_the_extensions_key_gives_the_origin_the_receiver_expects() -> None:
    manifest = json.loads((WINDOWS / "extension" / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]

    assert "chrome-extension://" + "".join(chr(ord("a") + int(character, 16)) for character in digest) == EXTENSION_ORIGIN


def test_the_extension_asks_for_no_more_than_it_needs_and_stays_out_of_private_windows() -> None:
    manifest = json.loads((WINDOWS / "extension" / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["incognito"] == "not_allowed"
    assert sorted(manifest["permissions"]) == ["alarms", "idle", "tabs"]
    assert manifest["host_permissions"] == ["http://__HOST__/*"]
    assert "content_scripts" not in manifest


def test_the_extension_sends_only_a_domain() -> None:
    script = (WINDOWS / "extension" / "background.js").read_text(encoding="utf-8")

    assert "parsed.hostname" in script
    for leak in ("pathname", "search", "tab.title", "parsed.href", "tab.url }"):
        assert leak not in script


@pytest.mark.parametrize("script", ["agent.ps1", "install.ps1"])
def test_the_powershell_is_plain_ascii(script: str) -> None:
    """Windows PowerShell reads a script with no byte-order mark in the local code page."""
    (WINDOWS / script).read_bytes().decode("ascii")


def test_the_agent_never_reads_a_window_title() -> None:
    agent = (WINDOWS / "agent.ps1").read_text(encoding="utf-8")

    assert "GetWindowText" not in agent
    assert "MainWindowTitle" not in agent


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("100.64.0.1", True),
        ("100.89.225.36", True),
        ("100.127.255.255", True),
        ("100.63.255.255", False),
        ("100.128.0.0", False),
        ("192.168.30.180", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_on_tailnet(address: str, expected: bool) -> None:
    assert on_tailnet(address) is expected


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("--host", ["192.168.30.180", "100.89.98.83", "100.74.58.93"]),
        ("--phone", ["100.89.225.36", "8.8.8.8", "100.74.58.93"]),
        ("--computer", ["100.89.225.36", "100.89.98.83", "0.0.0.0"]),
    ],
)
def test_serve_needs_every_address_on_the_tailnet(settings: Settings, name: str, arguments: list[str]) -> None:
    host, phone, computer = arguments

    with pytest.raises(ValueError, match=f"^{name} .* is not a Tailscale address"):
        receiver.serve(settings, host, 0, phone=phone, computer=computer)


def test_serve_listens_where_it_is_told_for_the_two_it_is_told(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served: list[UsageServer] = []
    monkeypatch.setattr(receiver, "on_tailnet", lambda _: True)
    monkeypatch.setattr(UsageServer, "serve_forever", lambda server: served.append(server))

    with caplog.at_level(logging.WARNING, logger=receiver.__name__):
        receiver.serve(settings, LOOPBACK, 0, phone="100.89.98.83", computer="100.74.58.93")

    assert len(served) == 1
    assert served[0].settings is settings
    assert (served[0].phone, served[0].computer) == ("100.89.98.83", "100.74.58.93")
    assert caplog.messages == [
        f"listening at http://{served[0].site}/ for the phone at 100.89.98.83 and the computer at 100.74.58.93"
    ]


def test_the_command_refuses_a_bad_address(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0", "--phone", "100.89.98.83", "--computer", "100.74.58.93"])

    assert result.exit_code == 2


def test_the_command_reports_an_address_it_cannot_listen_on(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    # An address inside the tailnet's range that no interface on the test machine holds.
    arguments = ["serve", "--host", "100.127.255.254", "--port", "8767", "--phone", "100.89.98.83", "--computer", "100.74.58.93"]
    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 1
    assert "cannot listen on 100.127.255.254:8767" in result.output
