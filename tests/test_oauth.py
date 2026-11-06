import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from two_busy_one_miss import google_calendar
from two_much_two_read import gmail
from two_read_runtime import oauth


def test_noninteractive_invalid_token_requires_explicit_reauthorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    token_path.write_text("{}", encoding="utf-8")
    credentials = MagicMock()
    credentials.has_scopes.return_value = False
    flow = MagicMock()
    create_flow = MagicMock(return_value=flow)
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", MagicMock(return_value=credentials))
    monkeypatch.setattr("two_read_runtime.oauth.InstalledAppFlow.from_client_secrets_file", create_flow)

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED.*2much2read auth gmail"):
        gmail.credentials(credentials_path, token_path)

    create_flow.assert_not_called()


def test_refresh_failure_requires_reauthorization_without_overwriting_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    token_path.write_text("original", encoding="utf-8")
    credentials = MagicMock(expired=True, refresh_token="refresh", valid=False)
    credentials.has_scopes.return_value = True
    credentials.refresh.side_effect = RefreshError("invalid_grant")  # type: ignore[no-untyped-call]
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", MagicMock(return_value=credentials))

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED.*2busy1miss auth calendar"):
        google_calendar.credentials(credentials_path, token_path)

    assert token_path.read_text(encoding="utf-8") == "original"


def test_refresh_transport_failure_is_retryable_without_overwriting_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    token_path.write_text("original", encoding="utf-8")
    credentials = MagicMock(expired=True, refresh_token="refresh", valid=False)
    credentials.has_scopes.return_value = True
    credentials.refresh.side_effect = TransportError("offline")  # type: ignore[no-untyped-call]
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", MagicMock(return_value=credentials))

    with pytest.raises(ValueError, match="AUTH_REFRESH_UNAVAILABLE"):
        gmail.credentials(credentials_path, token_path)

    assert token_path.read_text(encoding="utf-8") == "original"


def test_valid_refresh_replaces_token_atomically_with_private_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    token_path.write_text("original", encoding="utf-8")
    credentials = MagicMock(expired=True, refresh_token="refresh", valid=False)
    credentials.has_scopes.return_value = True
    credentials.to_json.return_value = "refreshed"

    def refresh(request: object) -> None:
        credentials.valid = True

    credentials.refresh.side_effect = refresh
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", MagicMock(return_value=credentials))

    assert gmail.credentials(credentials_path, token_path) is credentials
    assert token_path.read_text(encoding="utf-8") == "refreshed"
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert isinstance(credentials.refresh.call_args.args[0], Request)


def test_existing_permissive_token_is_repaired_before_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials_path = tmp_path / "client.json"
    token_path = tmp_path / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    token_path.write_text("original", encoding="utf-8")
    os.chmod(token_path, 0o644)
    credentials = MagicMock(valid=True, expired=False)
    credentials.has_scopes.return_value = True
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", MagicMock(return_value=credentials))

    assert gmail.credentials(credentials_path, token_path) is credentials
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_token_status_is_inspection_only_for_permissive_token(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    os.chmod(token_path, 0o644)

    assert oauth.token_status(token_path, gmail.SCOPES) == "reauth_required"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o644


def test_gmail_and_calendar_keep_credentials_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, Path, tuple[str, ...], str]] = []
    expected = MagicMock()

    def fake_load(
        credentials_path: Path,
        token_path: Path,
        scopes: tuple[str, ...],
        port: int,
        *,
        interactive: bool,
        auth_command: str,
        missing_credentials_code: str,
    ) -> MagicMock:
        assert port == 8765
        assert interactive
        assert missing_credentials_code.endswith("AUTH_REQUIRED")
        calls.append((credentials_path, token_path, scopes, auth_command))
        return expected

    monkeypatch.setattr(gmail, "load_credentials", fake_load)
    monkeypatch.setattr(google_calendar, "load_credentials", fake_load)
    gmail_client = tmp_path / "gmail-client.json"
    gmail_token = tmp_path / "gmail-token.json"
    calendar_client = tmp_path / "calendar-client.json"
    calendar_token = tmp_path / "calendar-token.json"

    assert gmail.credentials(gmail_client, gmail_token, interactive=True) is expected
    assert google_calendar.credentials(calendar_client, calendar_token, interactive=True) is expected
    assert calls == [
        (gmail_client, gmail_token, gmail.SCOPES, "2much2read auth gmail"),
        (calendar_client, calendar_token, google_calendar.SCOPES, "2busy1miss auth calendar"),
    ]


