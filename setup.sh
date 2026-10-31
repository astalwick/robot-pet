#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/robot-pet"
VENV_PATH="$REPO_DIR/.venv"
LOG_DIR="/var/log/robot-pet"
REPO_HTTPS="https://github.com/astalwick/robot-pet"

# Start order; foxglove comes last (nothing depends on it).
ROBOT_SERVICES=(
  robot-brain.service
  robot-telemetry.service
  robot-state-publisher.service
  robot-sensors.service
  robot-battery.service
  robot-pi-battery.service
  robot-motion.service
  robot-twist-mux.service
  robot-lidar.service
  robot-nav.service
  robot-lidar-status.service
  robot-nav-bridge.service
  robot-camera.service
  gamepad-teleop.service
  robot-vision.service
  robot-voice.service
  robot-web-dashboard.service
  robot-foxglove.service
)
ROBOT_SERVICES_STOP_ORDER=(
  robot-foxglove.service
  robot-nav-bridge.service
  robot-lidar-status.service
  robot-nav.service
  robot-lidar.service
  robot-vision.service
  gamepad-teleop.service
  robot-twist-mux.service
  robot-camera.service
  robot-voice.service
  robot-sensors.service
  robot-motion.service
  robot-pi-battery.service
  robot-battery.service
  robot-state-publisher.service
  robot-telemetry.service
  robot-web-dashboard.service
  robot-brain.service
)

echo "=== Robo-Pet Setup ==="
echo ""

# Ubuntu Server ships a few daemons that do nothing for a robot and together
# hold ~300 MB on the 4 GB Pi: snapd, fwupd, packagekit, and the unattended
# apt timers (which also grab the apt lock and CPU at random). Disable rather
# than purge so the ubuntu-server metapackage stays intact. Runs first so
# unattended-upgrades can't hold the apt lock through the rest of setup.
echo "[1/17] Disabling Ubuntu baggage and adding zram swap..."
sudo systemctl disable --now snapd.socket snapd.service snapd.seeded.service \
    fwupd.service fwupd-refresh.timer packagekit.service \
    unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
sudo systemctl mask snapd.service fwupd.service packagekit.service unattended-upgrades.service >/dev/null
# Ubuntu Server has no swap; compressed zram turns an out-of-memory kill under
# Nav2 + SLAM into a slowdown instead.
sudo apt install -y zram-tools
sudo tee /etc/default/zramswap >/dev/null <<'ZRAM'
ALGO=zstd
PERCENT=50
ZRAM
sudo systemctl restart zramswap.service

# Install base packages (idempotent - apt handles already-installed)
echo "[2/17] Installing base packages..."
sudo apt install -y git curl vim htop tmux python3-pip python3-venv python3-picamera2 python3-opencv opencv-data alsa-utils sox portaudio19-dev i2c-tools avahi-daemon python3-lgpio


# Add user to dialout group for serial port access (idempotent)
echo "[3/17] Adding $USER to dialout group..."
sudo usermod -a -G dialout "$USER"

# I2C for ToF sensors and future IMU (idempotent)
echo "[4/17] Enabling I2C and adding $USER to i2c group..."
BOOT_CONFIG="/boot/firmware/config.txt"
if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG" 2>/dev/null; then
    echo "dtparam=i2c_arm=on" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "    Added dtparam=i2c_arm=on to $BOOT_CONFIG (reboot required)"
else
    echo "    I2C already enabled in $BOOT_CONFIG"
fi
# Ubuntu's image has no i2c group (Pi OS creates it); make it and own the buses
if ! getent group i2c >/dev/null; then
    sudo groupadd i2c
fi
sudo tee /etc/udev/rules.d/99-robot-pet-i2c.rules >/dev/null <<'UDEV'
KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"
UDEV
sudo usermod -a -G i2c "$USER"

# GPIO for the battery MOSFET cutoff. gpiozero 2.0.1 hardcodes gpiochip4 on
# Pi 5, but kernels since 6.6.47 renamed the header chip to gpiochip0 — Pi OS
# ships a compat symlink, Ubuntu doesn't. /dev/gpiochip0 itself is already
# rw for the dialout group.
sudo tee /etc/udev/rules.d/99-robot-pet-gpio.rules >/dev/null <<'UDEV'
KERNEL=="gpiochip0", SYMLINK+="gpiochip4"
UDEV

# Add user to input and audio groups for controller/gamepad and ReSpeaker access (idempotent)
echo "[5/17] Adding $USER to input and audio groups and configuring ReSpeaker USB access..."
sudo usermod -a -G input "$USER"
sudo usermod -a -G audio "$USER"
# Camera: Pi OS puts the default user in video/render; Ubuntu doesn't, and
# libcamera needs /dev/media*, /dev/video* (video) and dma-buf heaps.
sudo usermod -a -G video "$USER"
sudo usermod -a -G render "$USER"
sudo tee /etc/udev/rules.d/99-robot-pet-respeaker.rules >/dev/null <<'UDEV'
SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="001e", GROUP="audio", MODE="0660"
UDEV
# RPLidar A1 (CP2102 USB-UART): stable /dev/rplidar path, and systemd starts
# robot-lidar.service when it appears and stops it when it goes away.
sudo tee /etc/udev/rules.d/99-robot-pet-rplidar.rules >/dev/null <<'UDEV'
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="rplidar", TAG+="systemd", ENV{SYSTEMD_WANTS}="robot-lidar.service"
ACTION=="remove", SUBSYSTEM=="tty", ENV{ID_VENDOR_ID}=="10c4", ENV{ID_MODEL_ID}=="ea60", RUN+="/usr/bin/systemctl --no-block stop robot-lidar.service"
UDEV
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=2886 --attr-match=idProduct=001e
sudo udevadm trigger --action=add --subsystem-match=tty
sudo udevadm trigger /dev/gpiochip0 2>/dev/null || true

