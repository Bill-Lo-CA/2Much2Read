from __future__ import annotations

import os
import tempfile
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]


def token_status(token_path: Path, scopes: tuple[str, ...]) -> str:
    if not token_path.is_file():
        return "missing"
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path))  # type: ignore[no-untyped-call]
    except (OSError, ValueError):
        return "reauth_required"
    if not credentials.has_scopes(scopes):
        return "reauth_required"
    if credentials.valid:
        return "ok"
    return "refresh_needed" if credentials.expired and credentials.refresh_token else "reauth_required"


def _write_token(token_path: Path, credentials: Credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{token_path.name}.", dir=token_path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())  # type: ignore[no-untyped-call]
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, token_path)
    finally:
        temporary.unlink(missing_ok=True)


def load_credentials(
    credentials_path: Path,
    token_path: Path,
    scopes: tuple[str, ...],
    port: int,
    *,
    interactive: bool,
    auth_command: str,
    missing_credentials_code: str,
) -> Credentials:
    credentials: Credentials | None = None
    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path))  # type: ignore[no-untyped-call]
        except (OSError, ValueError):
            credentials = None
    if credentials and not credentials.has_scopes(scopes):  # type: ignore[no-untyped-call]
        credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())  # type: ignore[no-untyped-call]
        except (RefreshError, OSError, ValueError):
            credentials = None
        else:
            _write_token(token_path, credentials)
    if credentials and credentials.valid:
        return credentials
    if not interactive:
        raise ValueError(f"AUTH_REAUTH_REQUIRED: run '{auth_command}'")
    if not credentials_path.is_file():
        raise ValueError(f"{missing_credentials_code}: missing {credentials_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
    credentials = flow.run_local_server(port=port, access_type="offline", prompt="consent", open_browser=False)
    _write_token(token_path, credentials)
    return credentials
