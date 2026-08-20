# 2Much2Read

Three local-first tools that post only their final output to a private Discord destination:

- `2much2read` reads configured Gmail newsletters, extracts candidates with local Ollama, reranks them locally, and records digests in SQLite.
- `2busy1miss` syncs configured Google Calendar events into SQLite reminder jobs,
  then sends due reminders without repeatedly querying Google.
- `2bored1made` sends a direct local notification, with optional whitelisted Discord user mentions.

They are separate commands, OAuth clients, OAuth tokens, YAML files, SQLite databases, and environment files. They share only configuration-path resolution, Discord delivery, and a process lock implementation.

## Runtime files

The commands use one private root with app-specific token and data directories, not the repository `.env`:

```text
~/.config/2much2read-runtime/
  .2bored1made.env
  .2much2read.env
  .2busy1miss.env
  gmail-client-secret.json
  calendar-client-secret.json
  sources.yaml
  reminders.yaml
  2much2read/
    gmail-token.json
  2busy1miss/
    calendar-token.json

~/.local/share/2much2read-runtime/
  2much2read/
    2much2read.sqlite3
    2much2read.lock
  2busy1miss/
    2busy1miss.sqlite3
    2busy1miss.lock
```

The environment files may contain duplicate variable names because each command and systemd unit loads only its own file. Do not source them together. The installers set the shared config root and app directories to mode `0700`, repair managed environment/YAML/OAuth/token/database/sidecar/lock files to `0600`, and keep SQLite databases inside the matching protected data directory.

### Migration and custom paths

On the first install after this layout change, each installer checks that its service is inactive, then moves its existing root-level token and SQLite database, `-wal`, `-shm`, `-journal`, and lock into the matching app directory. Environment/YAML/client-secret files remain at the shared config root. It never overwrites a new token or runtime file: if an old and new copy both exist, installation stops with both paths named. Stop the service, resolve the conflict, and rerun the installer. Timer prompts and their disabled-by-default behavior are unchanged.

The runtime itself only ever resolves the app-scoped paths. A token, database, or lock left at the
pre-scoping root-level location is ignored, so run the installer to move it before the first run;
otherwise the app starts against empty state at the new path while the old files sit unused.

The systemd templates allow writes only to the matching app config and data directories. If an environment-file path override such as `DATABASE_PATH`, `*_TOKEN_PATH`, or `*_CONFIG_PATH` points elsewhere, manual CLI runs may work but the sandboxed service will not; keep overrides under those app directories or add a reviewed user-unit drop-in with the required `ReadWritePaths` exception.

The service sandbox is intentionally compatible with local PyTorch/GPU use: it omits `PrivateDevices` and `MemoryDenyWriteExecute`. Inspect the installed units with:

```bash
systemd-analyze --user security 2much2read-runtime.service
systemd-analyze --user security 2busy1miss-runtime.service
systemd-analyze --user security 2busy1miss-runtime-agenda.service
systemd-analyze --user verify ~/.config/systemd/user/2much2read-runtime.service
systemd-analyze --user verify ~/.config/systemd/user/2busy1miss-runtime.service
systemd-analyze --user verify ~/.config/systemd/user/2busy1miss-runtime-agenda.service
```

## Discord delivery

Each private environment file selects one mode; the default remains `webhook`:

```dotenv
# Select one: webhook, bot, or both.
DISCORD_DELIVERY_MODE=webhook
DISCORD_WEBHOOK_URL=
DISCORD_USERNAME=2much2read
DISCORD_BOT_TOKEN=
DISCORD_BOT_CHANNEL_ID=
```

`webhook` requires `DISCORD_WEBHOOK_URL`; `bot` requires a bot token and numeric
channel ID; `both` independently sends to each. Newsletter and Calendar deliveries
persist one checkpoint per destination, so a retry sends only a failed destination.
`2bored1made` intentionally remains stateless: it reports a partial result when one
destination fails, and a manual resend is required.

Webhook mode accepts only HTTPS Discord webhook URLs on official Discord hosts with
the standard `/api/webhooks/{id}/{token}` path. For `2much2read`, Ollama is local by
default and ignores proxy environment variables. Set `OLLAMA_ALLOW_REMOTE=true` only
for an HTTPS remote Ollama server; email and article content will then leave this
machine. `OLLAMA_TRUST_ENV=true` explicitly enables HTTPX proxy environment handling.

To use bot mode, create a Discord application and bot manually, invite it only to
the private server/channel it should write to, and grant `View Channel` and `Send
Messages`. This REST-only sender needs no Gateway connection or privileged intents.
Keep `DISCORD_BOT_TOKEN` in the installer-created `0600` environment file; never
place it in YAML, the repository, or command-line arguments. `doctor` validates the
configured mode without sending; `doctor --send-test` explicitly posts one test
message to each configured destination.