# Ubuntu's portaudio is built with a PulseAudio backend, and Pa_Initialize
# fails hard when no sound server is running (PortAudio issue #900). Run
# pipewire-pulse for user pi at boot, but disable PipeWire's ALSA monitor so
# it never claims the ReSpeaker — voice code opens it directly via ALSA.
sudo apt install -y pipewire pipewire-pulse wireplumber
sudo mkdir -p /etc/wireplumber/wireplumber.conf.d
sudo tee /etc/wireplumber/wireplumber.conf.d/99-robot-pet-disable-alsa.conf >/dev/null <<'CONF'
wireplumber.profiles = {
  main = {
    monitor.alsa = disabled
  }
}
CONF
sudo loginctl enable-linger "$USER"
sudo systemctl --global enable pipewire.socket pipewire-pulse.socket
systemctl --user start pipewire.socket pipewire-pulse.socket 2>/dev/null || \
    echo "    WARNING: could not start PipeWire user sockets until next login/reboot"

# Free UART from Bluetooth for RoboClaw serial (idempotent)
echo "[6/17] Configuring UART for RoboClaw..."
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
# Ubuntu runs a login getty on the RoboClaw UART and lacks Pi OS's serial0 alias
BOOT_CMDLINE="/boot/firmware/cmdline.txt"
CMDLINE_TMP="$(mktemp)"
awk '{
  sep = ""
  for (i = 1; i <= NF; i++) {
    if ($i ~ /^console=(serial0|ttyAMA0),/) {
      continue
    }
    printf "%s%s", sep, $i
    sep = " "
  }
  print ""
}' "$BOOT_CMDLINE" >"$CMDLINE_TMP"
if ! cmp -s "$BOOT_CMDLINE" "$CMDLINE_TMP"; then
    sudo install -m 0644 "$CMDLINE_TMP" "$BOOT_CMDLINE"
    echo "    Removed serial console from $BOOT_CMDLINE (reboot required)"
else
    echo "    Serial console already clear of RoboClaw UART"
fi
rm "$CMDLINE_TMP"
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo systemctl mask serial-getty@ttyAMA0.service >/dev/null
sudo tee /etc/udev/rules.d/99-robot-pet-serial0.rules >/dev/null <<'UDEV'
KERNEL=="ttyAMA0", SYMLINK+="serial0"
UDEV

# Cut power on halt/shutdown (Pi 4/5 EEPROM; idempotent)
echo "[7/17] Configuring EEPROM power-off on halt..."
if command -v rpi-eeprom-config >/dev/null 2>&1; then
    EEPROM_CONFIG="$(mktemp)"
    sudo rpi-eeprom-config >"$EEPROM_CONFIG"
    if grep -q "^POWER_OFF_ON_HALT=1" "$EEPROM_CONFIG"; then
        echo "    POWER_OFF_ON_HALT=1 already set"
    else
        if grep -q "^POWER_OFF_ON_HALT=" "$EEPROM_CONFIG"; then
            sed -i 's/^POWER_OFF_ON_HALT=.*/POWER_OFF_ON_HALT=1/' "$EEPROM_CONFIG"
        else
            echo "POWER_OFF_ON_HALT=1" >>"$EEPROM_CONFIG"
        fi
        sudo rpi-eeprom-config --apply "$EEPROM_CONFIG"
        echo "    Set POWER_OFF_ON_HALT=1 in EEPROM"
    fi
    rm "$EEPROM_CONFIG"
else
    echo "    rpi-eeprom-config not found; skipping EEPROM power-off setting"
fi

# Create log directory (idempotent)
echo "[8/17] Creating log directory at $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$USER:$USER" "$LOG_DIR"

# SSH login welcome (dynamic MOTD on Debian / Raspberry Pi OS)
echo "[9/17] Installing login welcome message..."
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
echo "[10/17] Configuring Bash login directory, venv, and dashboard autostart..."
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
echo "[11/17] Installing redeploy permissions..."
chmod +x "$REPO_DIR/scripts/redeploy-robot.sh"
chmod +x "$REPO_DIR/scripts/install-service.sh"
chmod +x "$REPO_DIR/restart.sh"
SYSTEMCTL_PATH="$(command -v systemctl)"
SHUTDOWN_PATH="$(command -v shutdown)"
# One line per service per verb: Ubuntu's sudo-rs forbids wildcards in
# sudoers command arguments, so each unit has to be spelled out.
SUDOERS_TMP="$(mktemp)"
{
  echo "$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH daemon-reload"
  for service in "${ROBOT_SERVICES[@]}"; do
    for verb in enable start stop restart; do
      echo "$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH $verb $service"
    done
  done
  echo "$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart --no-block robot-web-dashboard.service"
  echo "$USER ALL=(root) NOPASSWD: $SHUTDOWN_PATH -h now"
  echo "$USER ALL=(root) NOPASSWD: $REPO_DIR/scripts/install-service.sh"
} >"$SUDOERS_TMP"
sudo visudo -cf "$SUDOERS_TMP"
sudo install -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/robot-pet-redeploy
rm "$SUDOERS_TMP"

