# 2Much2Read

Two local-first tools that post only their final output to a private Discord webhook:

- `2much2read` reads configured Gmail newsletters, summarizes them with local Ollama, and records digests in SQLite.
- `2busy1miss` reads Google Calendar events, applies local YAML rules, and sends due reminders.

They are separate commands, OAuth clients, OAuth tokens, YAML files, SQLite databases, and environment files. They share only configuration-path resolution, Discord delivery, and a process lock implementation.

## Runtime files

Both tools use one private root, not the repository `.env`:

```text
~/.config/2much2read/
  .2much2read.env
  .2busy1miss.env
  gmail-client-secret.json
  gmail-token.json
  calendar-client-secret.json
  calendar-token.json
  sources.yaml
  reminders.yaml

~/.local/share/2much2read/
  2much2read.sqlite3
  2busy1miss.sqlite3
```

The two environment files may contain duplicate variable names because each command and systemd unit loads only its own file. Do not source both files in one shell. All runtime directories are mode `0700`; environment, OAuth, YAML, SQLite, and lock files are mode `0600`.

## 2much2read

Requirements: Gmail API desktop OAuth credentials, a Discord webhook, and local Ollama.

```bash
uv sync --all-groups
sh scripts/install-2much2read-user-service.sh \
  --gmail-client-secret ~/Downloads/gmail-client.json

uv run 2much2read auth gmail
uv run 2much2read doctor
uv run 2much2read run --dry-run
uv run 2much2read run
```

The installer moves the supplied client credential to `gmail-client-secret.json`, copies `config/2much2read.env.example` and `sources.yaml` on first install, and enables `2much2read.timer`.

Useful commands:

```bash
uv run 2much2read labels ensure
uv run 2much2read filters ensure
uv run 2much2read mails list --source SOURCE_ID
uv run 2much2read delivery retry
```

## 2busy1miss

Requirements: Google Calendar API desktop OAuth credentials, a Discord webhook, and local reminder rules.

```bash
uv sync --all-groups
sh scripts/install-2busy1miss-user-service.sh \
  --calendar-client-secret ~/Downloads/calendar-client.json

uv run 2busy1miss auth calendar
uv run 2busy1miss doctor
uv run 2busy1miss rules test --days 7
uv run 2busy1miss run --dry-run
uv run 2busy1miss run
```

The installer moves the supplied client credential to `calendar-client-secret.json`, copies `config/2busy1miss.env.example` and `reminders.yaml` on first install, and enables `2busy1miss.timer`.

Useful commands:

```bash
uv run 2busy1miss calendars list
uv run 2busy1miss discover --days 7
uv run 2busy1miss agenda 2026-07-16 --dry-run
uv run 2busy1miss retry-delivery
```

## OAuth safety

Gmail and Calendar client secrets and user tokens are intentionally distinct. Give each installer its matching `--*-client-secret` path if the credentials came from different Google Cloud projects.

## Development

```bash
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src scripts
uv run pytest -q
uv build
```

Live Gmail, Calendar, Ollama, and Discord checks require local secrets and are opt-in.
