#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/robot-pet"
VENV_PATH="$REPO_DIR/.venv"
LOG_DIR="/var/log/robot-pet"
REPO_HTTPS="https://github.com/astalwick/robot-pet"

echo "=== Robo-Pet Setup ==="
echo ""

# Install base packages (idempotent - apt handles already-installed)
echo "[1/14] Installing base packages..."
sudo apt install -y git curl vim htop tmux python3-pip python3-venv python3-picamera2 python3-opencv opencv-data alsa-utils sox portaudio19-dev i2c-tools

# Add user to dialout group for serial port access (idempotent)
echo "[2/14] Adding $USER to dialout group..."
sudo usermod -a -G dialout "$USER"

# I2C for ToF sensors and future IMU (idempotent)
echo "[3/14] Enabling I2C and adding $USER to i2c group..."
BOOT_CONFIG="/boot/firmware/config.txt"
if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG" 2>/dev/null; then
    echo "dtparam=i2c_arm=on" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "    Added dtparam=i2c_arm=on to $BOOT_CONFIG (reboot required)"
else
    echo "    I2C already enabled in $BOOT_CONFIG"
fi
sudo usermod -a -G i2c "$USER"

# Add user to input and audio groups for controller/gamepad and ReSpeaker access (idempotent)
echo "[4/14] Adding $USER to input and audio groups and configuring ReSpeaker USB access..."
sudo usermod -a -G input "$USER"
sudo usermod -a -G audio "$USER"
sudo tee /etc/udev/rules.d/99-robot-pet-respeaker.rules >/dev/null <<'UDEV'
SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="001e", GROUP="audio", MODE="0660"
UDEV
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=2886 --attr-match=idProduct=001e

# Free UART from Bluetooth for RoboClaw serial (idempotent)
echo "[5/14] Configuring UART for RoboClaw..."
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
echo "[6/14] Creating log directory at $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$USER:$USER" "$LOG_DIR"

# SSH login welcome (dynamic MOTD on Debian / Raspberry Pi OS)
echo "[7/14] Installing login welcome message..."
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
echo "[8/14] Configuring Bash login directory, venv, and dashboard autostart..."
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
if [[ $- == *i* && -n "${SSH_TTY:-}" && -z "${ROBOT_PET_NO_DASHBOARD:-}" && -z "${ROBOT_PET_DASHBOARD_STARTED:-}" ]]; then
  export ROBOT_PET_DASHBOARD_STARTED=1
  echo "Starting Robo-Pet dashboard. Press q to exit to shell."
  python src/robot_dashboard.py
fi
# <<< robot-pet login setup <<<
BASH_LOGIN

