"""Tests for scripts/redeploy-robot.sh incremental deploy behavior."""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "redeploy-robot.sh"

ALL_SERVICES = {
    "robot-brain.service",
    "robot-telemetry.service",
    "robot-camera.service",
    "gamepad-teleop.service",
    "robot-vision.service",
    "robot-voice.service",
    "robot-web-dashboard.service",
}


def planned_services(paths: list[str]) -> set[str]:
    """Mirror plan_service_restarts() in scripts/redeploy-robot.sh."""
    services: set[str] = set()

    def want(name: str) -> None:
        services.add(name)

    def want_all() -> None:
        services.update(ALL_SERVICES)

    for path in paths:
        if not path:
            continue
        if path == "src/robot_brain.py":
            want("robot-brain.service")
        elif path in ("src/robot_telemetry.py",) or path.startswith("src/telemetry/"):
            want("robot-telemetry.service")
        elif path == "src/robot_camera.py":
            want("robot-camera.service")
        elif path == "src/gamepad_teleop.py" or path.startswith("src/control/"):
            want("gamepad-teleop.service")
        elif path == "src/robot_vision.py":
            want("robot-vision.service")
        elif path == "src/robot_voice.py" or path.startswith("src/voice/"):
            want("robot-voice.service")
        elif path == "src/robot_web_dashboard.py" or path.startswith("src/web_dashboard_static/"):
            want("robot-web-dashboard.service")
        elif path.startswith("src/lib/") or path == "pyproject.toml":
            want_all()
        elif path in (
            "src/drivers/motor.py",
            "src/drivers/controller.py",
            "src/drivers/__init__.py",
        ):
            want("robot-brain.service")
            want("gamepad-teleop.service")
        elif path == "src/drivers/camera.py":
            want("robot-camera.service")
            want("robot-vision.service")
        elif path == "src/drivers/respeaker.py":
            want("robot-voice.service")
        elif path.startswith("src/drivers/"):
            want("robot-brain.service")
            want("gamepad-teleop.service")
            want("robot-camera.service")
            want("robot-vision.service")
            want("robot-voice.service")
        elif path.startswith("src/config/"):
            want("gamepad-teleop.service")
            want("robot-vision.service")
            want("robot-voice.service")
        elif path.startswith("systemd/"):
            want(Path(path).name)

    return services


class TestRedeployRobot(unittest.TestCase):
    def test_script_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_exits_when_already_up_to_date(self):
        body = SCRIPT.read_text()
        self.assertIn('echo "Already up to date on $branch."', body)
        self.assertRegex(body, r'Already up to date on \$branch\."\n  exit 0')

    def test_skips_pip_without_pyproject_change(self):
        body = SCRIPT.read_text()
        self.assertIn('if [[ "$need_pip" -eq 1 ]]; then', body)
        self.assertIn('if [[ "$path" == pyproject.toml ]]; then', body)

    def test_no_apt_install(self):
        body = SCRIPT.read_text()
        self.assertNotIn("apt install", body)

    def test_dashboard_restart_writes_success_before_restart(self):
        body = SCRIPT.read_text()
        self.assertIn(
            """if [[ -n "${RESTART_SERVICES[robot-web-dashboard.service]:-}" ]]; then
    if [[ -n "${ROBOT_PET_REDEPLOY_STATUS_FILE:-}" ]]; then
      redeploy_status_dir="$(dirname "$ROBOT_PET_REDEPLOY_STATUS_FILE")"
      redeploy_status_tmp="$redeploy_status_dir/.redeploy-status.$$"
      mkdir -p "$redeploy_status_dir"
      printf '{"last_result":"success","last_message":"Redeploy complete."}\\n' > "$redeploy_status_tmp"
      mv "$redeploy_status_tmp" "$ROBOT_PET_REDEPLOY_STATUS_FILE"
    fi
    echo "restarting robot-web-dashboard.service"
    sudo systemctl restart --no-block robot-web-dashboard.service
  fi""",
            body,
        )
        self.assertIn("ROBOT_PET_REDEPLOY_STATUS_FILE", body)

    def test_voice_paths_restart_voice_only(self):
        services = planned_services(["src/voice/assistant.py"])
        self.assertEqual(services, {"robot-voice.service"})

    def test_pyproject_restarts_all_services(self):
        self.assertEqual(planned_services(["pyproject.toml"]), ALL_SERVICES)

    def test_docs_only_plans_no_restarts(self):
        self.assertEqual(
            planned_services(["docs/ARCHITECTURE.md", "README.md"]),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
