#!/bin/bash
set -euo pipefail

REPO_DIR="${ROBOT_PET_REPO_DIR:-$HOME/robot-pet}"
SERVICES=(
  robot-brain.service
  robot-telemetry.service
  robot-camera.service
  gamepad-teleop.service
  robot-vision.service
  robot-voice.service
  robot-web-dashboard.service
)
STOP_SERVICES=(
  robot-vision.service
  gamepad-teleop.service
  robot-camera.service
  robot-voice.service
  robot-telemetry.service
  robot-brain.service
)
START_SERVICES=(
  robot-brain.service
  robot-telemetry.service
  robot-camera.service
  gamepad-teleop.service
  robot-vision.service
  robot-voice.service
)

declare -A RESTART_SERVICES=()

want_restart() {
  RESTART_SERVICES[$1]=1
}

want_all_robot_services() {
  local service
  for service in "${SERVICES[@]}"; do
    want_restart "$service"
  done
}

plan_service_restarts() {
  local path
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    case "$path" in
      src/robot_brain.py)
        want_restart robot-brain.service
        ;;
      src/robot_telemetry.py | src/telemetry/*)
        want_restart robot-telemetry.service
        ;;
      src/robot_camera.py)
        want_restart robot-camera.service
        ;;
      src/gamepad_teleop.py | src/control/*)
        want_restart gamepad-teleop.service
        ;;
      src/robot_vision.py)
        want_restart robot-vision.service
        ;;
      src/robot_voice.py | src/voice/*)
        want_restart robot-voice.service
        ;;
      src/robot_web_dashboard.py | src/web_dashboard_static/*)
        want_restart robot-web-dashboard.service
        ;;
      src/lib/* | pyproject.toml)
        want_all_robot_services
        ;;
      src/drivers/motor.py | src/drivers/controller.py | src/drivers/__init__.py)
        want_restart robot-brain.service
        want_restart gamepad-teleop.service
        ;;
      src/drivers/camera.py)
        want_restart robot-camera.service
        want_restart robot-vision.service
        ;;
      src/drivers/respeaker.py)
        want_restart robot-voice.service
        ;;
      src/drivers/*)
        want_restart robot-brain.service
        want_restart gamepad-teleop.service
        want_restart robot-camera.service
        want_restart robot-vision.service
        want_restart robot-voice.service
        ;;
      src/config/*)
        want_restart gamepad-teleop.service
        want_restart robot-vision.service
        want_restart robot-voice.service
        ;;
      systemd/*)
        want_restart "$(basename "$path")"
        ;;
    esac
  done
}

echo "== Robo-Pet redeploy =="
echo "repo: $REPO_DIR"
echo ""

cd "$REPO_DIR"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to redeploy: working tree has local changes."
  echo "Commit, stash, or discard them before redeploying."
  exit 1
fi

echo "Fetching latest changes..."
git fetch --prune

old_rev="$(git rev-parse HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}")"
local_rev="$(git rev-parse "$branch")"
upstream_rev="$(git rev-parse "$upstream")"
base_rev="$(git merge-base "$branch" "$upstream")"

if [[ "$local_rev" == "$upstream_rev" ]]; then
  echo "Already up to date on $branch."
  exit 0
elif [[ "$local_rev" == "$base_rev" ]]; then
  echo "Fast-forwarding $branch from $upstream..."
  git merge --ff-only "$upstream"
else
  echo "Refusing to redeploy: $branch has diverged from $upstream."
  echo "Resolve the branch state manually, then redeploy."
  exit 1
fi

new_rev="$(git rev-parse HEAD)"
changed_files="$(git diff --name-only "$old_rev" "$new_rev")"
echo "Deployed $old_rev -> $new_rev"
echo ""

need_pip=0
systemd_changed=0
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if [[ "$path" == pyproject.toml ]]; then
    need_pip=1
  fi
  if [[ "$path" == systemd/* ]]; then
    systemd_changed=1
  fi
done <<< "$changed_files"

plan_service_restarts <<< "$changed_files"

if [[ "$need_pip" -eq 1 ]]; then
  echo "Installing Python package metadata and dependencies..."
  "$REPO_DIR/.venv/bin/python" -m pip install -e "$REPO_DIR"
  echo ""
fi

echo "Running tests..."
"$REPO_DIR/.venv/bin/python" -m unittest discover tests
echo ""

if [[ "$systemd_changed" -eq 1 ]]; then
  echo "Installing changed systemd units..."
  while IFS= read -r path; do
    [[ "$path" == systemd/* ]] || continue
    service="$(basename "$path")"
    sudo install -m 0644 "$REPO_DIR/$path" "/etc/systemd/system/$service"
    if [[ "$service" == robot-vision.service || "$service" == robot-voice.service ]]; then
      echo "enabling $service"
      sudo systemctl enable "$service"
    fi
  done <<< "$changed_files"
  sudo systemctl daemon-reload
  echo ""
fi

if [[ ${#RESTART_SERVICES[@]} -eq 0 ]]; then
  echo "No robot services need a restart for this commit."
else
  echo "Restarting robot services..."
  for service in "${STOP_SERVICES[@]}"; do
    if [[ -n "${RESTART_SERVICES[$service]:-}" ]]; then
      echo "stopping $service"
      sudo systemctl stop "$service"
    fi
  done
  for service in "${START_SERVICES[@]}"; do
    if [[ -n "${RESTART_SERVICES[$service]:-}" ]]; then
      echo "starting $service"
      sudo systemctl start "$service"
    fi
  done
  if [[ -n "${RESTART_SERVICES[robot-web-dashboard.service]:-}" ]]; then
    echo "restarting robot-web-dashboard.service"
    sudo systemctl restart --no-block robot-web-dashboard.service
  fi
  echo ""
fi

echo "Redeploy complete."
