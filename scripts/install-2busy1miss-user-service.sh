#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

calendar_client_secret=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --calendar-client-secret)
      calendar_client_secret="${2:-}"
      [ -n "$calendar_client_secret" ] || {
        printf '%s\n' "--calendar-client-secret requires a path" >&2
        exit 2
      }
      shift 2
      ;;
    --help|-h)
      printf '%s\n' "Usage: sh scripts/install-2busy1miss-user-service.sh [--calendar-client-secret PATH]"
      exit 0
      ;;
    *)
      printf '%s\n' "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -n "$calendar_client_secret" ] && [ ! -f "$calendar_client_secret" ]; then
  printf '%s\n' "Calendar client secret not found: $calendar_client_secret" >&2
  exit 1
fi

exe="$repo_dir/.venv/bin/2busy1miss"
[ -x "$exe" ] || {
  printf '%s\n' "2busy1miss executable not found; run uv sync first" >&2
  exit 1
}
command -v flock >/dev/null 2>&1 || {
  printf '%s\n' "flock is required to install 2busy1miss safely" >&2
  exit 1
}

config_root="$HOME/.config/2much2read-runtime"
config_dir="$config_root"
token_dir="$config_root/2busy1miss"
data_root="$HOME/.local/share/2much2read-runtime"
data_dir="$data_root/2busy1miss"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_dir/.2busy1miss.env"
reminders_file="$config_dir/reminders.yaml"
calendar_client_secret_file="$config_root/calendar-client-secret.json"
calendar_token_file="$token_dir/calendar-token.json"
database_file="$data_dir/2busy1miss.sqlite3"
lock_file="$data_dir/2busy1miss.lock"

file_exists() {
  [ -e "$1" ] || [ -L "$1" ]
}

reject_symlink() {
  if [ -L "$1" ]; then
    printf '%s\n' "refusing symbolic link at managed path: $1" >&2
    exit 1
  fi
}

check_migration() {
  reject_symlink "$1"
  reject_symlink "$2"
  if file_exists "$1" && file_exists "$2"; then
    printf '%s\n' "cannot migrate $1: old and new files both exist ($2); move one aside and retry" >&2
    exit 1
  fi
}

migrate_file() {
  if file_exists "$1"; then
    mv "$1" "$2"
  fi
}

check_sqlite_migration() {
  old_exists=false
  new_exists=false
  for suffix in "" -wal -shm -journal; do
    reject_symlink "$1$suffix"
    reject_symlink "$2$suffix"
    if file_exists "$1$suffix"; then
      old_exists=true
    fi
    if file_exists "$2$suffix"; then
      new_exists=true
    fi
  done
  if [ "$old_exists" = true ] && [ "$new_exists" = true ]; then
    printf '%s\n' "cannot migrate $1: old and new files both exist across SQLite database groups ($2); move one group aside and retry" >&2
    exit 1
  fi
}

migrate_sqlite() {
  for suffix in "" -wal -shm -journal; do
    migrate_file "$1$suffix" "$2$suffix"
  done
}

repair_file() {
  reject_symlink "$1"
  if [ -e "$1" ]; then
    [ -f "$1" ] || {
      printf '%s\n' "managed path is not a regular file: $1" >&2
      exit 1
    }
    chmod 600 "$1"
  fi
}

for timer in 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer; do
  timer_status=0
  systemctl --user is-active --quiet "$timer" || timer_status=$?
  case "$timer_status" in
    0|3)
      systemctl --user disable --now "$timer" || {
        printf '%s\n' "failed to stop and disable $timer" >&2
        exit 1
      }
      ;;
    4) ;;
    *)
      printf '%s\n' "cannot determine whether $timer is active" >&2
      exit 1
      ;;
  esac
done

for service in 2busy1miss-runtime.service 2busy1miss-runtime-agenda.service; do
  service_state=$(systemctl --user show --property=ActiveState --value "$service") || {
    printf '%s\n' "cannot determine whether $service is active" >&2
    exit 1
  }
  case "$service_state" in
    inactive|failed) ;;
    *)
      printf '%s\n' "stop $service before installing" >&2
      exit 1
      ;;
  esac
done