# ROS 2 Lyrical Luth from the official apt repo. Nav2 and slam_toolbox aren't
# used until the lidar plan; installing them now gives a real memory number on
# the 4 GB Pi instead of an estimate. universe is enabled by default on Ubuntu
# Server images, which the ros-* packages need.
echo "[12/17] Installing ROS 2 Lyrical..."
if ! dpkg -s ros2-apt-source >/dev/null 2>&1; then
    ROS_APT_SOURCE_VERSION="$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F '"tag_name"' | awk -F'"' '{print $4}')"
    ROS_APT_SOURCE_DEB="$(mktemp --suffix=.deb)"
    curl -fsSL -o "$ROS_APT_SOURCE_DEB" "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo "$VERSION_CODENAME")_all.deb"
    sudo dpkg -i "$ROS_APT_SOURCE_DEB"
    rm "$ROS_APT_SOURCE_DEB"
    sudo apt update
fi
sudo apt install -y ros-lyrical-ros-base ros-lyrical-rmw-cyclonedds-cpp \
    ros-lyrical-foxglove-bridge ros-lyrical-robot-state-publisher \
    ros-lyrical-twist-mux ros-lyrical-demo-nodes-py \
    ros-lyrical-slam-toolbox ros-lyrical-nav2-bringup ros-lyrical-rplidar-driver
# Every ROS unit runs through scripts/ros-node.sh, which sources this. DDS
# stays off the Wi-Fi: localhost discovery, a fixed domain, and CycloneDDS
# pinned to loopback. Without the pin it sends even local traffic to the
# Wi-Fi address, and the router's nightly Wi-Fi drop leaves nodes deaf to
# each other. Foxglove's websocket still listens on every address.
mkdir -p "$HOME/.config/robot-pet"
cat >"$HOME/.config/robot-pet/ros.env" <<'ENV'
ROS_DOMAIN_ID=17
ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="lo"/></Interfaces></General></Domain></CycloneDDS>'
ENV
chmod +x "$REPO_DIR/scripts/ros-node.sh"

# Set up Python venv (idempotent - only creates if missing or misconfigured).
# --system-site-packages lets the venv import apt-installed Pi libraries
# (picamera2, libcamera) that aren't reliably pip-installable.
echo "[13/17] Setting up Python venv at $VENV_PATH..."
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
echo "[14/17] Installing Python packages..."
source "$VENV_PATH/bin/activate"
python -m pip install --upgrade pip wheel setuptools
python -m pip install -e "$REPO_DIR"
# openwakeword: install separately with --no-deps because it hard-requires
# tflite-runtime on Linux (no Py 3.13 wheel) and we only use the ONNX backend.
# --ignore-requires-python bypasses its python_requires<3.12 gate, which would
# otherwise make pip fall back to the ancient 0.4.0 (missing download_models).
python -m pip install --no-deps --ignore-requires-python --upgrade 'openwakeword>=0.6.0'

echo "[15/17] Running tests..."
python -m unittest discover tests

# Set ReSpeaker PCM volume and persist via alsa-restore.service on next boot
echo "[16/17] Setting speaker volume..."
if amixer -c 0 sset 'PCM' 100% >/dev/null; then
  sudo alsactl store || echo "    WARNING: could not persist ALSA mixer state; continuing"
else
  echo "    WARNING: could not set PCM volume with amixer; continuing"
fi

# Install and enable systemd services
echo "[17/17] Installing systemd services..."
for service_file in "$REPO_DIR/systemd/"*.service; do
  sudo install -m 0644 "$service_file" "/etc/systemd/system/$(basename "$service_file")"
done
sudo systemctl daemon-reload
for service in "${ROBOT_SERVICES[@]}"; do
  echo "    enabling $service"
  sudo systemctl enable "$service"
done
for service in "${ROBOT_SERVICES_STOP_ORDER[@]}"; do
  echo "    stopping $service"
  sudo systemctl stop "$service"
done
for service in "${ROBOT_SERVICES[@]}"; do
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
echo "    ./restart.sh            # all units; or name some: ./restart.sh motion sensors"
echo "    ros2 topic list         # after: source /opt/ros/lyrical/setup.bash; set -a; source ~/.config/robot-pet/ros.env"
echo ""
echo "Open the web dashboard at http://<this-pi>:8080/"
echo "Create /home/pi/.config/robot-pet/voice.env with ELEVENLABS_API_KEY and OPENAI_API_KEY before enabling Listen."
