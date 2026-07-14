#!/bin/bash
set -euo pipefail

# Ubuntu Server has no mDNS until setup.sh installs avahi-daemon, so a
# fresh card is only reachable by IP: ./initialize-pi.sh 192.168.1.42 [branch]
PI_HOST="${1:-robot-pi.local}"
BRANCH="${2:-main}"
PI_USER="pi"
SSH_KEY="$HOME/.ssh/id_ed25519"
REPO_URL="git@github.com:astalwick/robot-pet.git"  # Adjust if needed
REMOTE_PATH="~/robot-pet"

echo "=== Robo-Pet Pi Initialization ==="
echo "Target: $PI_USER@$PI_HOST (branch: $BRANCH)"
echo ""

# Check that the SSH key exists locally
if [[ ! -f "$SSH_KEY" ]]; then
    echo "ERROR: SSH key not found at $SSH_KEY"
    exit 1
fi

# Check Pi is reachable
echo "[1/8] Checking Pi is reachable..."
if ! ssh -o ConnectTimeout=5 "$PI_USER@$PI_HOST" "echo 'Pi is up'" 2>/dev/null; then
    echo "ERROR: Cannot reach $PI_HOST"
    echo "       On a freshly flashed card, pass the Pi's IP: $0 192.168.1.42"
    exit 1
fi

# Copy GitHub SSH key to Pi
echo "[2/8] Copying GitHub SSH key to Pi..."
ssh "$PI_USER@$PI_HOST" "mkdir -p ~/.ssh && chmod 700 ~/.ssh"
scp "$SSH_KEY" "$PI_USER@$PI_HOST:~/.ssh/id_github"
scp "${SSH_KEY}.pub" "$PI_USER@$PI_HOST:~/.ssh/id_github.pub" 2>/dev/null || true
ssh "$PI_USER@$PI_HOST" "chmod 600 ~/.ssh/id_github"

# Configure SSH to use this key for GitHub
echo "[3/8] Configuring SSH for GitHub..."
ssh "$PI_USER@$PI_HOST" 'cat >> ~/.ssh/config << "EOF"

Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_github
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config 2>/dev/null || true'

# Add GitHub to known_hosts (avoid prompt on first git clone)
echo "[4/8] Adding GitHub to known_hosts..."
ssh "$PI_USER@$PI_HOST" 'ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null'

# Run apt update and upgrade. On Ubuntu, cloud-init and unattended-upgrades
# hold the apt lock on first boot, and needrestart prompts interactively.
echo "[5/8] Running apt update && apt full-upgrade (this may take a while)..."
ssh -t "$PI_USER@$PI_HOST" 'command -v cloud-init >/dev/null && cloud-init status --wait; sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt update && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt full-upgrade -y'

# Restore robot-local config (sensors.json, drive tuning, voice.env with API
# keys) from a local backup. These files live outside git; keep a copy of the
# Pi's ~/.config/robot-pet in ~/robot-pet-config on your Mac. Only copied when
# the Pi has no config yet, so re-runs never clobber newer robot state.
CONFIG_BACKUP_DIR="$HOME/robot-pet-config"
if [[ -d "$CONFIG_BACKUP_DIR" ]]; then
    if ssh "$PI_USER@$PI_HOST" "test -d ~/.config/robot-pet"; then
        echo "[6/8] Pi already has ~/.config/robot-pet; leaving it alone."
    else
        echo "[6/8] Restoring robot config from $CONFIG_BACKUP_DIR..."
        ssh "$PI_USER@$PI_HOST" "mkdir -p ~/.config/robot-pet && chmod 700 ~/.config/robot-pet"
        scp "$CONFIG_BACKUP_DIR"/* "$PI_USER@$PI_HOST:~/.config/robot-pet/"
    fi
else
    echo "[6/8] No $CONFIG_BACKUP_DIR on this machine; skipping config restore."
fi

# Clone repo and run setup.sh
echo "[7/8] Cloning repo and running setup.sh..."
ssh -t "$PI_USER@$PI_HOST" "
    if [[ -d $REMOTE_PATH ]]; then
        echo 'Repo already exists, updating to $BRANCH...'
        cd $REMOTE_PATH && git fetch --prune && git checkout $BRANCH && git pull
    else
        echo 'Cloning repo ($BRANCH)...'
        git clone -b $BRANCH $REPO_URL $REMOTE_PATH
    fi
    cd $REMOTE_PATH
    chmod +x setup.sh
    ./setup.sh
"

# Reboot to apply kernel updates, group membership, and start services cleanly
echo "[8/8] Rebooting Pi..."
ssh -t "$PI_USER@$PI_HOST" 'sudo reboot' || true

echo "    Waiting for Pi to come back online..."
sleep 10
while ! ssh -o ConnectTimeout=5 "$PI_USER@$PI_HOST" "echo 'Pi is back'" 2>/dev/null; do
    echo "    Still waiting..."
    sleep 5
done

echo ""
echo "=== Initialization complete! ==="
echo "SSH in with: ssh $PI_USER@$PI_HOST"
echo "Repo is at: $REMOTE_PATH"
echo ""
echo "Service status: ssh $PI_USER@$PI_HOST 'sudo systemctl status robot-brain'"
