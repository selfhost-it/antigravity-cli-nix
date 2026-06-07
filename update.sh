#!/usr/bin/env bash
# update.sh — Update antigravity-cli-nix to the latest upstream release.
#
# UNLIKE the other flakes in this repo, this updater is a plain deterministic
# script, not a `claude -p` bot. It needs no LLM and no nix-prefetch: the
# upstream auto-updater manifest already publishes the exact { version, url,
# sha512 } per platform, so we just fetch all four manifests, convert each
# sha512 straight to a Nix SRI hash, and rewrite sources.json.
#
# Requirements: bash, curl, jq, nix (with flakes), git.
#
# Usage:
#   ./update.sh            # fetch, bump, build, verify, commit, push
#   ./update.sh --dry-run  # show the new sources.json, change nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Enable flakes for every `nix` call without repeating CLI flags (and without
# the word-splitting hazard of a multi-word command variable).
export NIX_CONFIG="experimental-features = nix-command flakes"

# jq/curl live in this flake's devShell, not globally (NixOS). If jq is missing,
# re-exec ourselves inside `nix develop` (which provides jq + curl). The guard
# variable prevents an infinite loop if the devShell somehow lacks jq.
if ! command -v jq >/dev/null 2>&1 && [[ -z "${AGY_UPDATE_IN_SHELL:-}" ]]; then
  echo "jq not found — re-running inside the flake devShell..." >&2
  exec env AGY_UPDATE_IN_SHELL=1 nix develop "$SCRIPT_DIR" --command bash "$0" "$@"
fi

BASE="https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"

# Nix system  ->  upstream manifest platform name
declare -A MANIFEST=(
  [x86_64-linux]=linux_amd64
  [aarch64-linux]=linux_arm64
  [x86_64-darwin]=darwin_amd64
  [aarch64-darwin]=darwin_arm64
)
# Stable iteration order so the generated JSON is deterministic.
ORDER=(x86_64-linux aarch64-linux x86_64-darwin aarch64-darwin)

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

current_version="$(jq -r '.version' sources.json)"

# Build the new sources.json incrementally with jq.
new_json='{}'
new_version=""
for sys in "${ORDER[@]}"; do
  plat="${MANIFEST[$sys]}"
  manifest="$(curl -fsSL "$BASE/manifests/$plat.json")" || {
    echo "Fatal: failed to fetch manifest for $plat ($sys)." >&2
    exit 1
  }
  ver="$(jq -r '.version' <<<"$manifest")"
  url="$(jq -r '.url' <<<"$manifest")"
  sha_hex="$(jq -r '.sha512' <<<"$manifest")"

  if [[ -z "$ver" || -z "$url" || -z "$sha_hex" || "$sha_hex" == "null" ]]; then
    echo "Fatal: malformed manifest for $plat." >&2
    exit 1
  fi

  # Upstream ships hex sha512; Nix wants an SRI string.
  sri="$(nix hash convert --hash-algo sha512 --to sri "$sha_hex")"

  if [[ -z "$new_version" ]]; then
    new_version="$ver"
  elif [[ "$ver" != "$new_version" ]]; then
    echo "Warning: $plat reports version $ver, expected $new_version (release mid-roll?)." >&2
  fi

  new_json="$(jq \
    --arg sys "$sys" --arg url "$url" --arg hash "$sri" \
    '.platforms[$sys] = {url: $url, hash: $hash}' <<<"$new_json")"
done

new_json="$(jq --arg v "$new_version" '. + {version: $v} | {version, platforms}' <<<"$new_json")"

echo "Current version: $current_version"
echo "Upstream version: $new_version"

if [[ "$current_version" == "$new_version" ]]; then
  echo "antigravity-cli is up to date (v$current_version)."
  exit 0
fi

if $DRY_RUN; then
  echo "--- new sources.json (dry run, not written) ---"
  echo "$new_json"
  exit 0
fi

echo "$new_json" > sources.json
echo "Wrote sources.json for v$new_version."

echo "Building..."
nix build .#antigravity-cli

built_version="$(./result/bin/agy --version 2>&1 | head -1 | tr -d '[:space:]')"
if [[ "$built_version" != "$new_version" ]]; then
  echo "Fatal: built binary reports '$built_version', expected '$new_version'." >&2
  exit 1
fi
echo "Verified: agy --version -> $built_version"

# Pre-commit secret scan (hashes in sources.json are checksums, not secrets).
if git ls-files -z | xargs -0 grep -nIE \
  'BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key|secret[_-]?key|password[[:space:]]*=' 2>/dev/null; then
  echo "Fatal: potential secret found in tracked files — aborting before commit." >&2
  exit 1
fi

git add -A
git commit -m "Update Antigravity CLI to v$new_version"
GIT_SSH_COMMAND="ssh -i ~/.ssh/self-host-github" git push origin main
echo "Done: pushed v$new_version."
