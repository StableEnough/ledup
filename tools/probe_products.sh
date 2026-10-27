#!/bin/sh
# Find an unknown product ID: send Switch to each product, 5 s apart, and
# note the ID printed just before the tree toggles. Extra args go to send
# (e.g. --adapter hci0 or --dry-run).
set -e
cd "$(dirname "$0")/.."
for p in $(uv run python ledup.py products | cut -f1); do
  k=$(uv run python ledup.py commands --product "$p" | awk -F'\t' '$2=="Switch"{print $1; exit}')
  echo ">>> $p"
  uv run python ledup.py send --product "$p" --key "$k" "$@"
  sleep 5
done