## Destructive reset

Runtime state lives entirely under `~/.config/2much2read-runtime` and
`~/.local/share/2much2read-runtime`. To start clean, stop the timers, then delete the app
directory under each root; the next run recreates empty state. Deleting a database discards
digest history but not Gmail processing state, which is tracked by the `NewsletterBot/Processed`
and `NewsletterBot/Failed` labels in Gmail, so already-processed mail is not re-analyzed unless
you pass `--force`.

## 2much2read

Requirements: Gmail API desktop OAuth credentials, a Discord webhook or bot, and local Ollama.

```bash
uv sync --all-groups
ollama pull llama3.2:3b
ollama pull qwen3:8b
sh scripts/install-2much2read-user-service.sh \
  --gmail-client-secret ~/Downloads/gmail-client.json

uv run 2much2read auth gmail
uv run 2much2read doctor
uv run 2much2read run --dry-run
uv run 2much2read run
```

The installer copies the supplied client credential to the shared config root, copies `config/2much2read.env.example` and `sources.yaml` on first install, then asks whether to enable `2much2read-runtime.timer`. Reply `y` only after configuration and authorization are ready; an empty response keeps it stopped and disabled. You can enable it later:

```bash
systemctl --user enable --now 2much2read-runtime.timer
```

`DIGEST_SCHEDULE_TIME` and `DIGEST_SCHEDULE_TIMEZONE` control the newsletter timer
(defaults: `08:00` and `America/Montreal`). After changing either setting, rerun the
installer to render the systemd timer. Manual CLI runs are unchanged.

Each run uses `OLLAMA_MODEL` to extract candidates, `RERANKER_MODEL` to rank them,
then `OLLAMA_REVIEW_MODEL` for final selection. The extractor is released before
the reranker loads, and the reranker is released before the reviewer starts, so the
three models never share memory. `RERANKER_DEVICE` defaults to `cpu`, which keeps the
reranker off the GPU entirely; set it to `cuda` only when the GPU has room to spare
alongside the reviewer. `DIGEST_RERANK_CANDIDATE_LIMIT` bounds how many candidates reach the
reranker, `DIGEST_REVIEW_CANDIDATE_LIMIT` bounds reviewer input, and `DIGEST_MAX_ITEMS` is the
final delivered-item limit. Each item includes its newsletter source in the Discord digest.

The digest has two sections. `DIGEST_MAX_ITEMS` headline items carry the full summary, reason, and
links, and `DIGEST_SECONDARY_ITEMS` (default 10) candidates the reviewer passed over follow as
one-line mentions under "其他值得注意", ordered by reranker score. Those candidates were already
extracted and ranked, so listing them costs nothing beyond the message length; set the value to 0
for a headline-only digest. `DIGEST_TOP_ITEMS` controls how many entries the renderer puts in the
headline section, so keep it equal to `DIGEST_MAX_ITEMS` unless you want mentions promoted into it.

Several newsletters cover the same story, and the reviewer drops the copies from its own selection,
which used to land them in the secondary section under the headline they duplicate. Repeat coverage
is now folded into the entry it duplicates: the strongest one keeps its place, the other newsletters
are listed alongside it, a link is borrowed from a merged entry when the headline's own newsletter
carried none, and a merged Hacker News entry keeps its discussion link, score, and comment count.
The secondary limit is applied after merging, so an absorbed mention frees its slot for the next
candidate instead of shrinking the section.

Two newsletters linking the same canonical article are folded together first, and that case needs
none of what follows: an identical URL is not a heuristic, so it holds in every digest language.
Everything below is for the harder case, where the same event reaches two newsletters as two
different pages.

