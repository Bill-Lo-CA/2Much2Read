#!/bin/sh
set -eu

systemctl --user disable --now 2much2read-runtime.timer || true
# The run can take up to TimeoutStartSec=30min, so removing the unit files without stopping the
# service leaves a live process running from units that no longer exist.
systemctl --user stop 2much2read-runtime.service || true
rm -f "$HOME/.config/systemd/user/2much2read-runtime.service" "$HOME/.config/systemd/user/2much2read-runtime.timer"
systemctl --user daemon-reload
