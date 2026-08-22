#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

. "$repo_dir/scripts/lib/systemd-units.sh"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h)
      printf '%s\n' "Usage: sh scripts/install-2bored1made.sh"
      exit 0
      ;;
    *)
      printf '%s\n' "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

exe="$repo_dir/.venv/bin/2bored1made"
[ -x "$exe" ] || {
  printf '%s\n' "2bored1made executable not found; run uv sync first" >&2
  exit 1
}
command -v flock >/dev/null 2>&1 || {
  printf '%s\n' "flock is required to install 2bored1made safely" >&2
  exit 1
}

config_root="$HOME/.config/2much2read-runtime"
data_root="$HOME/.local/share/2much2read-runtime"
data_dir="$data_root/2bored1made"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_root/.2bored1made.env"
nudges_file="$config_root/nudges.yaml"
database_file="$data_dir/2bored1made.sqlite3"
lock_file="$data_dir/2bored1made.lock"

reject_symlink() {
  if [ -L "$1" ]; then
    printf '%s\n' "refusing symbolic link at managed path: $1" >&2
    exit 1
  fi
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

units_require_systemd
units_require_inactive_service 2bored1made-runtime.service

# The timer state is read here, before any file is touched, so a service manager that cannot answer
# aborts while everything is still where it was. Stopping the timer happens much later, in
# units_commit, once nothing left can refuse the installation.
units_init "$systemd_dir" 2bored1made-runtime.service 2bored1made-runtime.timer
units_trap
units_record_timer 2bored1made-runtime.timer
read -r timer_was_enabled timer_was_active < "$units_dir/state.2bored1made-runtime.timer"

for directory in "$config_root" "$data_root" "$data_dir"; do
  reject_symlink "$directory"
done
mkdir -p "$config_root" "$data_root" "$data_dir" "$systemd_dir"
for directory in "$config_root" "$data_root" "$data_dir"; do
  chmod 700 "$directory"
done

repair_file "$lock_file"
exec 9>>"$lock_file"
chmod 600 "$lock_file"
flock -n 9 || {
  printf '%s\n' "runtime lock is held: $lock_file" >&2
  exit 1
}

for file in "$env_file" "$nudges_file"; do
  reject_symlink "$file"
done

if [ ! -f "$env_file" ]; then
  cp config/2bored1made.env.example "$env_file"
fi

if [ ! -f "$nudges_file" ]; then
  cp config/2bored1made.nudges.example.yaml "$nudges_file"
fi

for file in "$env_file" "$nudges_file" "$database_file" "$database_file-wal" \
  "$database_file-shm" "$database_file-journal" "$lock_file"; do
  repair_file "$file"
done

for unit in "$systemd_dir/2bored1made-runtime.service" "$systemd_dir/2bored1made-runtime.timer"; do
  reject_symlink "$unit"
done

units_render deploy/systemd/2bored1made-runtime.service 2bored1made-runtime.service \
  __EXECUTABLE__ "$(units_stage_executable "$exe")"
units_render deploy/systemd/2bored1made-runtime.timer 2bored1made-runtime.timer
units_commit

exec 9>&-

# An upgrade must not silently switch a working schedule off, so a timer that was already enabled
# stays enabled unless the answer says otherwise. A first installation still defaults to disabled.
if [ "$timer_was_enabled" = enabled ]; then
  prompt="Keep the nudge timer enabled? [Y/n] "
  default_enable=true
else
  prompt="Enable the nudge timer now? [y/N] "
  default_enable=false
fi
printf '%s' "$prompt"
if ! IFS= read -r answer; then
  answer=""
fi
case "$answer" in
  y | Y) enable_timer=true ;;
  n | N) enable_timer=false ;;
  *) enable_timer=$default_enable ;;
esac

if [ "$enable_timer" = true ]; then
  # Keeping an enabled timer must not restart one the operator had deliberately stopped, while a
  # newly enabled timer should start now.
  if [ "$timer_was_enabled" = enabled ]; then
    units_apply_timer_state 2bored1made-runtime.timer enabled "$timer_was_active"
  else
    units_apply_timer_state 2bored1made-runtime.timer enabled active
  fi
  timer_message="Timer enabled."
else
  timer_message="Timer disabled. Enable when ready: systemctl --user enable --now 2bored1made-runtime.timer"
fi

printf '%s\n' \
  "Config: $config_root" \
  "Edit Discord delivery settings and allowed user IDs: $env_file" \
  "Edit recurring nudges: $nudges_file" \
  "Check setup: cd $repo_dir && uv run 2bored1made doctor" \
  "Dry run: cd $repo_dir && uv run 2bored1made run --dry-run" \
  "Countdown: cd $repo_dir && uv run 2bored1made status" \
  "Send once: cd $repo_dir && uv run 2bored1made send --message 'Hello' --mention DISCORD_USER_ID" \
  "$timer_message" \
  "Logs: journalctl --user -u 2bored1made-runtime.service"
