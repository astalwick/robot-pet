#!/bin/bash
# Install one repo systemd unit. Exists because Ubuntu 26.04's sudo-rs forbids
# wildcards in sudoers command arguments, so the redeploy allowlist names this
# script instead of "install ... systemd/*.service".
set -euo pipefail
unit="$(basename "$1")"
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$repo_dir/systemd/$unit" ]]; then
  install -m 0644 "$repo_dir/systemd/$unit" "/etc/systemd/system/$unit"
else
  # The unit was deleted from the repo: stop it and take it out of systemd.
  # Tolerate a unit that is already gone so a retried redeploy still passes.
  echo "retiring $unit"
  systemctl stop "$unit" 2>/dev/null || true
  systemctl disable "$unit" 2>/dev/null || true
  rm -f "/etc/systemd/system/$unit"
  systemctl daemon-reload
fi
