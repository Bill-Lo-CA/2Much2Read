#!/bin/sh
set -eu
systemctl --user disable --now 2much2read.timer || true
rm -f "$HOME/.config/systemd/user/2much2read.service" "$HOME/.config/systemd/user/2much2read.timer"
systemctl --user daemon-reload
