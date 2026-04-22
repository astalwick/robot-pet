#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/robot-pet"
VENV_PATH="$REPO_DIR/.venv"
LOG_DIR="/var/log/robot-pet"
REPO_HTTPS="https://github.com/astalwick/robot-pet"

echo "=== Robo-Pet Setup ==="
echo ""

# Install base packages (idempotent - apt handles already-installed)
echo "[1/5] Installing base packages..."
sudo apt install -y git curl vim htop tmux python3-pip python3-venv

# Create log directory (idempotent)
echo "[2/5] Creating log directory at $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$USER:$USER" "$LOG_DIR"

# SSH login welcome (dynamic MOTD on Debian / Raspberry Pi OS)
echo "[3/5] Installing login welcome message..."
sudo tee /etc/update-motd.d/99-robot-pet >/dev/null <<'MOTD'
#!/bin/bash
# Robot-pet welcome — runs on SSH login (pam_motd)

cat <<'ART'
      |  |
    .------.
    | o  o |
    |  __  |
    |______|
  .-|------|-.
  | |      | |
  | |      | |
  | |      | |
    | |  | |
    | |  | |
    | |  | |
   [_]    [_]
ART

echo ""
echo "  Project  __ROBOT_PET_REPO__"
echo "  Logs     __ROBOT_PET_LOG_DIR__"
echo "  Code     /home/pi/robot-pet"
echo "  Python   /home/pi/robot-pet/.venv"
echo ""
echo "  --- $(hostname) ---"
echo "  Uptime   $(uptime -p 2>/dev/null || uptime | sed 's/.*up *//;s/,.*users.*//;s/  */ /g')"
echo "  Load     $(awk '{print $1,$2,$3}' /proc/loadavg)  (1 / 5 / 15 min)"
echo "  Memory   $(free -h | awk '/^Mem:/ {print $3 " used / " $2 " total (" $7 " avail)"}')"
echo "  Disk /   $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')"
if command -v vcgencmd >/dev/null 2>&1; then
  echo "  SoC temp $(vcgencmd measure_temp 2>/dev/null | sed "s/temp=//")"
fi
ips=$(hostname -I 2>/dev/null | awk '{gsub(/ /,", "); sub(/, $/,""); print}')
if [[ -n "$ips" ]]; then
  echo "  Addrs    $ips"
fi
echo ""
MOTD
sudo sed -i \
  -e "s|__ROBOT_PET_REPO__|$REPO_HTTPS|g" \
  -e "s|__ROBOT_PET_LOG_DIR__|$LOG_DIR|g" \
  -e "s|/home/pi|$HOME|g" \
  /etc/update-motd.d/99-robot-pet
sudo chmod +x /etc/update-motd.d/99-robot-pet

# Set up Python venv (idempotent - only creates if missing)
echo "[4/5] Setting up Python venv at $VENV_PATH..."
if [[ ! -d "$VENV_PATH" ]]; then
  python3 -m venv "$VENV_PATH"
fi

# Upgrade pip and install base packages (idempotent - pip handles already-installed)
echo "[5/5] Installing Python packages..."
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip wheel setuptools
pip install numpy pyserial gpiozero

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To activate the venv:"
echo "    source $VENV_PATH/bin/activate"
echo ""
echo "Logs directory: $LOG_DIR"