Merging runs only for a Chinese digest, and `DIGEST_LANGUAGE` accepts only Chinese and English at
all — every stage is language-specific, and these are the ones the pipeline has been built and
measured for. Neither the canonical URL nor the title identifies a story across newsletters: each
translates a headline differently and links to a different page for one event. Matching therefore
runs on the tokens shaped like an identity — capitalised in the source, or carrying a version
number — in the title and summary, minus the tokens too widespread across the run's candidates to
identify anything. `DIGEST_MERGE_SIMILARITY` sets the threshold — strictly positive, since 0 reads
as "off" but would mean "merge on the token conditions alone" — and two conditions hold regardless
of it: two distinct shared tokens, and at least one of them shared by the two titles. Every
condition was added because the previous one let something through. Pairs covering one story shared two to five
tokens while every other pair shared at most one, always a bare vendor word like "openai". A summary
routinely names another story to compare against it, so body overlap alone merged an item about Grok
into one about GPT-5.6 Sol. And ordinary vocabulary only looks identifying in a language whose prose
is Latin script: with `DIGEST_LANGUAGE=en`, "OpenAI launches new AI model for coding" shares five
tokens with the same sentence about Google. Filtering by document frequency does not fix that — no
cutoff both keeps `gpt-5.6` and drops `model`, because a batch of fifteen items is far too small to
infer a stopword list — whereas shape separates them in both languages. Products written lowercase
in their own name are the known cost. Shape does not catch a generic acronym, though — `AI` is
capitalised, and two unrelated OpenAI products sharing `openai` and `ai` scored 0.5 — so tokens
appearing in more than 15% of the run's candidates are dropped as well. That frequency is measured
over every ranked candidate rather than the handful that reach merging: repeat coverage of one story
inflates its own identifying tokens, and over the merge input the same measurement inverts, putting
`gpt-5.6` at 26.7% against `ai` at 6.7%.

None of that survives an English digest, which is why merging is gated rather than tuned further.
The whole technique rests on Chinese prose leaving nothing but proper nouns in Latin script. English
prose leaves ordinary vocabulary there too, and three successive attempts each closed one class and
exposed the next: shape filtered lowercase words, frequency filtered a bare acronym, and Title Case
defeats both — "OpenAI Launches New AI Model for Coding" and the same sentence about Search share
five accepted tokens and score 0.714, with a frequency filter powerless because those words appear
only in that pair. Identifying stories across an English digest needs embeddings or entity
recognition, not another pattern.

Headline items are then rewritten from fuller text than the extractor ever saw. The extractor splits
one email into up to ten items, so each is written from a few lines and lands around 60 characters,
which is thin for the items leading the digest. `DIGEST_DEEPEN_HEADLINES` (default on) fetches each
headline's article and rewrites its summary and significance on the review model, falling back to the
merged newsletter coverage when there is no link or the fetch fails. A Hacker News self-post is
always rewritten from that fallback: it has no article, so its stored URL is the discussion page,
and extract_article cannot tell the author's post from the replies to it. Email bodies are never persisted
- only their hash and length - so the article is the only route back to fuller text. The rewrite is
discarded, keeping the original summary, when the fetch or the model fails, when the model reports
that the source text is not about this item, or when it answers in the wrong language. A headline
with no article and no merged coverage is skipped outright rather than rewritten: the fallback would
be its own summary, and a prompt asking for four to six sentences naming versions and numbers could
only be met from one sentence by padding or inventing. This adds
roughly one article fetch and one generation per headline. Selection hands the review model over
still loaded when the rewrite is enabled, and it stays resident across the headlines, so the rewrite
costs no model load; the extractor and reranker are already released by then, so the three models
still never share memory.

Reviewer input is split by category rather than taken as one global top-N.
`DIGEST_SECURITY_CANDIDATE_SLOTS` (default 7 of the 20) reserves slots for `SECURITY` items; the
rest go to the remaining categories. The reranker ranks AI releases above vulnerability
disclosures on a single scale — in one 100-candidate run only 3 of 23 SECURITY items reached a
global top 20, with named CVEs at ranks 35 and 43 — and prompt wording that lifts them demotes AI
and tooling stories by as much. Splitting the slots keeps both. Either group takes the other's
unused slots, so a quiet security day costs nothing.

Every reranked candidate is recorded in the append-only `reranker_scores` table with the model,
prompt version, and timestamp. Scores are stored exactly as the model produced them rather than
normalized, and the table carries no foreign key to `items` so the history survives reprocessing,
which deletes and re-inserts a document's item rows. Because SQLite reuses deleted row ids, join
audit rows on `scored_at` and `normalized_title` rather than on `item_id` alone.

The reviewer prompt is bounded against `OLLAMA_NUM_CTX` before it is sent. Ollama truncates an
oversized prompt from the head without erroring, which would silently drop the system prompt while
keeping the untrusted candidate text, so excess candidates are trimmed from the tail instead and
the injection guard is repeated after the candidate block.

On an 8 GB GPU the reviewer is the binding constraint: `qwen3:8b` at `OLLAMA_NUM_CTX=16384`
needs roughly 8 GB for Q4 weights plus an f16 KV cache, so Ollama offloads layers to the
CPU. Quantizing the KV cache on the Ollama server halves the cache cost:

```bash
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
```

KV quantization silently falls back to f16 on unsupported architectures, so confirm the
reported size with `ollama ps` rather than assuming the setting took effect.

Useful commands:

