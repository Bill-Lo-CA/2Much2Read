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

agenda_schedule_time=$(sed -n 's/^AGENDA_SCHEDULE_TIME=//p' "$env_file" || :)
agenda_schedule_time=${agenda_schedule_time:-21:00}
case "$agenda_schedule_time" in
  [01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;;
  *)
    printf '%s\n' "AGENDA_SCHEDULE_TIME must use HH:MM in $env_file" >&2
    exit 1
    ;;
esac

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
sed "s|__AGENDA_SCHEDULE_TIME__|$agenda_schedule_time|" deploy/systemd/2busy1miss-runtime-agenda.timer > "$systemd_dir/2busy1miss-runtime-agenda.timer"

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
  "$timer_status"
[ -z "$agenda_status" ] || printf '%s\n' "$agenda_status"
printf '%s\n' "Logs: journalctl --user -u 2busy1miss-runtime.service"
