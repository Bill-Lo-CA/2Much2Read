# 2Much2Read

Two local-first tools that post only their final output to a private Discord webhook:

- `2much2read` reads configured Gmail newsletters, summarizes them with local Ollama, and records digests in SQLite.
- `2busy1miss` syncs configured Google Calendar events into SQLite reminder jobs,
  then sends due reminders without repeatedly querying Google.

They are separate commands, OAuth clients, OAuth tokens, YAML files, SQLite databases, and environment files. They share only configuration-path resolution, Discord delivery, and a process lock implementation.

## Runtime files

Both tools use one private root, not the repository `.env`:

```text
~/.config/2much2read-runtime/
  .2much2read.env
  .2busy1miss.env
  gmail-client-secret.json
  gmail-token.json
  calendar-client-secret.json
  calendar-token.json
  sources.yaml
  reminders.yaml

~/.local/share/2much2read-runtime/
  2much2read.sqlite3
  2busy1miss.sqlite3
```

The two environment files may contain duplicate variable names because each command and systemd unit loads only its own file. Do not source both files in one shell. The installers set both runtime directories to mode `0700`, set copied environment/YAML files, OAuth credentials/tokens, and lock files to `0600`, and keep SQLite databases inside the protected data directory.

## Destructive reset

Existing 2Much2Read, newsletter-digest, and 2busy1miss runtime data is unsupported and is not migrated. Before installing this version, run `sh scripts/legacy_cleanup.sh`; it permanently deletes all listed configuration, OAuth credentials, tokens, and SQLite data. It removes exactly these legacy locations: `~/.config/2Much2Read`, `~/.config/2much2read`, `~/.config/newsletter-digest`, `~/.config/2busy1miss`, their matching `~/.local/share/` directories, the checkout `.env`, and the `newsletter-digest`, `2much2read`, and `2busy1miss` user unit files. The new `2much2read-runtime` roots and `*-runtime` units are not cleanup targets, so the script is safe to run again.

## 2much2read

Requirements: Gmail API desktop OAuth credentials, a Discord webhook, and local Ollama.

```bash
uv sync --all-groups
ollama pull llama3.2:3b
sh scripts/install-2much2read-user-service.sh \
  --gmail-client-secret ~/Downloads/gmail-client.json

uv run 2much2read auth gmail
uv run 2much2read doctor
uv run 2much2read run --dry-run
uv run 2much2read run
```

The installer copies the supplied client credential to `gmail-client-secret.json`, copies `config/2much2read.env.example` and `sources.yaml` on first install, then asks whether to enable `2much2read-runtime.timer`. Reply `y` only after configuration and authorization are ready; an empty response keeps it stopped and disabled. You can enable it later:

```bash
systemctl --user enable --now 2much2read-runtime.timer
```

`DIGEST_SCHEDULE_TIME` and `DIGEST_SCHEDULE_TIMEZONE` control the newsletter timer
(defaults: `08:00` and `America/Montreal`). After changing either setting, rerun the
installer to render the systemd timer. Manual CLI runs are unchanged.

Useful commands:

```bash
uv run 2much2read labels ensure
uv run 2much2read labels reconcile
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

The installer copies the supplied client credential to `calendar-client-secret.json`, copies `config/2busy1miss.env.example` and `reminders.yaml` on first install, then asks whether to enable both timers. Reply `y` only after configuration and authorization are ready; an empty response keeps both stopped and disabled. You can enable either timer later:

```bash
systemctl --user enable --now 2busy1miss-runtime.timer
systemctl --user enable --now 2busy1miss-runtime-agenda.timer
```

To stop active reminder/agenda jobs and remove only the installed units while preserving configuration, OAuth files, and SQLite data:

```bash
sh scripts/uninstall-2busy1miss-user-service.sh
```

`REMINDER_LOOKAHEAD_DAYS` in `.2busy1miss.env` controls the Calendar sync horizon
(default: 7; maximum: 366). `AGENDA_SCHEDULE_TIME` controls the daily next-day
agenda time in `HH:MM` format (default: `21:00`); after changing it, rerun the
installer to render the systemd timer. That agenda job reads the horizon and writes
one-time reminder jobs to SQLite. `2busy1miss-runtime.timer` runs every minute and
only dispatches those local jobs. To refresh jobs after adding or changing an event,
run `uv run 2busy1miss agenda-next-day`; an already delivered agenda is skipped,
but reminder jobs are reconciled.

Useful commands:

```bash
uv run 2busy1miss calendars list
uv run 2busy1miss discover --days 7
uv run 2busy1miss agenda 2026-07-16 --dry-run
uv run 2busy1miss agenda 2026-07-16 --force
uv run 2busy1miss agenda-next-day --dry-run
uv run 2busy1miss agenda-next-day --force
uv run 2busy1miss agenda-retry 2026-07-16
uv run 2busy1miss retry-delivery
```

Manual and next-day agendas use the same durable delivery record, de-duplicated by date, timezone, and Discord destination; `agenda-retry` retries failed records and `--force` is the explicit resend path. `2busy1miss-runtime-agenda.timer` runs at `AGENDA_SCHEDULE_TIME` in the user service manager's local timezone. It sends the next calendar day according to the configured reminder timezone and synchronizes the configured reminder horizon. Its persistent catch-up is ignored before that configured time, so a morning startup cannot send the next day's agenda early. Empty days are sent as `No events`. Reminder messages use the same Markdown code-block style as agendas; a retry after an event starts marks the job `expired` instead of sending it.

## Delivery behavior

Newsletter digests contain only items extracted in that run, so a source-specific
run cannot include older items or another source's items. `2much2read run
--no-deliver` stores the rendered digest as pending and reserves its daily key;
send it later with `uv run 2much2read delivery retry`. Durable digest, reminder,
and agenda deliveries checkpoint each confirmed Discord chunk, so a retry
only sends the remaining chunks.

## OAuth safety

Gmail and Calendar client secrets and user tokens are intentionally distinct. Give each installer its matching `--*-client-secret` path if the credentials came from different Google Cloud projects.

## Operations and recovery

OAuth consent screens left in Google test mode can invalidate refresh tokens. If a
scheduled command reports `AUTH_REAUTH_REQUIRED`, reauthorize interactively with
`uv run 2much2read auth gmail` or `uv run 2busy1miss auth calendar`; systemd jobs
never open a browser. For a remote host, create the callback tunnel on the local
machine before authorizing, then complete the printed URL in the local browser:

```bash
ssh -L 8765:127.0.0.1:8765 user@remote-host
```

Use SQLite's backup command rather than copying a live database file. Stop the
relevant timers and service before restoring; preserve the current database under
a new name, restore the backup at the configured path with mode `0600`, then run
`doctor` and a dry run before enabling a timer.

```bash
sqlite3 ~/.local/share/2much2read-runtime/2much2read.sqlite3 \
  ".backup '/secure-backups/2much2read.sqlite3'"
sqlite3 ~/.local/share/2much2read-runtime/2busy1miss.sqlite3 \
  ".backup '/secure-backups/2busy1miss.sqlite3'"
```

Inspect timers and recent failures without sending work:

```bash
systemctl --user status 2much2read-runtime.timer 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer
journalctl --user -u 2much2read-runtime.service -u 2busy1miss-runtime.service -u 2busy1miss-runtime-agenda.service -n 100 --no-pager
```

`LOCK_CONTENDED` means another local run is active; retry after it finishes.
`DISCORD_DELIVERY_FAILED` leaves a durable delivery pending, so use the relevant
retry command after fixing the webhook or network. `doctor` checks local setup
without posting unless `--send-test` is explicitly supplied.

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
