#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/robot-pet"
VENV_PATH="$REPO_DIR/.venv"
LOG_DIR="/var/log/robot-pet"
REPO_HTTPS="https://github.com/astalwick/robot-pet"

echo "=== Robo-Pet Setup ==="
echo ""

# Install base packages (idempotent - apt handles already-installed)
echo "[1/6] Installing base packages..."
sudo apt install -y git curl vim htop tmux python3-pip python3-venv

# Create log directory (idempotent)
echo "[2/6] Creating log directory at $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$USER:$USER" "$LOG_DIR"

# SSH login welcome (dynamic MOTD on Debian / Raspberry Pi OS)
echo "[3/6] Installing login welcome message..."
sudo tee /etc/update-motd.d/99-robot-pet >/dev/null <<'MOTD'
#!/bin/bash
# Robot-pet welcome — runs on SSH login (pam_motd)

# Robot art (12 lines, padded to 24 chars)
art=(
"      |  |             "
"    .------.           "
"    | o  o |           "
"    |  __  |           "
"    |______|           "
"  .-|------|-.         "
"  | |      | |         "
"  | |      | |         "
"  | |      | |         "
"    | |  | |           "
"    | |  | |           "
"    | |  | |           "
"   [_]    [_]          "
)

# Build info lines
info=()
info+=("Project  __ROBOT_PET_REPO__")
info+=("Logs     __ROBOT_PET_LOG_DIR__")
info+=("Code     __ROBOT_PET_HOME__/robot-pet")
info+=("Python   __ROBOT_PET_HOME__/robot-pet/.venv")
info+=("")
info+=("--- $(hostname) ---")
info+=("Uptime   $(uptime -p 2>/dev/null || uptime | sed 's/.*up *//;s/,.*users.*//;s/  */ /g')")
info+=("Load     $(awk '{print $1,$2,$3}' /proc/loadavg)  (1 / 5 / 15 min)")
info+=("Memory   $(free -h | awk '/^Mem:/ {print $3 " used / " $2 " total (" $7 " avail)"}')")
info+=("Disk /   $(df -h / | awk 'NR==2 {print $3 " used / " $2 " total (" $5 " full)"}')")
if command -v vcgencmd >/dev/null 2>&1; then
  info+=("SoC temp $(vcgencmd measure_temp 2>/dev/null | sed 's/temp=//')")
fi
ips=$(hostname -I 2>/dev/null | awk '{gsub(/ /,", "); sub(/, $/,""); print}')
if [[ -n "$ips" ]]; then
  info+=("Addrs    $ips")
fi

# Print side by side
echo ""
max=${#art[@]}
(( ${#info[@]} > max )) && max=${#info[@]}
for ((i=0; i<max; i++)); do
  printf "%s %s\n" "${art[i]:-                        }" "${info[i]:-}"
done
echo ""
MOTD
sudo sed -i \
  -e "s|__ROBOT_PET_REPO__|$REPO_HTTPS|g" \
  -e "s|__ROBOT_PET_LOG_DIR__|$LOG_DIR|g" \
  -e "s|__ROBOT_PET_HOME__|$HOME|g" \
  /etc/update-motd.d/99-robot-pet
sudo chmod +x /etc/update-motd.d/99-robot-pet

# Set up Python venv (idempotent - only creates if missing)
echo "[4/6] Setting up Python venv at $VENV_PATH..."
if [[ ! -d "$VENV_PATH" ]]; then
  python3 -m venv "$VENV_PATH"
fi

# Upgrade pip and install base packages (idempotent - pip handles already-installed)
echo "[5/6] Installing Python packages..."
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip wheel setuptools
pip install numpy pyserial gpiozero evdev
pip install git+https://github.com/basicmicro/roboclaw_python.git

# Install and enable systemd services
echo "[6/6] Installing systemd services..."
sudo cp "$REPO_DIR/systemd/"*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable robot-brain.service
sudo systemctl restart robot-brain.service

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To activate the venv:"
echo "    source $VENV_PATH/bin/activate"
echo ""
echo "Logs directory: $LOG_DIR"
echo ""
echo "Service status:"
echo "    sudo systemctl status robot-brain"
echo "    journalctl -u robot-brain -f"
