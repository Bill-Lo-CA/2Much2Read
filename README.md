# Newsletter Digest

Local-first pipeline that reads configured Gmail newsletters, sends sanitized text to local
Ollama `qwen3:8b`, stores validated Traditional Chinese summaries in SQLite, and posts a daily
digest to a private Discord webhook. Email bodies, OAuth tokens, webhook URLs, and model
reasoning are not stored or logged.

```text
Gmail API → MIME sanitizer → local Ollama → Pydantic validation → SQLite → Discord webhook
```

## Prerequisites

- Linux with Python 3.11–3.13 and `uv`
- Ollama running at `http://127.0.0.1:11434`
- A Google Cloud desktop OAuth client with Gmail API enabled
- A private Discord channel incoming webhook

```bash
ollama pull qwen3:8b
ollama list
uv sync --all-groups
cp .env.example .env
cp config/sources.example.yaml ~/.config/newsletter-digest/sources.yaml
chmod 600 .env ~/.config/newsletter-digest/sources.yaml
```

Edit `.env` paths for the deployment user. Keep the runtime directory at mode `0700` and all
credential, token, and environment files at `0600`. Never commit `.env`, Google JSON files, the
SQLite database, or real newsletter fixtures.

## Gmail OAuth

In Google Cloud, enable Gmail API, configure the OAuth consent screen, and create **Desktop app**
credentials. Save the downloaded JSON at `GMAIL_CREDENTIALS_PATH`. Projects left in Testing mode
may issue refresh tokens that expire after seven days; use the appropriate production publishing
status for persistent personal use.

```bash
# When authorizing a remote Linux machine, run this locally first:
ssh -L 8765:localhost:8765 USER@SERVER

uv run newsletter-digest auth gmail
uv run newsletter-digest labels ensure
uv run newsletter-digest discover --query 'AlphaSignal newer_than:30d'
```

`discover` prints metadata only. After observing the real sender, optionally strengthen the local
`sources.yaml` query with `from:`. The application never fetches links or modifies non-
`NewsletterBot/` labels.

## Run

```bash
uv run newsletter-digest doctor
uv run newsletter-digest run --dry-run --source alphasignal --max-messages 1
uv run newsletter-digest run
uv run newsletter-digest retry-delivery
uv run newsletter-digest backfill --days 7 --source alphasignal
```

Dry run uses an in-memory database and does not change Gmail labels or send Discord messages.
Backfill does not deliver unless `--deliver` is supplied. Discord mentions are disabled on every
request.

## systemd user timer

Run the installer from an activated project environment so it can resolve the executable:

```bash
sh scripts/install-user-service.sh
systemctl --user status newsletter-digest.timer
journalctl --user -u newsletter-digest.service
```

To run while logged out, an administrator may explicitly enable lingering with
`loginctl enable-linger "$USER"`. The installer does not do this automatically.

## Development and validation

```bash
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv build
uv run pip audit
```

The audit command requires network access and is intentionally not part of offline tests. Live
Gmail, Ollama, and Discord checks are opt-in and require local secrets.

## Recovery and troubleshooting

- OAuth expired: rerun `newsletter-digest auth gmail`; check the consent-screen publishing status.
- Model missing or GPU fallback: run `ollama list`, `ollama pull qwen3:8b`, and inspect Ollama logs.
- Invalid model JSON: one repair is automatic; repeated failure remains retryable on a later run.
- Gmail query mismatch: use `discover`, then update only the local `sources.yaml` query.
- Discord rate limit/outage: run `retry-delivery`; Gmail and Ollama are not called again.
- Back up the SQLite database and OAuth token together while the service is stopped; restore with
  mode `0600`. Keep encrypted backups outside the repository.
