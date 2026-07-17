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
python="$repo_dir/.venv/bin/python"
[ -x "$exe" ] && [ -x "$python" ] || {
  printf '%s\n' "2busy1miss executable not found; run uv sync first" >&2
  exit 1
}

systemctl --user disable --now 2busy1miss.timer 2busy1miss-agenda.timer 2>/dev/null || true
for service in 2busy1miss.service 2busy1miss-agenda.service; do
  if systemctl --user is-active --quiet "$service"; then
    printf '%s\n' "stop $service before migrating its files" >&2
    exit 1
  fi
done
if [ -n "$calendar_client_secret" ]; then
  "$python" -m two_much_two_read.migrate calendar \
    --legacy-env "$HOME/.config/2busy1miss/2busy1miss.env" \
    --calendar-client-secret "$calendar_client_secret"
else
  "$python" -m two_much_two_read.migrate calendar --legacy-env "$HOME/.config/2busy1miss/2busy1miss.env"
fi

config_dir="$HOME/.config/2much2read"
data_dir="$HOME/.local/share/2much2read"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_dir/.2busy1miss.env"
reminders_file="$config_dir/reminders.yaml"

mkdir -p "$config_dir" "$data_dir" "$systemd_dir"
chmod 700 "$config_dir" "$data_dir"

if [ ! -f "$env_file" ]; then
  cp config/2busy1miss.env.example "$env_file"
  chmod 600 "$env_file"
fi

if [ ! -f "$reminders_file" ]; then
  cp config/2busy1miss.reminders.example.yaml "$reminders_file"
  chmod 600 "$reminders_file"
fi

sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss.service > "$systemd_dir/2busy1miss.service"
cp deploy/systemd/2busy1miss.timer "$systemd_dir/2busy1miss.timer"
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss-agenda.service > "$systemd_dir/2busy1miss-agenda.service"
cp deploy/systemd/2busy1miss-agenda.timer "$systemd_dir/2busy1miss-agenda.timer"

systemctl --user daemon-reload

printf '%s\n' \
  "Config: $config_dir" \
  "Edit Discord webhook: $env_file" \
  "Authorize calendar: cd $repo_dir && uv run 2busy1miss auth calendar" \
  "Check setup: cd $repo_dir && uv run 2busy1miss doctor" \
  "Dry run: cd $repo_dir && uv run 2busy1miss run --dry-run" \
  "Agenda dry run: cd $repo_dir && uv run 2busy1miss agenda-next-day --dry-run" \
  "Enable reminders when ready: systemctl --user enable --now 2busy1miss.timer" \
  "Enable agenda when ready: systemctl --user enable --now 2busy1miss-agenda.timer" \
  "Logs: journalctl --user -u 2busy1miss.service"
