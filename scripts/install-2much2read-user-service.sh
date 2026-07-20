#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

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

systemctl --user disable --now 2much2read-runtime.timer 2>/dev/null || true
if systemctl --user is-active --quiet 2much2read-runtime.service; then
  printf '%s\n' "stop 2much2read-runtime.service before installing" >&2
  exit 1
fi

config_dir="$HOME/.config/2much2read-runtime"
data_dir="$HOME/.local/share/2much2read-runtime"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_dir/.2much2read.env"
sources_file="$config_dir/sources.yaml"

mkdir -p "$config_dir" "$data_dir" "$systemd_dir"
chmod 700 "$config_dir" "$data_dir"
if [ -n "$gmail_client_secret" ] && [ ! -f "$config_dir/gmail-client-secret.json" ]; then
  cp "$gmail_client_secret" "$config_dir/gmail-client-secret.json"
  chmod 600 "$config_dir/gmail-client-secret.json"
fi
if [ ! -f "$env_file" ]; then
  cp config/2much2read.env.example "$env_file"
  chmod 600 "$env_file"
fi
if [ ! -f "$sources_file" ]; then
  cp config/sources.example.yaml "$sources_file"
  chmod 600 "$sources_file"
fi

sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2much2read-runtime.service > "$systemd_dir/2much2read-runtime.service"
cp deploy/systemd/2much2read-runtime.timer "$systemd_dir/2much2read-runtime.timer"
systemctl --user daemon-reload

printf '%s\n' \
  "Config: $config_dir" \
  "Edit Discord webhook: $env_file" \
  "Authorize Gmail: cd $repo_dir && uv run 2much2read auth gmail" \
  "Check setup: cd $repo_dir && uv run 2much2read doctor" \
  "Dry run: cd $repo_dir && uv run 2much2read run --dry-run" \
  "Enable when ready: systemctl --user enable --now 2much2read-runtime.timer" \
  "Logs: journalctl --user -u 2much2read-runtime.service"
