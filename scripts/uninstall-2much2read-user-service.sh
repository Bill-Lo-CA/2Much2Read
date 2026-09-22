#!/bin/sh
set -eu

systemctl --user disable --now 2much2read-runtime.timer || true
# The run can take up to TimeoutStartSec=30min, so removing the unit files without stopping the
# service leaves a live process running from units that no longer exist.
systemctl --user stop 2much2read-runtime.service || true
service_status=0
systemctl --user is-active --quiet 2much2read-runtime.service || service_status=$?
case "$service_status" in
  3|4) ;;
  0)
    printf '%s\n' "refusing to remove unit files while 2much2read-runtime.service is active" >&2
    exit 1
    ;;
  *)
    printf '%s\n' "cannot determine whether 2much2read-runtime.service is active" >&2
    exit 1
    ;;
esac
rm -f "$HOME/.config/systemd/user/2much2read-runtime.service" "$HOME/.config/systemd/user/2much2read-runtime.timer"
systemctl --user daemon-reload
