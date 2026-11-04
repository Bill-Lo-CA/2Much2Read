#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

port=8766
while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      port="${2:-}"
      [ -n "$port" ] || {
        printf '%s\n' "--port requires a number" >&2
        exit 2
      }
      shift 2
      ;;
    --help|-h)
      printf '%s\n' "Usage: sh scripts/install-2busy1miss-todo-web.sh [--port PORT]"
      exit 0
      ;;
    *)
      printf '%s\n' "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

# The port is interpolated into a unit file, and the command refuses anything outside this range.
case "$port" in
  [1-9][0-9][0-9][0-9]|[1-9][0-9][0-9][0-9][0-9]) ;;
  *)
    printf '%s\n' "the port must be a number from 1024 to 65535, got '$port'" >&2
    exit 2
    ;;
esac
if [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
  printf '%s\n' "the port must be a number from 1024 to 65535, got '$port'" >&2
  exit 2
fi

exe="$repo_dir/.venv/bin/2busy1miss"
[ -x "$exe" ] || {
  printf '%s\n' "2busy1miss executable not found; run uv sync first" >&2
  exit 1
}
command -v tailscale >/dev/null 2>&1 || {
  printf '%s\n' "tailscale is required: the to-do page only listens on a Tailscale address" >&2
  exit 1
}
host=$(tailscale ip -4 | head -n1) || host=""
case "$host" in
  ''|*[!0-9.]*)
    printf '%s\n' "this machine has no Tailscale address; is Tailscale up?" >&2
    exit 1
    ;;
esac

reminders_file="$HOME/.config/2much2read-runtime/reminders.yaml"
database_dir="$HOME/.local/share/2much2read-runtime/2busy1miss"
systemd_dir="$HOME/.config/systemd/user"
unit="$systemd_dir/2busy1miss-todo-web.service"

[ -f "$reminders_file" ] || {
  printf '%s\n' "reminders configuration not found: $reminders_file; run install-2busy1miss-user-service.sh first" >&2
  exit 1
}
[ -d "$database_dir" ] || {
  printf '%s\n' "database directory not found: $database_dir; run install-2busy1miss-user-service.sh first" >&2
  exit 1
}

# The page cannot read the environment file, so the timezone it may hold is resolved here and written
# into the unit. env -i for the reason the main installer drops these names: the service reads the
# managed file, and a variable exported in this shell would outrank it. Only the exception's type is
# reported, since a ValidationError over the file quotes the value it rejected, which may be a secret.
timezone=$(env -i HOME="$HOME" "$repo_dir/.venv/bin/python" - <<'PY'
import sys

from two_busy_one_miss.config import Settings, load_reminders

try:
    settings = Settings()
    print(load_reminders(settings.reminders_config_path).timezone or settings.reminder_timezone)
except Exception as error:  # noqa: BLE001 - reported by type alone, see above
    print(f"the 2busy1miss settings could not be read ({type(error).__name__}); run `2busy1miss doctor`", file=sys.stderr)
    sys.exit(1)
PY
)
case "$timezone" in
  ''|/*|*..*|*[!A-Za-z0-9_+./-]*)
    printf '%s\n' "the timezone must name a system timezone, got '$timezone'" >&2
    exit 1
    ;;
esac

if [ -L "$unit" ]; then
  printf '%s\n' "refusing symbolic link at managed path: $unit" >&2
  exit 1
fi
mkdir -p "$systemd_dir"
sed -e "s|__EXECUTABLE__|$exe|" \
  -e "s|__REPO_DIR__|$repo_dir|g" \
  -e "s|__HOST__|$host|" \
  -e "s|__PORT__|$port|" \
  -e "s|__TIMEZONE__|$timezone|" \
  deploy/systemd/2busy1miss-todo-web.service > "$unit"

systemctl --user daemon-reload

printf '%s' "Start the to-do page now? [y/N] "
if ! IFS= read -r start_page; then
  start_page=""
fi
case "$start_page" in
  y|Y)
    systemctl --user enable 2busy1miss-todo-web.service
    # Not enable --now: a page already running would keep the code and the address it started with.
    systemctl --user restart 2busy1miss-todo-web.service
    page_status="Started the to-do page."
    ;;
  *)
    page_status="The to-do page is not started. Start it when ready: systemctl --user enable --now 2busy1miss-todo-web.service"
    ;;
esac

printf '%s\n' \
  "Page: http://$host:$port/ (from devices on the tailnet)" \
  "To link it from the agenda, set in $HOME/.config/2much2read-runtime/.2busy1miss.env:" \
  "TODO_WEB_URL=http://$host:$port/" \
  "Rerun this installer after changing the timezone. After deploying new code: systemctl --user restart 2busy1miss-todo-web.service" \
  "$page_status" \
  "Logs: journalctl --user -u 2busy1miss-todo-web.service"
