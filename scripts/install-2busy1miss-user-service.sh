#!/bin/sh
set -eu

client_secret=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --client-secret)
      client_secret="${2:-}"
      [ -n "$client_secret" ] || {
        printf '%s\n' "--client-secret requires a path" >&2
        exit 2
      }
      shift 2
      ;;
    --help|-h)
      printf '%s\n' "Usage: sh scripts/install-2busy1miss-user-service.sh [--client-secret PATH]"
      exit 0
      ;;
    *)
      printf '%s\n' "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -n "$client_secret" ] && [ ! -f "$client_secret" ]; then
  printf '%s\n' "client secret not found: $client_secret" >&2
  exit 1
fi

config_dir="$HOME/.config/2busy1miss"
data_dir="$HOME/.local/share/2busy1miss"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_dir/2busy1miss.env"
reminders_file="$config_dir/reminders.yaml"
target_secret="$config_dir/google-client-secret.json"

mkdir -p "$config_dir" "$data_dir" "$systemd_dir"
chmod 700 "$config_dir" "$data_dir"

if [ ! -f "$reminders_file" ]; then
  cp config/2busy1miss.reminders.example.yaml "$reminders_file"
  chmod 600 "$reminders_file"
fi

if [ ! -f "$env_file" ]; then
  cat > "$env_file" <<EOF
GOOGLE_CALENDAR_CREDENTIALS_PATH=$target_secret
GOOGLE_CALENDAR_TOKEN_PATH=$config_dir/google-calendar-token.json
REMINDERS_CONFIG_PATH=$reminders_file
DATABASE_PATH=$data_dir/2busy1miss.sqlite3
LOCK_PATH=$data_dir/2busy1miss.lock
DISCORD_WEBHOOK_URL=
DISCORD_USERNAME=2busy1miss
REMINDER_TIMEZONE=America/Montreal
REMINDER_LOOKAHEAD_DAYS=7
EOF
  chmod 600 "$env_file"
fi

if [ -n "$client_secret" ]; then
  cp "$client_secret" "$target_secret"
  chmod 600 "$target_secret"
fi

exe="$(command -v 2busy1miss)"
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2busy1miss.service > "$systemd_dir/2busy1miss.service"
cp deploy/systemd/2busy1miss.timer "$systemd_dir/2busy1miss.timer"

systemctl --user daemon-reload
systemctl --user enable --now 2busy1miss.timer

printf '%s\n' \
  "Config: $config_dir" \
  "Edit Discord webhook: $env_file" \
  "Authorize calendar: 2busy1miss auth calendar" \
  "Check timer: systemctl --user status 2busy1miss.timer" \
  "Logs: journalctl --user -u 2busy1miss.service"
