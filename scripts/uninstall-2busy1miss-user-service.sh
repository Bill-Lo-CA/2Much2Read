#!/bin/sh
set -eu

systemctl --user disable --now 2busy1miss-runtime.timer 2busy1miss-runtime-agenda.timer || true
systemctl --user stop 2busy1miss-runtime.service 2busy1miss-runtime-agenda.service || true
rm -f \
  "$HOME/.config/systemd/user/2busy1miss-runtime.service" \
  "$HOME/.config/systemd/user/2busy1miss-runtime.timer" \
  "$HOME/.config/systemd/user/2busy1miss-runtime-agenda.service" \
  "$HOME/.config/systemd/user/2busy1miss-runtime-agenda.timer"
systemctl --user daemon-reload
