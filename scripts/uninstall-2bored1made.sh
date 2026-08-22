#!/bin/sh
set -eu

systemctl --user disable --now 2bored1made-runtime.timer || true
systemctl --user stop 2bored1made-runtime.service || true
rm -f \
  "$HOME/.config/systemd/user/2bored1made-runtime.service" \
  "$HOME/.config/systemd/user/2bored1made-runtime.timer"
systemctl --user daemon-reload
