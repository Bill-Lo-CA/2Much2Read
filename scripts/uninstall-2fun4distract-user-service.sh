#!/bin/sh
set -eu

systemctl --user disable --now 2fun4distract-receiver.service 2fun4distract-summary.timer || true
systemctl --user stop 2fun4distract-summary.service || true
service_status=0
systemctl --user is-active --quiet 2fun4distract-receiver.service 2fun4distract-summary.service || service_status=$?
case "$service_status" in
  3|4) ;;
  0)
    printf '%s\n' "refusing to remove unit files while a 2fun4distract service is active" >&2
    exit 1
    ;;
  *)
    printf '%s\n' "cannot determine whether the 2fun4distract services are active" >&2
    exit 1
    ;;
esac
rm -f \
  "$HOME/.config/systemd/user/2fun4distract-receiver.service" \
  "$HOME/.config/systemd/user/2fun4distract-summary.service" \
  "$HOME/.config/systemd/user/2fun4distract-summary.timer"
systemctl --user daemon-reload
