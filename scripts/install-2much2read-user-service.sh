#!/bin/sh
set -eu

repo_dir=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

. "$repo_dir/scripts/lib/systemd-units.sh"

gmail_client_secret=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --gmail-client-secret)
      gmail_client_secret="${2:-}"
      [ -n "$gmail_client_secret" ] || {
        printf '%s\n' "--gmail-client-secret requires a path" >&2
        exit 2
      }
      shift 2
      ;;
    --help|-h)
      printf '%s\n' "Usage: sh scripts/install-2much2read-user-service.sh [--gmail-client-secret PATH]"
      exit 0
      ;;
    *)
      printf '%s\n' "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -n "$gmail_client_secret" ] && [ ! -f "$gmail_client_secret" ]; then
  printf '%s\n' "Gmail client secret not found: $gmail_client_secret" >&2
  exit 1
fi

exe="$repo_dir/.venv/bin/2much2read"
[ -x "$exe" ] || {
  printf '%s\n' "2much2read executable not found; run uv sync first" >&2
  exit 1
}
command -v flock >/dev/null 2>&1 || {
  printf '%s\n' "flock is required to install 2much2read safely" >&2
  exit 1
}

config_root="$HOME/.config/2much2read-runtime"
config_dir="$config_root"
token_dir="$config_root/2much2read"
data_root="$HOME/.local/share/2much2read-runtime"
data_dir="$data_root/2much2read"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_dir/.2much2read.env"
sources_file="$config_dir/sources.yaml"
gmail_client_secret_file="$config_root/gmail-client-secret.json"
gmail_token_file="$token_dir/gmail-token.json"
database_file="$data_dir/2much2read.sqlite3"
lock_file="$data_dir/2much2read.lock"

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

# The timer is stopped much later, inside units_commit, so everything that can refuse the
# installation gets to refuse it while the schedule is still running.
units_require_systemd
units_require_inactive_service 2much2read-runtime.service

# The timer state is read here, before any file is touched, so a service manager that cannot answer
# aborts while everything is still where it was. Stopping the timer happens much later, in
# units_commit, once nothing left can refuse the installation.
units_init "$systemd_dir" 2much2read-runtime.service 2much2read-runtime.timer
units_trap
units_record_timer 2much2read-runtime.timer
read -r timer_was_enabled timer_was_active < "$units_dir/state.2much2read-runtime.timer"

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

for file in "$env_file" "$sources_file" "$gmail_client_secret_file"; do
  reject_symlink "$file"
done

check_migration "$config_root/gmail-token.json" "$gmail_token_file"
check_sqlite_migration "$data_root/2much2read.sqlite3" "$database_file"
migrate_file "$config_root/gmail-token.json" "$gmail_token_file"
migrate_sqlite "$data_root/2much2read.sqlite3" "$database_file"

if [ -n "$gmail_client_secret" ] && [ ! -f "$gmail_client_secret_file" ]; then
  cp "$gmail_client_secret" "$gmail_client_secret_file"
fi
if [ ! -f "$env_file" ]; then
  cp config/2much2read.env.example "$env_file"
fi
if [ ! -f "$sources_file" ]; then
  cp config/sources.example.yaml "$sources_file"
fi

for file in "$env_file" "$sources_file" "$gmail_client_secret_file" "$gmail_token_file" \
  "$database_file" "$database_file-wal" "$database_file-shm" "$database_file-journal" "$lock_file"; do
  repair_file "$file"
done

digest_schedule_time=$(sed -n 's/^DIGEST_SCHEDULE_TIME=//p' "$env_file" | tail -n 1)
digest_schedule_time=${digest_schedule_time:-08:00}
case "$digest_schedule_time" in
  [0-2][0-9]:[0-5][0-9]) ;;
  *)
    printf '%s\n' "DIGEST_SCHEDULE_TIME must use HH:MM" >&2
    exit 1
    ;;
esac
[ "${digest_schedule_time%:*}" -le 23 ] || {
  printf '%s\n' "DIGEST_SCHEDULE_TIME must use HH:MM" >&2
  exit 1
}
digest_schedule_timezone=$(sed -n 's/^DIGEST_SCHEDULE_TIMEZONE=//p' "$env_file" | tail -n 1)
digest_schedule_timezone=${digest_schedule_timezone:-America/Montreal}
case "$digest_schedule_timezone" in
  /*|*..*|*[!A-Za-z0-9_+./-]*)
    printf '%s\n' "DIGEST_SCHEDULE_TIMEZONE must name a system timezone" >&2
    exit 1
    ;;
esac
[ -f "/usr/share/zoneinfo/$digest_schedule_timezone" ] || {
  printf '%s\n' "DIGEST_SCHEDULE_TIMEZONE must name a system timezone" >&2
  exit 1
}

for unit in "$systemd_dir/2much2read-runtime.service" "$systemd_dir/2much2read-runtime.timer"; do
  reject_symlink "$unit"
done

units_render deploy/systemd/2much2read-runtime.service 2much2read-runtime.service \
  __EXECUTABLE__ "$(units_stage_executable "$exe")"
units_render deploy/systemd/2much2read-runtime.timer 2much2read-runtime.timer \
  __DIGEST_SCHEDULE_TIME__ "$digest_schedule_time" \
  __DIGEST_SCHEDULE_TIMEZONE__ "$digest_schedule_timezone"
units_commit

exec 9>&-

# Disabling a schedule that was already running is a change the operator did not ask for, so an
# upgrade offers to keep it and a first installation still defaults to leaving it off.
if [ "$timer_was_enabled" = enabled ]; then
  printf '%s' "Keep the 2much2read timer enabled? [Y/n] "
else
  printf '%s' "Enable 2much2read timer now? [y/N] "
fi
if ! IFS= read -r answer; then
  answer=""
fi
# Only an explicit answer changes anything. Both bits are restored otherwise, because a timer that
# was started without being enabled is still a schedule the operator is running.
case "$answer" in
  y | Y)
    desired_enabled=enabled
    desired_active=active
    [ "$timer_was_enabled" = enabled ] && desired_active=$timer_was_active
    ;;
  n | N)
    desired_enabled=disabled
    desired_active=inactive
    ;;
  *)
    desired_enabled=$timer_was_enabled
    desired_active=$timer_was_active
    ;;
esac
units_apply_timer_state 2much2read-runtime.timer "$desired_enabled" "$desired_active"
if [ "$desired_enabled" = enabled ]; then
  timer_status="Timer enabled."
else
  timer_status="Timer remains disabled. Enable when ready: systemctl --user enable --now 2much2read-runtime.timer"
fi

printf '%s\n' \
  "Config: $config_dir" \
  "Edit Discord delivery settings: $env_file" \
  "Authorize Gmail: cd $repo_dir && uv run 2much2read auth gmail" \
  "Check setup: cd $repo_dir && uv run 2much2read doctor" \
  "Dry run: cd $repo_dir && uv run 2much2read run --dry-run" \
  "After changing DIGEST_SCHEDULE_TIME or DIGEST_SCHEDULE_TIMEZONE, rerun this installer." \
  "$timer_status" \
  "Logs: journalctl --user -u 2much2read-runtime.service"