# Install redeploy permissions for the dashboard action.
echo "[9/14] Installing redeploy permissions..."
chmod +x "$REPO_DIR/scripts/redeploy-robot.sh"
chmod +x "$REPO_DIR/restart.sh"
SYSTEMCTL_PATH="$(command -v systemctl)"
INSTALL_PATH="$(command -v install)"
APT_PATH="$(command -v apt)"
SHUTDOWN_PATH="$(command -v shutdown)"
SUDOERS_TMP="$(mktemp)"
cat >"$SUDOERS_TMP" <<SUDOERS
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH daemon-reload
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH enable robot-vision.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH enable robot-voice.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH enable robot-sensors.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH enable robot-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH enable robot-pi-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH enable robot-motion.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-brain.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-telemetry.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start gamepad-teleop.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-camera.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-vision.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-voice.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-sensors.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-pi-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH start robot-motion.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-brain.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-telemetry.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop gamepad-teleop.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-camera.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-vision.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-voice.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-sensors.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-pi-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH stop robot-motion.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-brain.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-telemetry.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart gamepad-teleop.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-camera.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-vision.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-voice.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-sensors.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-pi-battery.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-motion.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart robot-web-dashboard.service
$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart --no-block robot-web-dashboard.service
$USER ALL=(root) NOPASSWD: $SHUTDOWN_PATH -h now
$USER ALL=(root) NOPASSWD: $INSTALL_PATH -m 0644 /home/pi/robot-pet/systemd/*.service /etc/systemd/system/*.service
SUDOERS
sudo visudo -cf "$SUDOERS_TMP"
sudo install -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/robot-pet-redeploy
rm "$SUDOERS_TMP"

# Set up Python venv (idempotent - only creates if missing or misconfigured).
# --system-site-packages lets the venv import apt-installed Pi libraries
# (picamera2, libcamera) that aren't reliably pip-installable.
echo "[10/14] Setting up Python venv at $VENV_PATH..."
NEEDS_VENV_RECREATE=0
if [[ -d "$VENV_PATH" ]]; then
  if ! grep -q "^include-system-site-packages = true" "$VENV_PATH/pyvenv.cfg" 2>/dev/null; then
    echo "    Existing venv missing --system-site-packages; recreating"
    NEEDS_VENV_RECREATE=1
  fi
else
  NEEDS_VENV_RECREATE=1
fi
if [[ "$NEEDS_VENV_RECREATE" == "1" ]]; then
  rm -rf "$VENV_PATH"
  python3 -m venv --system-site-packages "$VENV_PATH"
fi

# Upgrade pip and install base packages (idempotent - pip handles already-installed)
echo "[11/14] Installing Python packages..."
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip wheel setuptools
python -m pip install -e "$REPO_DIR"
# openwakeword: install separately with --no-deps because it hard-requires
# tflite-runtime on Linux (no Py 3.13 wheel) and we only use the ONNX backend.
# --ignore-requires-python bypasses its python_requires<3.12 gate, which would
# otherwise make pip fall back to the ancient 0.4.0 (missing download_models).
python -m pip install --no-deps --ignore-requires-python --upgrade 'openwakeword>=0.6.0'

echo "[12/14] Running tests..."
python -m unittest discover tests

# Set ReSpeaker PCM volume and persist via alsa-restore.service on next boot
echo "[13/14] Setting speaker volume..."
if amixer -c 0 sset 'PCM' 100% >/dev/null; then
  sudo alsactl store || echo "    WARNING: could not persist ALSA mixer state; continuing"
else
  echo "    WARNING: could not set PCM volume with amixer; continuing"
fi

# Install and enable systemd services
echo "[14/14] Installing systemd services..."
for service_file in "$REPO_DIR/systemd/"*.service; do
  sudo install -m 0644 "$service_file" "/etc/systemd/system/$(basename "$service_file")"
done
sudo systemctl daemon-reload
for service in \
  robot-brain.service \
  robot-telemetry.service \
  robot-battery.service \
  robot-pi-battery.service \
  robot-motion.service \
  gamepad-teleop.service \
  robot-camera.service \
  robot-vision.service \
  robot-voice.service \
  robot-sensors.service \
  robot-web-dashboard.service
do
  echo "    enabling $service"
  sudo systemctl enable "$service"
done
for service in \
  robot-vision.service \
  gamepad-teleop.service \
  robot-camera.service \
  robot-voice.service \
  robot-sensors.service \
  robot-motion.service \
  robot-pi-battery.service \
  robot-battery.service \
  robot-telemetry.service \
  robot-web-dashboard.service \
  robot-brain.service
do
  echo "    stopping $service"
  sudo systemctl stop "$service"
done
for service in \
  robot-brain.service \
  robot-telemetry.service \
  robot-sensors.service \
  robot-battery.service \
  robot-pi-battery.service \
  robot-motion.service \
  robot-camera.service \
  gamepad-teleop.service \
  robot-vision.service \
  robot-voice.service \
  robot-web-dashboard.service
do
  echo "    starting $service"
  sudo systemctl start "$service"
done

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
echo "    sudo systemctl status robot-telemetry"
echo "    journalctl -u robot-telemetry -f"
echo "    sudo systemctl status robot-battery"
echo "    journalctl -u robot-battery -f"
echo "    sudo systemctl status robot-pi-battery"
echo "    journalctl -u robot-pi-battery -f"
echo "    sudo systemctl status robot-motion"
echo "    journalctl -u robot-motion -f"
echo "    sudo systemctl status gamepad-teleop"
echo "    journalctl -u gamepad-teleop -f"
echo "    sudo systemctl status robot-sensors"
echo "    journalctl -u robot-sensors -f"
echo "    sudo systemctl status robot-camera"
echo "    journalctl -u robot-camera -f"
echo "    sudo systemctl status robot-vision"
echo "    journalctl -u robot-vision -f"
echo "    sudo systemctl status robot-voice"
echo "    journalctl -u robot-voice -f"
echo "    sudo systemctl status robot-web-dashboard"
echo "    journalctl -u robot-web-dashboard -f"
echo ""
echo "Open the web dashboard at http://<this-pi>:8080/"
echo "Create /home/pi/.config/robot-pet/voice.env with ELEVENLABS_API_KEY and OPENAI_API_KEY before enabling Listen."
