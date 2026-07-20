#!/bin/sh
set -eu
systemctl --user disable --now 2much2read-runtime.timer || true
rm -f "$HOME/.config/systemd/user/2much2read-runtime.service" "$HOME/.config/systemd/user/2much2read-runtime.timer"
systemctl --user daemon-reload
