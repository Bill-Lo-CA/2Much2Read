from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError

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
    monkeypatch.setattr(oauth.Credentials, "from_authorized_user_file", MagicMock(return_value=credentials))
    monkeypatch.setattr(oauth.InstalledAppFlow, "from_client_secrets_file", create_flow)

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
    credentials.refresh.side_effect = RefreshError("invalid_grant")
    monkeypatch.setattr(oauth.Credentials, "from_authorized_user_file", MagicMock(return_value=credentials))

    with pytest.raises(ValueError, match="AUTH_REAUTH_REQUIRED.*2busy1miss auth calendar"):
        google_calendar.credentials(credentials_path, token_path)

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
    monkeypatch.setattr(oauth.Credentials, "from_authorized_user_file", MagicMock(return_value=credentials))

    assert gmail.credentials(credentials_path, token_path) is credentials
    assert token_path.read_text(encoding="utf-8") == "refreshed"
    assert token_path.stat().st_mode & 0o777 == 0o600


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


def test_token_status_distinguishes_missing_and_unusable_files(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"

    assert oauth.token_status(token_path, gmail.SCOPES) == "missing"
    token_path.write_text("{}", encoding="utf-8")
    assert oauth.token_status(token_path, gmail.SCOPES) == "reauth_required"
