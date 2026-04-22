#!/bin/bash
set -euo pipefail

echo "=== Robo-Pet Setup ==="
echo ""

# Install base packages (idempotent - apt handles already-installed)
echo "[1/4] Installing base packages..."
sudo apt install -y git curl vim htop tmux python3-pip python3-venv

# Create workspace directories (idempotent - mkdir -p)
echo "[2/4] Creating workspace directories..."
mkdir -p ~/robot/{src,logs,venvs}

# Set up Python venv (idempotent - only creates if missing)
VENV_PATH="$HOME/robot/venvs/main"
echo "[3/4] Setting up Python venv at $VENV_PATH..."
if [[ ! -d "$VENV_PATH" ]]; then
    python3 -m venv "$VENV_PATH"
fi

# Upgrade pip and install base packages (idempotent - pip handles already-installed)
echo "[4/4] Installing Python packages..."
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip wheel setuptools
pip install numpy pyserial gpiozero

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To activate the venv:"
echo "    source ~/robot/venvs/main/bin/activate"
echo ""
echo "Workspace structure:"
echo "    ~/robot/src/    - your code"
echo "    ~/robot/logs/   - log files"
echo "    ~/robot/venvs/  - Python virtual environments"
