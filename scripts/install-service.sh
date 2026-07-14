#!/bin/bash
# Install one repo systemd unit. Exists because Ubuntu 26.04's sudo-rs forbids
# wildcards in sudoers command arguments, so the redeploy allowlist names this
# script instead of "install ... systemd/*.service".
set -euo pipefail
unit="$(basename "$1")"
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
install -m 0644 "$repo_dir/systemd/$unit" "/etc/systemd/system/$unit"