for directory in "$config_root" "$token_dir" "$data_root" "$data_dir"; do
  reject_symlink "$directory"
done
mkdir -p "$config_root" "$token_dir" "$data_root" "$data_dir" "$systemd_dir"
for directory in "$config_root" "$token_dir" "$data_root" "$data_dir"; do
  chmod 700 "$directory"
done

repair_file "$lock_file"
exec 9>>"$lock_file"
chmod 600 "$lock_file"
flock -n 9 || {
  printf '%s\n' "runtime lock is held: $lock_file" >&2
  exit 1
}

for file in "$env_file" "$reminders_file" "$calendar_client_secret_file"; do
  reject_symlink "$file"
done

check_migration "$config_root/calendar-token.json" "$calendar_token_file"
check_sqlite_migration "$data_root/2busy1miss.sqlite3" "$database_file"
migrate_file "$config_root/calendar-token.json" "$calendar_token_file"
migrate_sqlite "$data_root/2busy1miss.sqlite3" "$database_file"

if [ -n "$calendar_client_secret" ] && [ ! -f "$calendar_client_secret_file" ]; then
  cp "$calendar_client_secret" "$calendar_client_secret_file"
fi

if [ ! -f "$env_file" ]; then
  cp config/2busy1miss.env.example "$env_file"
fi

if [ ! -f "$reminders_file" ]; then
  cp config/2busy1miss.reminders.example.yaml "$reminders_file"
fi

for file in "$env_file" "$reminders_file" "$calendar_client_secret_file" "$calendar_token_file" \
  "$database_file" "$database_file-wal" "$database_file-shm" "$database_file-journal" "$lock_file"; do
  repair_file "$file"
done

# The timer used to carry no timezone, so it fired at that hour in whatever zone the manager runs
# in, while the command's own guard reads the same hour in the configured one. Where the two
# differed the run landed early, returned before_schedule, and no later run replaced it that day.
#
# Both halves of that hour are therefore read by the application rather than approximated in shell,
# so that a configuration the command accepts is never rejected here. sed gets all three of these
# wrong, and the timers are disabled by the time this runs, so a false rejection leaves the schedule
# off over a file the application reads without complaint:
#
#   AGENDA_SCHEDULE_TIME="21:00"          quotes are dotenv syntax, not part of the value
#   REMINDER_TIMEZONE=Europe/Berlin # local   a trailing comment, likewise
#   timezone: null                        absent to the YAML loader, the literal "null" to sed
#
# Which reminders file to read is itself a setting: REMINDERS_CONFIG_PATH moves it, and the
# scheduled command follows it. Reading the fixed path instead would put the timer in one zone and
# the before_schedule guard in another - the same disagreement, moved rather than removed.
#
# A file the application cannot read at all falls back to the defaults and a note on stderr, rather
# than stopping the install. Validating either config is not this script's job - `2busy1miss doctor`,
# which it points at below, reports the real problem - and failing here would leave the timers
# disabled over a file the installer never used to look at. The hour such an install lands on hardly
# matters: a command that cannot build its own Settings does not run either way, and the summary
# below says to rerun this script once the file is fixed.
agenda_schedule=$("$repo_dir/.venv/bin/python" - "$reminders_file" <<'PY'
import sys
from pathlib import Path

# Named lines rather than the first and second, so that anything else reaching stdout one day -
# an import-time notice from a dependency, say - cannot silently shift both values by one.
# The reason for a fallback goes to stderr, and names only the exception type: a ValidationError
# over this file quotes the value it rejected, and that value may be the Discord webhook or the
# bot token.


def warn(what_failed: str, error: BaseException, consequence: str) -> None:
    print(
        f"{what_failed} could not be read ({type(error).__name__}), so {consequence}. "
        "Run `2busy1miss doctor` to see what is wrong with it.",
        file=sys.stderr,
    )


settings = None
try:
    from two_busy_one_miss.config import Settings

    settings = Settings()
except Exception as error:  # noqa: BLE001 - the defaults are installed instead; doctor explains why
    warn(
        "the environment file",
        error,
        "AGENDA_SCHEDULE_TIME and REMINDER_TIMEZONE are both taken from their defaults, even if "
        "only one of them is at fault",
    )

schedule_time = "" if settings is None else settings.agenda_schedule_time.strftime("%H:%M")
reminders = Path(sys.argv[1]) if settings is None else settings.reminders_config_path
timezone = ""
try:
    from two_busy_one_miss.config import load_reminders

    timezone = load_reminders(reminders).timezone or ""
except Exception as error:  # noqa: BLE001 - same, for the reminders file
    warn(str(reminders), error, "the timer falls back to the timezone in the environment file")
if not timezone and settings is not None:
    timezone = settings.reminder_timezone
print(f"time={schedule_time}")
print(f"timezone={timezone}")
PY
)
agenda_schedule_time=$(printf '%s\n' "$agenda_schedule" | sed -n 's/^time=//p' | tail -n1)
agenda_schedule_timezone=$(printf '%s\n' "$agenda_schedule" | sed -n 's/^timezone=//p' | tail -n1)
agenda_schedule_time=${agenda_schedule_time:-21:00}
agenda_schedule_timezone=${agenda_schedule_timezone:-America/Montreal}