@pytest.mark.parametrize(("contents", "expected"), [(None, "missing"), ("{}", "reauth_required")])
def test_token_status_distinguishes_missing_and_unusable_files(tmp_path: Path, contents: str | None, expected: str) -> None:
    token_path = tmp_path / "token.json"

    if contents is not None:
        token_path.write_text(contents, encoding="utf-8")

    assert oauth.token_status(token_path, gmail.SCOPES) == expected


@pytest.mark.parametrize(
    ("scoped", "valid", "expired", "refresh_token", "expected"),
    [
        (False, True, False, "r", "reauth_required"),  # a token without every scope is reauthorized
        (True, True, False, None, "ok"),
        (True, False, True, "r", "refresh_needed"),
        (True, False, True, None, "reauth_required"),
        (True, False, False, "r", "reauth_required"),
    ],
)
def test_token_status_reads_scopes_validity_and_refreshability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
    valid: bool,
    expired: bool,
    refresh_token: str | None,
    expected: str,
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    credentials = MagicMock(valid=valid, expired=expired, refresh_token=refresh_token)
    credentials.has_scopes.return_value = scoped
    monkeypatch.setattr("two_read_runtime.oauth.Credentials.from_authorized_user_file", MagicMock(return_value=credentials))

    assert oauth.token_status(token_path, gmail.SCOPES) == expected
    credentials.has_scopes.assert_called_with(gmail.SCOPES)


OTHER_SCOPES = ("https://www.googleapis.com/auth/other",)


def _real_token(path: Path, scopes: tuple[str, ...]) -> None:
    # Nothing is mocked here: the mocks above never show that a token on disk is read, or which
    # scopes it is checked against.
    contents = {
        "token": "access",
        "refresh_token": "refresh",
        "client_id": "id",
        "client_secret": "secret",
        "scopes": [*scopes],
        # A token with no expiry is read as expired, which would send these to the network.
        "expiry": "2099-01-01T00:00:00Z",
    }
    path.write_text(json.dumps(contents), encoding="utf-8")
    path.chmod(0o600)


def _load(credentials_path: Path, token_path: Path, *, interactive: bool = False) -> Credentials:
    return oauth.load_credentials(
        credentials_path,
        token_path,
        gmail.SCOPES,
        8765,
        interactive=interactive,
        auth_command="2much2read auth gmail",
        missing_credentials_code="TEST_AUTH_REQUIRED",
    )


@pytest.mark.parametrize(("granted", "status"), [(gmail.SCOPES, "ok"), (OTHER_SCOPES, "reauth_required")])
def test_token_status_reads_a_real_token_against_the_scopes_asked_for(
    tmp_path: Path, granted: tuple[str, ...], status: str
) -> None:
    token_path = tmp_path / "token.json"
    _real_token(token_path, granted)

    assert oauth.token_status(token_path, gmail.SCOPES) == status


def test_a_real_token_with_every_scope_is_used(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    _real_token(token_path, gmail.SCOPES)

    credentials = _load(tmp_path / "client.json", token_path)

    assert credentials.token == "access"


def test_a_real_token_without_every_scope_is_not_used(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    _real_token(token_path, OTHER_SCOPES)

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED.*2much2read auth gmail"):
        _load(tmp_path / "client.json", token_path)


def test_a_token_not_there_yet_asks_for_authorization_and_creates_nothing(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED"):
        _load(tmp_path / "client.json", token_path)

    assert not token_path.exists()


def test_authorizing_without_a_client_secret_names_what_is_missing(tmp_path: Path) -> None:
    credentials_path = tmp_path / "client.json"

    with pytest.raises(ValueError, match=f"TEST_AUTH_REQUIRED: missing {credentials_path}"):
        _load(credentials_path, tmp_path / "token.json", interactive=True)


def test_a_token_is_written_beside_itself_and_private_before_it_takes_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.replace is atomic only within a filesystem, and a shared /tmp is no place for a token.
    token_path = tmp_path / "tokens" / "token.json"
    credentials = MagicMock()
    credentials.to_json.return_value = "written"
    seen: list[tuple[Path, int, str]] = []
    real_replace = os.replace

    def replace(source: Path, destination: Path) -> None:
        seen.append((Path(source), stat.S_IMODE(Path(source).stat().st_mode), Path(source).read_text(encoding="utf-8")))
        real_replace(source, destination)

    monkeypatch.setattr("two_read_runtime.oauth.os.replace", replace)

    oauth._write_token(token_path, credentials)

    ((source, mode, contents),) = seen
    assert source.parent == token_path.parent
    assert source.name.startswith(".token.json.")
    assert (mode, contents) == (0o600, "written")
    assert list(token_path.parent.iterdir()) == [token_path]
