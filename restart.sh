#!/bin/bash
set -euo pipefail

ALL_SERVICES=(
  robot-brain.service
  robot-telemetry.service
  robot-battery.service
  robot-pi-battery.service
  robot-motion.service
  robot-camera.service
  gamepad-teleop.service
  robot-vision.service
  robot-voice.service
  robot-sensors.service
  robot-web-dashboard.service
)
STOP_ORDER=(
  robot-vision.service
  gamepad-teleop.service
  robot-camera.service
  robot-voice.service
  robot-sensors.service
  robot-motion.service
  robot-pi-battery.service
  robot-battery.service
  robot-telemetry.service
  robot-brain.service
  robot-web-dashboard.service
)
START_ORDER=(
  robot-brain.service
  robot-telemetry.service
  robot-sensors.service
  robot-battery.service
  robot-pi-battery.service
  robot-motion.service
  robot-camera.service
  gamepad-teleop.service
  robot-vision.service
  robot-voice.service
  robot-web-dashboard.service
)

usage() {
  cat <<EOF
Usage: $(basename "$0") [service ...]

Restart all robot-pet systemd services, or only the ones named.

Service names can be short (voice, motion, dashboard) or full (robot-voice.service).
With no arguments, every service is restarted in dependency order.

Examples:
  $(basename "$0")
  $(basename "$0") voice motion
  $(basename "$0") robot-camera gamepad-teleop
EOF
}

normalize_service() {
  local name="${1%.service}"
  case "$name" in
    brain) echo robot-brain.service; return ;;
    telemetry) echo robot-telemetry.service; return ;;
    battery) echo robot-battery.service; return ;;
    pi-battery|ups) echo robot-pi-battery.service; return ;;
    motion) echo robot-motion.service; return ;;
    camera) echo robot-camera.service; return ;;
    gamepad-teleop|gamepad|teleop) echo gamepad-teleop.service; return ;;
    vision) echo robot-vision.service; return ;;
    voice) echo robot-voice.service; return ;;
    sensors) echo robot-sensors.service; return ;;
    web-dashboard|dashboard) echo robot-web-dashboard.service; return ;;
  esac
  if [[ "$name" == robot-* || "$name" == gamepad-teleop ]]; then
    echo "${name}.service"
    return
  fi
  echo "Unknown service: $1" >&2
  echo "Known services: brain telemetry battery pi-battery motion camera gamepad-teleop vision voice sensors dashboard" >&2
  exit 1
}

REQUESTED=()

should_restart() {
  local service="$1"
  local name
  for name in "${REQUESTED[@]}"; do
    [[ "$name" == "$service" ]] && return 0
  done
  return 1
}

if [[ $# -gt 0 && ( "$1" == "-h" || "$1" == "--help" || "$1" == "help" ) ]]; then
  usage
  exit 0
fi

if [[ $# -eq 0 ]]; then
  REQUESTED=("${ALL_SERVICES[@]}")
else
  while [[ $# -gt 0 ]]; do
    REQUESTED+=("$(normalize_service "$1")")
    shift
  done
fi

echo "== Robo-Pet restart =="
for service in "${STOP_ORDER[@]}"; do
  if should_restart "$service"; then
    echo "stopping $service"
    sudo systemctl stop "$service"
  fi
done
for service in "${START_ORDER[@]}"; do
  if should_restart "$service"; then
    echo "starting $service"
    sudo systemctl start "$service"
  fi
done
echo "Restart complete."
