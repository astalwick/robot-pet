#!/bin/bash
set -euo pipefail

PI_HOST="robot-pi.local"
PI_USER="pi"
SSH_KEY="$HOME/.ssh/id_ed25519"
REPO_URL="git@github.com:astalwick/robot-pet.git"  # Adjust if needed
REMOTE_PATH="~/robot-pet"

echo "=== Robo-Pet Pi Initialization ==="
echo "Target: $PI_USER@$PI_HOST"
echo ""

# Check that the SSH key exists locally
if [[ ! -f "$SSH_KEY" ]]; then
    echo "ERROR: SSH key not found at $SSH_KEY"
    exit 1
fi

# Check Pi is reachable
echo "[1/7] Checking Pi is reachable..."
if ! ssh -o ConnectTimeout=5 "$PI_USER@$PI_HOST" "echo 'Pi is up'" 2>/dev/null; then
    echo "ERROR: Cannot reach $PI_HOST"
    exit 1
fi

# Copy GitHub SSH key to Pi
echo "[2/7] Copying GitHub SSH key to Pi..."
ssh "$PI_USER@$PI_HOST" "mkdir -p ~/.ssh && chmod 700 ~/.ssh"
scp "$SSH_KEY" "$PI_USER@$PI_HOST:~/.ssh/id_github"
scp "${SSH_KEY}.pub" "$PI_USER@$PI_HOST:~/.ssh/id_github.pub" 2>/dev/null || true
ssh "$PI_USER@$PI_HOST" "chmod 600 ~/.ssh/id_github"

# Configure SSH to use this key for GitHub
echo "[3/7] Configuring SSH for GitHub..."
ssh "$PI_USER@$PI_HOST" 'cat >> ~/.ssh/config << "EOF"

Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_github
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config 2>/dev/null || true'

# Add GitHub to known_hosts (avoid prompt on first git clone)
echo "[4/7] Adding GitHub to known_hosts..."
ssh "$PI_USER@$PI_HOST" 'ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null'

# Run apt update and upgrade
echo "[5/7] Running apt update && apt full-upgrade (this may take a while)..."
ssh -t "$PI_USER@$PI_HOST" 'sudo apt update && sudo apt full-upgrade -y'

# Reboot and wait for Pi to come back
echo "[6/7] Rebooting Pi..."
ssh -t "$PI_USER@$PI_HOST" 'sudo reboot' || true  # Will disconnect, that's expected

echo "    Waiting for Pi to come back online..."
sleep 10  # Give it time to actually go down
while ! ssh -o ConnectTimeout=5 "$PI_USER@$PI_HOST" "echo 'Pi is back'" 2>/dev/null; do
    echo "    Still waiting..."
    sleep 5
done
echo "    Pi is back online!"

# Clone repo and run setup.sh
echo "[7/7] Cloning repo and running setup.sh..."
ssh -t "$PI_USER@$PI_HOST" "
    if [[ -d $REMOTE_PATH ]]; then
        echo 'Repo already exists, pulling latest...'
        cd $REMOTE_PATH && git pull
    else
        echo 'Cloning repo...'
        git clone $REPO_URL $REMOTE_PATH
    fi
    cd $REMOTE_PATH
    chmod +x setup.sh
    ./setup.sh
"

echo ""
echo "=== Initialization complete! ==="
echo "SSH in with: ssh $PI_USER@$PI_HOST"
echo "Repo is at: $REMOTE_PATH"
