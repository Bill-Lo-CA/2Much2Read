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
mkdir ~/.config/newsletter-digest
cp config/sources.example.yaml ~/.config/newsletter-digest/sources.yaml
chmod 600 .env ~/.config/newsletter-digest/sources.yaml
```

The paths in `.env.example` use `${HOME}`, which dotenv expands to the deployment user's home
directory. Keep the runtime directory at mode `0700` and all
credential, token, and environment files at `0600`. Never commit `.env`, Google JSON files, the
SQLite database, or real newsletter fixtures.

## Gmail OAuth

In Google Cloud, enable Gmail API, configure the OAuth consent screen, and create **Desktop app**
credentials. Save the downloaded JSON at `GMAIL_CREDENTIALS_PATH`. Projects left in Testing mode
may issue refresh tokens that expire after seven days; use the appropriate production publishing
status for persistent personal use.

Google normally downloads the desktop OAuth credential with a name similar to
`client_secret_123.apps.googleusercontent.com.json`. This is the OAuth client credential, not the
user token. Keep its original name if preferred and set the full path in `.env`:

```dotenv
GMAIL_CREDENTIALS_PATH=${HOME}/.config/newsletter-digest/client_secret_123.apps.googleusercontent.com.json
GMAIL_TOKEN_PATH=${HOME}/.config/newsletter-digest/google-token.json
```

Neither file belongs in Git. Transfer the client credential directly to Linux, then let the auth
command create `google-token.json` there:

```bash
scp client_secret_*.apps.googleusercontent.com.json USER@SERVER:~/.config/newsletter-digest/
chmod 600 ~/.config/newsletter-digest/client_secret_*.apps.googleusercontent.com.json
```

To let the application create a Gmail label and incoming-message filter, use a criteria dictionary
in the local `sources.yaml`:

```yaml
sources:
  - id: alphasignal
    name: AlphaSignal
    enabled: true
    category: AI
    gmail_query: 'label:ai-newsPaper from:news@alphasignal.ai'
    gmail_filter:
      label: ai-newsPaper
      criteria:
        from: news@alphasignal.ai
    max_items_per_email: 10
```

Filter criteria support `from`, `to`, `subject`, `query`, `negatedQuery`, `hasAttachment`,
`excludeChats`, `size`, and `sizeComparison`. Actions are intentionally fixed to adding the one
configured label; the application cannot configure archive, delete, or forwarding actions.
The shared category labels are `ai-newsPaper`, `cloud-data-newspaper`, `cyber-newspaper`,
`dev-newspaper`, and `product-business-newspaper`. Keep `from:` in each source query so newsletters
sharing a category remain distinguishable.

```bash
# When authorizing a remote Linux machine, run this locally first:
ssh -L 8765:localhost:8765 USER@SERVER

uv run newsletter-digest auth gmail
uv run newsletter-digest labels ensure
uv run newsletter-digest filters list
# Preview totals by topic, then apply category labels to existing messages matched by each source's sender.
uv run python scripts/backfill_category_labels.py
uv run python scripts/backfill_category_labels.py --apply
# Preview, then reset all local database state and Gmail processing labels for testing.
uv run python scripts/cleanup_test_environment.py
uv run python scripts/cleanup_test_environment.py --apply
uv run newsletter-digest discover mails --source alphasignal
uv run newsletter-digest discover --query 'label:ai-newsPaper from:news@alphasignal.ai'
uv run newsletter-digest discover subscriptions list
uv run newsletter-digest discover subscriptions --sync
uv run newsletter-digest discover subscriptions --sync --apply

# Inspect the sanitized text for one discover result without writing state or applying labels.
uv run newsletter-digest inspect --source alphasignal --id DISPLAY_ID

