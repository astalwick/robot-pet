#!/bin/bash
# Read-only survey of a fresh Ubuntu card: what exists, what doesn't.
# Installs nothing, changes nothing. Its output is what we write setup.sh from.
# Temporary — delete once the Ubuntu port is done (ros2-migration Phase 1).

echo "=== OS ==="
. /etc/os-release && echo "$PRETTY_NAME ($VERSION_CODENAME)"
uname -m

echo ""
echo "=== apt packages: does the archive have them? ==="
for package in python3-picamera2 python3-libcamera opencv-data python3-opencv \
               alsa-utils sox portaudio19-dev i2c-tools avahi-daemon \
               rpi-eeprom libcamera-tools rpicam-apps python3-lgpio python3-gpiozero; do
    candidate="$(apt-cache policy "$package" 2>/dev/null | awk '/Candidate:/ {print $2}')"
    printf "%-22s %s\n" "$package" "${candidate:-MISSING}"
done

echo ""
echo "=== groups (usermod fails if absent) ==="
for group in dialout i2c input audio video render; do
    if getent group "$group" >/dev/null; then
        printf "%-10s present\n" "$group"
    else
        printf "%-10s MISSING\n" "$group"
    fi
done

echo ""
echo "=== boot config ==="
echo "--- /boot/firmware/cmdline.txt (look for console=serial0/ttyAMA0) ---"
cat /boot/firmware/cmdline.txt 2>/dev/null || echo "MISSING"
echo "--- /boot/firmware/config.txt (enable_uart / disable-bt / i2c_arm) ---"
grep -E "enable_uart|disable-bt|i2c_arm" /boot/firmware/config.txt 2>/dev/null || echo "none of ours present yet"

echo ""
echo "=== serial console holding the UART? ==="
systemctl is-enabled serial-getty@ttyAMA0.service 2>/dev/null || echo "serial-getty@ttyAMA0: not enabled"
ls -l /dev/serial0 2>/dev/null || echo "/dev/serial0 MISSING (expected until enable_uart + reboot)"

echo ""
echo "=== hardware presence ==="
echo "--- i2c buses ---"
ls /dev/i2c-* 2>/dev/null || echo "no /dev/i2c-* (expected until i2c_arm + reboot)"
echo "--- audio capture ---"
arecord -l 2>/dev/null || echo "arecord not installed yet"
echo "--- gamepad ---"
ls /dev/input/js* /dev/input/event* 2>/dev/null || echo "no input devices"
echo "--- camera ---"
ls /dev/video* 2>/dev/null || echo "no /dev/video*"

echo ""
echo "=== telemetry fallbacks ==="
command -v vcgencmd >/dev/null && echo "vcgencmd present" || echo "vcgencmd absent (expected on Ubuntu)"
cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo "thermal_zone0 MISSING"

echo ""
echo "=== python ==="
python3 --version
