#!/usr/bin/env bash
# Deterministic local updater. See ./update.sh --help and README.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export NIX_CONFIG="${NIX_CONFIG:-}"$'\n''extra-experimental-features = nix-command flakes'
if command -v python3 >/dev/null 2>&1; then
  exec python3 -B "$SCRIPT_DIR/scripts/update.py" "$@"
elif [[ -x /run/current-system/sw/bin/python3 ]]; then
  exec /run/current-system/sw/bin/python3 -B "$SCRIPT_DIR/scripts/update.py" "$@"
elif [[ "${SELFHOST_UPDATE_IN_SHELL:-}" != 1 ]]; then
  exec env SELFHOST_UPDATE_IN_SHELL=1 nix develop --no-write-lock-file "$SCRIPT_DIR" --command bash "$SCRIPT_DIR/update.sh" "$@"
else
  echo 'Python 3 is required to run the updater.' >&2
  exit 127
fi