# Also send that sanitized text to Ollama and print the structured extraction.
uv run newsletter-digest inspect --source alphasignal --id DISPLAY_ID --extract
```

The application requests both `gmail.modify` and `gmail.settings.basic`. Running `auth gmail` with
an older token automatically opens the consent flow again when the settings scope is missing. A
successful flow replaces the token; the old token is not overwritten if authorization fails.
`labels ensure` creates the two processing labels, configured category labels, and filters
idempotently. Sender-specific queries combine `label:` with `from:` so sources sharing a category
remain distinct. Label lookup is case-insensitive to match Gmail's conflict behavior. Gmail filters
affect new matching messages and do not retroactively classify existing mail. `filters list` prints
the existing Gmail filter criteria and actions.

`backfill_category_labels.py` finds existing messages by each enabled source's sender. Its default
mode reports totals by category; `--apply` adds only the configured category labels.
`cleanup_test_environment.py` defaults to a preview. Its `--apply` mode acquires the process lock,
creates a mode-`0600` timestamped SQLite backup beside the database, clears local test state, and
removes only `NewsletterBot/Processed` and `NewsletterBot/Failed` from configured newsletter mail.
It preserves category labels, Gmail filters, OAuth credentials, and existing Discord messages.

`discover --query` runs an explicit Gmail query. `discover mails --source <id>` uses that source's
configured query. `discover subscriptions list` groups recent mail carrying `List-ID` or
`List-Unsubscribe` headers and marks sources already configured. `discover subscriptions --sync`
previews missing enabled YAML entries. Adding `--apply` prompts for one of five uppercase categories
or `EXCLUDED` for every candidate. Categorized entries are appended to `sources.yaml`; excluded entries
are stored in mode-`0600` `excluded-subscriptions.yaml` beside it and skipped by future list and sync runs.
Both files are validated before replacement. Subscription discovery reads message metadata only. The source ID `list` remains
reserved. All discovery commands print metadata only.
Subscription identity prefers standard `List-ID`, then a hashed provider list ID, then the complete
`From` mailbox. When one address sends multiple newsletters, sync builds a display-name query and
checks its Gmail results before prompting. Queries that return another sender identity, no messages,
or non-subscription mail are reported as `ambiguous` and are not written.
After observing the real sender, optionally strengthen the local
`sources.yaml` query with `from:`. The application never fetches links. Runtime processing modifies
only `NewsletterBot/Processed` and `NewsletterBot/Failed`; maintenance commands may add the category
labels explicitly configured in `sources.yaml`.

`inspect` searches up to 100 messages matching the source query and compares the privacy-safe
display ID printed by `discover`. It prints message headers, Gmail label IDs, MIME type, and the
sanitized text that would be sent to Ollama. `--extract` additionally calls Ollama and prints its
structured result. Inspect never writes SQLite state, applies Gmail labels, or sends Discord. Its
terminal output can contain private email text, so do not run it from systemd or save the output in
logs.

## Run

```bash
# Configuration and Ollama checks. Add --send-test only when a Discord test post is wanted.
uv run newsletter-digest doctor

# Gmail metadata-only connectivity check.
uv run newsletter-digest discover mails --source alphasignal --limit 1

# Gmail → MIME → Ollama without persistent database state, processed labels, or Discord delivery.
uv run newsletter-digest run --dry-run --source alphasignal --max-messages 1

# Persist one result and apply Gmail labels, but hold Discord delivery.
uv run newsletter-digest run --no-deliver --source alphasignal --max-messages 1

# Deliver the already stored digest without calling Gmail or Ollama again.
uv run newsletter-digest retry-delivery

uv run newsletter-digest backfill --days 7 --source alphasignal

# Reprocess a bounded number of messages even if they already have processed labels, then send a new digest.
uv run newsletter-digest run --force --source alphasignal --max-messages 1

# Reprocess and persist a new pending digest without sending it.
uv run newsletter-digest run --force --no-deliver --source alphasignal --max-messages 1

# Resend the latest stored digest without calling Gmail or Ollama.
uv run newsletter-digest run --resend
```

Replace `alphasignal` with an enabled `id` from your local `sources.yaml`. If `--source` is
omitted, the command processes every enabled source.
`--force` requires both `--source` and `--max-messages`; it replaces stored items for matching
messages and creates a new digest record. With `--no-deliver`, that record remains pending for
`retry-delivery`. `--max-messages` applies per source when multiple sources run; the rendered digest
still contains at most `DIGEST_MAX_ITEMS` items. Topic-based runs are not currently supported.
`--resend` cannot be combined with other run options and sends a newly recorded copy of the latest
digest directly from SQLite.

`labels ensure`, `filters list`, and `discover` are the current live Gmail API checks. `doctor` only
checks whether the Gmail token file exists; it does not make a Gmail API request. Dry run uses an in-memory
database and does not apply processing labels or send Discord messages. It may create missing
app-owned labels during startup, so run `labels ensure` first when that distinction matters.
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
uv run mypy src scripts
uv run pytest -q
uv build
uv run pip audit
```

The audit command requires network access and is intentionally not part of offline tests. Live
Gmail, Ollama, and Discord checks are opt-in and require local secrets.

## Recovery and troubleshooting

To isolate Ollama from the Gmail pipeline, test `/api/chat` directly. `think: false` prevents
Qwen from returning a separate reasoning trace:

```bash
curl -i http://127.0.0.1:11434/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "Reply only with: test successful"}],
    "stream": false,
    "think": false
  }'
```

- OAuth expired: rerun `newsletter-digest auth gmail`; check the consent-screen publishing status.
- Model missing or GPU fallback: run `ollama list`, `ollama pull qwen3:8b`, and inspect Ollama logs.
- Ollama grammar errors: the application removes `maxLength` from the generation schema because
  Ollama rejects large repetition limits; Pydantic still validates those limits after generation.
- Invalid model JSON: one repair is automatic; repeated failure returns `OLLAMA_SCHEMA_INVALID`.
  Source URLs are checked against both plain and Markdown-formatted links after URL normalization.
- Gmail query mismatch: use `discover`, then update only the local `sources.yaml` query.
- Discord rate limit/outage: run `retry-delivery`; Gmail and Ollama are not called again.
- Back up the SQLite database and OAuth token together while the service is stopped; restore with
  mode `0600`. Keep encrypted backups outside the repository.
