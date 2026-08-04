#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

config_dir="$HOME/.config/2much2read-runtime"
env_file="$config_dir/.2bored1made.env"

if [ -L "$config_dir" ] || [ -L "$env_file" ]; then
  printf '%s\n' "refusing symbolic link at managed runtime path" >&2
  exit 1
fi
mkdir -p "$config_dir"
chmod 700 "$config_dir"
if [ ! -f "$env_file" ]; then
  cp config/2bored1made.env.example "$env_file"
fi
[ -f "$env_file" ] || {
  printf '%s\n' "managed path is not a regular file: $env_file" >&2
  exit 1
}
chmod 600 "$env_file"

printf '%s\n' \
  "Edit Discord delivery settings and allowed user IDs: $env_file" \
  "Send: cd $repo_dir && uv run 2bored1made send --message 'Hello' --mention DISCORD_USER_ID"
