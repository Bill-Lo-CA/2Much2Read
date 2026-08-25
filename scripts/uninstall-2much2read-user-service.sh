#!/bin/sh
set -eu

repo_dir=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)

. "$repo_dir/scripts/lib/systemd-units.sh"

units_systemd_dir="$HOME/.config/systemd/user"

if ! units_teardown 2much2read-runtime.timer 2much2read-runtime.service; then
  printf '%s\n' "refusing to remove unit files the service manager would not stop" >&2
  exit 1
fi

rm -f \
  "$units_systemd_dir/2much2read-runtime.service" \
  "$units_systemd_dir/2much2read-runtime.timer"
systemctl --user daemon-reload
