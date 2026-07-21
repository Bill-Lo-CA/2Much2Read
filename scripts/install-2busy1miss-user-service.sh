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

systemctl --user disable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer 2>/dev/null || true
for service in 2busy1miss-runtime.service 2busy1miss-runtime-agenda.service; do
  if systemctl --user is-active --quiet "$service"; then
    printf '%s\n' "stop $service before installing" >&2
    exit 1
  fi
done

config_dir="$HOME/.config/2much2read-runtime"
data_dir="$HOME/.local/share/2much2read-runtime"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_dir/.2busy1miss.env"
reminders_file="$config_dir/reminders.yaml"

mkdir -p "$config_dir" "$data_dir" "$systemd_dir"
chmod 700 "$config_dir" "$data_dir"
if [ -n "$calendar_client_secret" ] && [ ! -f "$config_dir/calendar-client-secret.json" ]; then
  cp "$calendar_client_secret" "$config_dir/calendar-client-secret.json"
  chmod 600 "$config_dir/calendar-client-secret.json"
fi

if [ ! -f "$env_file" ]; then
  cp config/2busy1miss.env.example "$env_file"
  chmod 600 "$env_file"
fi

if [ ! -f "$reminders_file" ]; then
  cp config/2busy1miss.reminders.example.yaml "$reminders_file"
  chmod 600 "$reminders_file"
fi

agenda_schedule_time=$(sed -n 's/^AGENDA_SCHEDULE_TIME=//p' "$env_file" || :)
agenda_schedule_time=${agenda_schedule_time:-21:00}
case "$agenda_schedule_time" in
  [01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;;
  *)
    printf '%s\n' "AGENDA_SCHEDULE_TIME must use HH:MM in $env_file" >&2
    exit 1
    ;;
esac

sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss-runtime.service > "$systemd_dir/2busy1miss-runtime.service"
cp deploy/systemd/2busy1miss-runtime.timer "$systemd_dir/2busy1miss-runtime.timer"
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss-runtime-agenda.service > "$systemd_dir/2busy1miss-runtime-agenda.service"
sed "s|__AGENDA_SCHEDULE_TIME__|$agenda_schedule_time|" deploy/systemd/2busy1miss-runtime-agenda.timer > "$systemd_dir/2busy1miss-runtime-agenda.timer"

systemctl --user daemon-reload

printf '%s\n' \
  "Config: $config_dir" \
  "Edit Discord webhook: $env_file" \
  "Authorize calendar: cd $repo_dir && uv run 2busy1miss auth calendar" \
  "Check setup: cd $repo_dir && uv run 2busy1miss doctor" \
  "Dry run: cd $repo_dir && uv run 2busy1miss run --dry-run" \
  "Agenda dry run: cd $repo_dir && uv run 2busy1miss agenda-next-day --dry-run" \
  "Enable reminders when ready: systemctl --user enable --now 2busy1miss-runtime.timer" \
  "Enable agenda when ready: systemctl --user enable --now 2busy1miss-runtime-agenda.timer" \
  "Logs: journalctl --user -u 2busy1miss-runtime.service"