# Both values are interpolated into a unit file, so they are checked again here whatever produced
# them.
case "$agenda_schedule_time" in
  [01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;;
  *)
    printf '%s\n' "AGENDA_SCHEDULE_TIME must use HH:MM in $env_file" >&2
    exit 1
    ;;
esac
case "$agenda_schedule_timezone" in
  /*|*..*|*[!A-Za-z0-9_+./-]*)
    printf '%s\n' "the agenda timezone must name a system timezone, got '$agenda_schedule_timezone'" >&2
    exit 1
    ;;
esac
[ -f "/usr/share/zoneinfo/$agenda_schedule_timezone" ] || {
  printf '%s\n' "the agenda timezone must name a system timezone, got '$agenda_schedule_timezone'" >&2
  exit 1
}

for unit in \
  "$systemd_dir/2busy1miss-runtime.service" \
  "$systemd_dir/2busy1miss-runtime.timer" \
  "$systemd_dir/2busy1miss-runtime-agenda.service" \
  "$systemd_dir/2busy1miss-runtime-agenda.timer"; do
  reject_symlink "$unit"
done
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss-runtime.service > "$systemd_dir/2busy1miss-runtime.service"
cp deploy/systemd/2busy1miss-runtime.timer "$systemd_dir/2busy1miss-runtime.timer"
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss-runtime-agenda.service > "$systemd_dir/2busy1miss-runtime-agenda.service"
sed -e "s|__AGENDA_SCHEDULE_TIME__|$agenda_schedule_time|" \
  -e "s|__AGENDA_SCHEDULE_TIMEZONE__|$agenda_schedule_timezone|" \
  deploy/systemd/2busy1miss-runtime-agenda.timer > "$systemd_dir/2busy1miss-runtime-agenda.timer"

systemctl --user daemon-reload

exec 9>&-

printf '%s' "Enable reminder and agenda timers now? [y/N] "
if ! IFS= read -r enable_timers; then
  enable_timers=""
fi
case "$enable_timers" in
  y|Y)
    systemctl --user enable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer
    timer_status="Timers enabled."
    agenda_status=""
    ;;
  *)
    timer_status="Timers remain disabled. Enable reminders when ready: systemctl --user enable --now 2busy1miss-runtime.timer"
    agenda_status="Enable agenda when ready: systemctl --user enable --now 2busy1miss-runtime-agenda.timer"
    ;;
esac

printf '%s\n' \
  "Config: $config_dir" \
  "Edit Discord delivery settings: $env_file" \
  "Authorize calendar: cd $repo_dir && uv run 2busy1miss auth calendar" \
  "Check setup: cd $repo_dir && uv run 2busy1miss doctor" \
  "Dry run: cd $repo_dir && uv run 2busy1miss run --dry-run" \
  "Agenda dry run: cd $repo_dir && uv run 2busy1miss agenda-next-day --dry-run" \
  "Agenda timer: $agenda_schedule_time $agenda_schedule_timezone. Rerun this installer after changing either." \
  "$timer_status"
[ -z "$agenda_status" ] || printf '%s\n' "$agenda_status"
printf '%s\n' "Logs: journalctl --user -u 2busy1miss-runtime.service"