```bash
uv run 2much2read labels ensure
uv run 2much2read labels reconcile
uv run 2much2read filters ensure
uv run 2much2read mails list --source SOURCE_ID
uv run 2much2read delivery retry
uv run 2much2read delivery reset-checkpoint --delivery-id ID
```

### Hacker News sources

Add an enabled `type: hackernews` entry to `sources.yaml`; the disabled example
in `config/sources.example.yaml` is ready to copy. Supported feeds are
`topstories`, `beststories`, `newstories`, `askstories`, and `showstories`.
`max_story_candidates` is capped at 200, `max_articles_per_run` at 30, and
`max_age_hours` at 168. External stories enter a digest only after bounded,
SSRF-safe full-text extraction by default; set `allow_metadata_fallback: true`
only when a clearly marked metadata-only result is acceptable.

```bash
uv run 2much2read hackernews list --source hn-best
uv run 2much2read hackernews inspect --source hn-best --story-id ID --fetch-article
uv run 2much2read run --source hn-best --dry-run
```

## 2busy1miss

Requirements: Google Calendar API desktop OAuth credentials, a Discord webhook or bot, and local reminder rules.

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

The installer copies the supplied client credential to the shared config root, copies `config/2busy1miss.env.example` and `reminders.yaml` on first install, then asks whether to enable both timers. Reply `y` only after configuration and authorization are ready; an empty response keeps both stopped and disabled. You can enable either timer later:

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
uv run 2busy1miss reset-delivery-checkpoint --delivery-id ID
uv run 2busy1miss reset-agenda-checkpoint --delivery-id ID
```

Manual and next-day agendas use the same durable delivery record, de-duplicated by date, timezone, and Discord destination; `agenda-retry` retries failed records and `--force` is the explicit resend path. `2busy1miss-runtime-agenda.timer` runs at `AGENDA_SCHEDULE_TIME` in the user service manager's local timezone. It sends the next calendar day according to the configured reminder timezone and synchronizes the configured reminder horizon. Its persistent catch-up is ignored before that configured time, so a morning startup cannot send the next day's agenda early. Empty days are sent as `No events`. Reminder messages use the same Markdown code-block style as agendas; a retry after an event starts marks the job `expired` instead of sending it.

## 2bored1made

This is a direct notification skeleton: no database, retry queue, YAML hooks, or timer.

```bash
uv sync --all-groups
sh scripts/install-2bored1made.sh
# Edit ~/.config/2much2read-runtime/.2bored1made.env:
# DISCORD_DELIVERY_MODE=webhook
# DISCORD_WEBHOOK_URL=...
# DISCORD_ALLOWED_MENTION_IDS=123456789012345678
uv run 2bored1made send --message "Build failed" --mention 123456789012345678
```

Only user IDs listed in `DISCORD_ALLOWED_MENTION_IDS` can be tagged. Repeating
`--mention` tags more than one configured user; all other `@` text is neutralized.
Long notifications split only between complete mention tokens, never inside one.

## Delivery behavior

Newsletter digests contain only items extracted in that run, so a source-specific
run cannot include older items or another source's items. `2much2read run
--no-deliver` stores the rendered digest as pending and reserves its daily key;
send it later with `uv run 2much2read delivery retry`. Durable digest, reminder,
and agenda deliveries checkpoint each confirmed Discord chunk, so a retry
only sends the remaining chunks.

If a stored Discord checkpoint is corrupt, reset only its known failed destination record,
then run the usual retry command:

```bash
uv run 2much2read delivery reset-checkpoint --delivery-id ID
uv run 2busy1miss reset-delivery-checkpoint --delivery-id ID
uv run 2busy1miss reset-agenda-checkpoint --delivery-id ID
```

These commands accept only `DISCORD_MESSAGE_IDS_CORRUPT` failures and clear the
stored chunk IDs. The next retry may resend earlier Discord chunks.

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
sqlite3 ~/.local/share/2much2read-runtime/2much2read/2much2read.sqlite3 \
  ".backup '/secure-backups/2much2read.sqlite3'"
sqlite3 ~/.local/share/2much2read-runtime/2busy1miss/2busy1miss.sqlite3 \
  ".backup '/secure-backups/2busy1miss.sqlite3'"
```

Inspect timers and recent failures without sending work:

```bash
systemctl --user status 2much2read-runtime.timer 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer
journalctl --user -u 2much2read-runtime.service -u 2busy1miss-runtime.service -u 2busy1miss-runtime-agenda.service -n 100 --no-pager
```

`LOCK_CONTENDED` means another local run is active; retry after it finishes.
`DISCORD_DELIVERY_FAILED` leaves a durable destination delivery pending, so use the relevant
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
