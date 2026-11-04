#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

usage="Usage: sh scripts/install-2fun4distract-user-service.sh --phone ADDRESS --computer ADDRESS [--port PORT]"
phone=""
computer=""
port=8767

option_value() {
  [ -n "$2" ] || {
    printf '%s\n' "$1 requires a value" >&2
    exit 2
  }
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --phone)
      phone="${2:-}"
      option_value "$1" "$phone"
      shift 2
      ;;
    --computer)
      computer="${2:-}"
      option_value "$1" "$computer"
      shift 2
      ;;
    --port)
      port="${2:-}"
      option_value "$1" "$port"
      shift 2
      ;;
    --help|-h)
      printf '%s\n' "$usage"
      exit 0
      ;;
    *)
      printf '%s\n' "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

[ -n "$phone" ] && [ -n "$computer" ] || {
  printf '%s\n' "$usage" "The addresses are the devices' Tailscale addresses, as in \`tailscale status\`." >&2
  exit 2
}

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

exe="$repo_dir/.venv/bin/2fun4distract"
[ -x "$exe" ] || {
  printf '%s\n' "2fun4distract executable not found; run uv sync first" >&2
  exit 1
}
command -v tailscale >/dev/null 2>&1 || {
  printf '%s\n' "tailscale is required: the receiver only listens on a Tailscale address" >&2
  exit 1
}
host=$(tailscale ip -4 | head -n1) || host=""
case "$host" in
  ''|*[!0-9.]*)
    printf '%s\n' "this machine has no Tailscale address; is Tailscale up?" >&2
    exit 1
    ;;
esac

config_root="$HOME/.config/2much2read-runtime"
data_root="$HOME/.local/share/2much2read-runtime"
data_dir="$data_root/2fun4distract"
systemd_dir="$HOME/.config/systemd/user"
env_file="$config_root/.2fun4distract.env"
units="2fun4distract-receiver.service 2fun4distract-summary.service 2fun4distract-summary.timer"

reject_symlink() {
  if [ -L "$1" ]; then
    printf '%s\n' "refusing symbolic link at managed path: $1" >&2
    exit 1
  fi
}

for path in "$config_root" "$data_root" "$data_dir" "$env_file"; do
  reject_symlink "$path"
done
for unit in $units; do
  reject_symlink "$systemd_dir/$unit"
done

mkdir -p "$config_root" "$data_root" "$data_dir" "$systemd_dir"
chmod 700 "$config_root" "$data_root" "$data_dir"
if [ ! -f "$env_file" ]; then
  cp config/2fun4distract.env.example "$env_file"
fi
chmod 600 "$env_file"

# env -i because the service reads the managed file, and a variable exported in this shell would
# outrank it. Only the exception's type is reported: a ValidationError over the file quotes the value
# it rejected, which may be the webhook.
resolved=$(env -i HOME="$HOME" "$repo_dir/.venv/bin/python" - "$phone" "$computer" <<'PY'
import sys

from two_fun_four_distract.config import Settings
from two_read_runtime.tailnet import interpreter_root, on_tailnet

for address in sys.argv[1:]:
    if not on_tailnet(address):
        print(f"{address} is not a Tailscale address", file=sys.stderr)
        sys.exit(2)
try:
    settings = Settings()
except Exception as error:  # noqa: BLE001 - reported by type alone, see above
    print(f"the 2fun4distract settings could not be read ({type(error).__name__}); run `2fun4distract doctor`", file=sys.stderr)
    sys.exit(1)
print(f"time={settings.summary_schedule_time.strftime('%H:%M')}")
print(f"timezone={settings.summary_timezone}")
print(f"interpreter_root={interpreter_root(sys.executable, sys.base_prefix)}")
PY
)
schedule_time=$(printf '%s\n' "$resolved" | sed -n 's/^time=//p' | tail -n1)
schedule_timezone=$(printf '%s\n' "$resolved" | sed -n 's/^timezone=//p' | tail -n1)
interpreter_root=$(printf '%s\n' "$resolved" | sed -n 's/^interpreter_root=//p' | tail -n1)

