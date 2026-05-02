#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/robot-pet"
VENV_PATH="$REPO_DIR/.venv"
LOG_DIR="/var/log/robot-pet"
REPO_HTTPS="https://github.com/astalwick/robot-pet"

echo "=== Robo-Pet Setup ==="
echo ""

# Install base packages (idempotent - apt handles already-installed)
echo "[1/10] Installing base packages..."
sudo apt install -y git curl vim htop tmux python3-pip python3-venv

# Add user to dialout group for serial port access (idempotent)
echo "[2/10] Adding $USER to dialout group..."
sudo usermod -a -G dialout "$USER"

# Add user to input group for controller/gamepad access (idempotent)
echo "[3/10] Adding $USER to input group..."
sudo usermod -a -G input "$USER"

# Free UART from Bluetooth for RoboClaw serial (idempotent)
echo "[4/10] Configuring UART for RoboClaw..."
BOOT_CONFIG="/boot/firmware/config.txt"
if ! grep -q "^enable_uart=1" "$BOOT_CONFIG" 2>/dev/null; then
    echo "enable_uart=1" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "    Added enable_uart=1 to $BOOT_CONFIG"
else
    echo "    UART already enabled"
fi
if ! grep -q "^dtoverlay=disable-bt" "$BOOT_CONFIG" 2>/dev/null; then
    echo "dtoverlay=disable-bt" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "    Added dtoverlay=disable-bt to $BOOT_CONFIG"
else
    echo "    Bluetooth already disabled on UART"
fi

# Create log directory (idempotent)
echo "[5/10] Creating log directory at $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$USER:$USER" "$LOG_DIR"

# SSH login welcome (dynamic MOTD on Debian / Raspberry Pi OS)
echo "[6/10] Installing login welcome message..."
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

# Configure interactive Bash sessions to start in robot-pet with the venv active.
echo "[7/10] Configuring Bash login directory and venv..."
BASHRC="$HOME/.bashrc"
BASH_LOGIN_START="# >>> robot-pet login setup >>>"
BASH_LOGIN_END="# <<< robot-pet login setup <<<"
touch "$BASHRC"
awk -v start="$BASH_LOGIN_START" -v end="$BASH_LOGIN_END" '
  $0 == start { skip = 1; next }
  $0 == end { skip = 0; next }
  !skip { print }
' "$BASHRC" >"$BASHRC.tmp"
mv "$BASHRC.tmp" "$BASHRC"
cat >>"$BASHRC" <<'BASH_LOGIN'

# >>> robot-pet login setup >>>
cd "$HOME/robot-pet"
source .venv/bin/activate
# <<< robot-pet login setup <<<
BASH_LOGIN

# Set up Python venv (idempotent - only creates if missing)
echo "[8/10] Setting up Python venv at $VENV_PATH..."
if [[ ! -d "$VENV_PATH" ]]; then
  python3 -m venv "$VENV_PATH"
fi

# Upgrade pip and install base packages (idempotent - pip handles already-installed)
echo "[9/10] Installing Python packages..."
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip wheel setuptools
pip install numpy pyserial gpiozero evdev basicmicro

# Install and enable systemd services
echo "[10/10] Installing systemd services..."
sudo cp "$REPO_DIR/systemd/"*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable robot-brain.service
sudo systemctl enable gamepad-teleop.service
sudo systemctl restart robot-brain.service
sudo systemctl restart gamepad-teleop.service

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
echo "    sudo systemctl status gamepad-teleop"
echo "    journalctl -u gamepad-teleop -f"
