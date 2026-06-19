#!/bin/sh
set -eu
target="$HOME/.config/systemd/user"
exe="$(command -v newsletter-digest)"
mkdir -p "$target"
sed "s|__EXECUTABLE__|$exe|" deploy/systemd/newsletter-digest.service > "$target/newsletter-digest.service"
cp deploy/systemd/newsletter-digest.timer "$target/newsletter-digest.timer"
systemctl --user daemon-reload
systemctl --user enable --now newsletter-digest.timer
printf '%s\n' 'systemctl --user status newsletter-digest.timer' 'journalctl --user -u newsletter-digest.service'