# All of these are interpolated into a unit file, so they are checked again here whatever produced them.
case "$schedule_time" in
  [01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;;
  *)
    printf '%s\n' "SUMMARY_SCHEDULE_TIME must use HH:MM in $env_file" >&2
    exit 1
    ;;
esac
case "$schedule_timezone" in
  ''|/*|*..*|*[!A-Za-z0-9_+./-]*)
    printf '%s\n' "the summary timezone must name a system timezone, got '$schedule_timezone'" >&2
    exit 1
    ;;
esac

# Only an interpreter under home needs binding, since the sandbox hides nothing else it could be in.
# One whose directory also holds the credentials or the databases cannot be bound at all: it would
# bring them back into the sandbox, which is the one thing the sandbox is for.
refuse_interpreter() {
  printf '%s\n' "cannot sandbox the receiver with the Python at $interpreter_root: $1" >&2
  exit 1
}
interpreter_bind=""
case "$interpreter_root" in
  "$HOME"|"$HOME"/*)
    case "$interpreter_root" in
      *[!A-Za-z0-9_+./@-]*) refuse_interpreter "a unit file cannot name that path" ;;
    esac
    for private_dir in "$config_root" "$data_root"; do
      case "$private_dir" in
        "$interpreter_root"/*) refuse_interpreter "binding it would expose $private_dir; use one installed elsewhere" ;;
      esac
    done
    interpreter_bind=" $interpreter_root"
    ;;
esac

sed -e "s|__EXECUTABLE__|$exe|" \
  -e "s|__REPO_DIR__|$repo_dir|g" \
  -e "s|__HOST__|$host|" \
  -e "s|__PORT__|$port|" \
  -e "s|__PHONE__|$phone|" \
  -e "s|__COMPUTER__|$computer|" \
  -e "s| __INTERPRETER_ROOT__|$interpreter_bind|" \
  deploy/systemd/2fun4distract-receiver.service > "$systemd_dir/2fun4distract-receiver.service"
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/2fun4distract-summary.service > "$systemd_dir/2fun4distract-summary.service"
sed -e "s|__SUMMARY_SCHEDULE_TIME__|$schedule_time|" \
  -e "s|__SUMMARY_TIMEZONE__|$schedule_timezone|" \
  deploy/systemd/2fun4distract-summary.timer > "$systemd_dir/2fun4distract-summary.timer"

systemctl --user daemon-reload

printf '%s' "Start the receiver and the daily summary now? [y/N] "
if ! IFS= read -r start_now; then
  start_now=""
fi
case "$start_now" in
  y|Y)
    systemctl --user enable 2fun4distract-receiver.service 2fun4distract-summary.timer
    # Not enable --now: a receiver already running would keep the code and addresses it started with.
    systemctl --user restart 2fun4distract-receiver.service 2fun4distract-summary.timer
    start_status="Started the receiver and the summary timer."
    ;;
  *)
    start_status="Not started. When ready: systemctl --user enable --now 2fun4distract-receiver.service 2fun4distract-summary.timer"
    ;;
esac

printf '%s\n' \
  "Receiver: http://$host:$port/ (answers $phone and $computer only)" \
  "Discord settings: $env_file" \
  "Check setup: cd $repo_dir && uv run 2fun4distract doctor" \
  "Summary: $schedule_time $schedule_timezone. Rerun this installer after changing either." \
  "Phone: two automations, each a POST to http://$host:$port/phone/open?app= or /phone/close?app= ending in the Shortcut Input variable." \
  "Computer, in PowerShell there: irm http://$host:$port/setup/install.ps1 -OutFile \$env:TEMP\\install-2fun4distract.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File \$env:TEMP\\install-2fun4distract.ps1" \
  "After deploying new code: systemctl --user restart 2fun4distract-receiver.service" \
  "$start_status" \
  "Logs: journalctl --user -u 2fun4distract-receiver.service"
