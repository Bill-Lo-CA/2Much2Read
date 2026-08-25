#!/bin/sh
set -eu

repo_dir=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)

. "$repo_dir/scripts/lib/systemd-units.sh"

units_systemd_dir="$HOME/.config/systemd/user"

if ! units_teardown \
  2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer \
  2busy1miss-runtime.service 2busy1miss-runtime-agenda.service; then
  printf '%s\n' "refusing to remove unit files the service manager would not stop" >&2
  exit 1
fi

rm -f \
  "$units_systemd_dir/2busy1miss-runtime.service" \
  "$units_systemd_dir/2busy1miss-runtime.timer" \
  "$units_systemd_dir/2busy1miss-runtime-agenda.service" \
  "$units_systemd_dir/2busy1miss-runtime-agenda.timer"
systemctl --user daemon-reload
