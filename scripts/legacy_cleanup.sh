#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
systemd_dir="$HOME/.config/systemd/user"

for unit in \
  newsletter-digest.timer newsletter-digest.service \
  2much2read.timer 2much2read.service \
  2busy1miss.timer 2busy1miss.service \
  2busy1miss-agenda.timer 2busy1miss-agenda.service
do
  systemctl --user disable --now "$unit" 2>/dev/null || true
done

rm -rf \
  "$HOME/.config/2Much2Read" \
  "$HOME/.config/2much2read" \
  "$HOME/.config/newsletter-digest" \
  "$HOME/.config/2busy1miss" \
  "$HOME/.local/share/2Much2Read" \
  "$HOME/.local/share/2much2read" \
  "$HOME/.local/share/newsletter-digest" \
  "$HOME/.local/share/2busy1miss" \
  "$repo_dir/.env"
rm -f \
  "$systemd_dir/newsletter-digest.timer" \
  "$systemd_dir/newsletter-digest.service" \
  "$systemd_dir/2much2read.timer" \
  "$systemd_dir/2much2read.service" \
  "$systemd_dir/2busy1miss.timer" \
  "$systemd_dir/2busy1miss.service" \
  "$systemd_dir/2busy1miss-agenda.timer" \
  "$systemd_dir/2busy1miss-agenda.service"
systemctl --user daemon-reload
