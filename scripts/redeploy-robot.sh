#!/bin/bash
set -euo pipefail

REPO_DIR="${ROBOT_PET_REPO_DIR:-$HOME/robot-pet}"
SERVICES=(
  robot-vision.service
  robot-camera.service
  robot-telemetry.service
  robot-web-dashboard.service
  gamepad-teleop.service
  robot-brain.service
)

echo "== Robo-Pet redeploy =="
echo "repo: $REPO_DIR"
echo ""

cd "$REPO_DIR"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to redeploy: working tree has local changes."
  echo "Commit, stash, or discard them before redeploying."
  exit 1
fi

echo "[1/6] Fetching latest changes..."
git fetch --prune

branch="$(git rev-parse --abbrev-ref HEAD)"
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}")"
local_rev="$(git rev-parse "$branch")"
upstream_rev="$(git rev-parse "$upstream")"
base_rev="$(git merge-base "$branch" "$upstream")"

if [[ "$local_rev" == "$upstream_rev" ]]; then
  echo "Already up to date on $branch."
elif [[ "$local_rev" == "$base_rev" ]]; then
  echo "[2/6] Fast-forwarding $branch from $upstream..."
  git merge --ff-only "$upstream"
else
  echo "Refusing to redeploy: $branch has diverged from $upstream."
  echo "Resolve the branch state manually, then redeploy."
  exit 1
fi

echo "[3/7] Installing OpenCV system packages..."
sudo apt install -y python3-opencv opencv-data

echo "[4/7] Installing Python package metadata and dependencies..."
"$REPO_DIR/.venv/bin/python" -m pip install -e "$REPO_DIR"

echo "[5/7] Running tests..."
"$REPO_DIR/.venv/bin/python" -m unittest discover tests

echo "[6/7] Installing and reloading systemd units..."
for service in "${SERVICES[@]}"; do
  sudo install -m 0644 "$REPO_DIR/systemd/$service" "/etc/systemd/system/$service"
done
sudo systemctl daemon-reload
echo "enabling robot-vision.service"
timeout 45 sudo systemctl enable robot-vision.service

echo "[7/7] Restarting robot services..."
for service in "${SERVICES[@]}"; do
  echo "restarting $service"
  timeout 45 sudo systemctl restart "$service"
done

echo ""
echo "Redeploy complete."
