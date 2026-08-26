#!/bin/sh
set -eu

systemctl --user disable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer || true
systemctl --user stop 2busy1miss-runtime.service 2busy1miss-runtime-agenda.service || true
service_status=0
systemctl --user is-active --quiet 2busy1miss-runtime.service 2busy1miss-runtime-agenda.service || service_status=$?
case "$service_status" in
  3|4) ;;
  0)
    printf '%s\n' "refusing to remove unit files while a 2busy1miss service is active" >&2
    exit 1
    ;;
  *)
    printf '%s\n' "cannot determine whether the 2busy1miss services are active" >&2
    exit 1
    ;;
esac
rm -f \
  "$HOME/.config/systemd/user/2busy1miss-runtime.service" \
  "$HOME/.config/systemd/user/2busy1miss-runtime.timer" \
  "$HOME/.config/systemd/user/2busy1miss-runtime-agenda.service" \
  "$HOME/.config/systemd/user/2busy1miss-runtime-agenda.timer"
systemctl --user daemon-reload
