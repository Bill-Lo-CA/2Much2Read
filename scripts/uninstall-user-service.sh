#!/bin/sh
set -eu
systemctl --user disable --now newsletter-digest.timer || true
rm -f "$HOME/.config/systemd/user/newsletter-digest.service" "$HOME/.config/systemd/user/newsletter-digest.timer"
systemctl --user daemon-reload
