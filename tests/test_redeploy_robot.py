"""Tests for scripts/redeploy-robot.sh incremental deploy behavior."""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "redeploy-robot.sh"
SETUP = REPO_ROOT / "setup.sh"

ALL_SERVICES = {
    "robot-brain.service",
    "robot-telemetry.service",
    "robot-battery.service",
    "robot-motion.service",
    "robot-camera.service",
    "gamepad-teleop.service",
    "robot-vision.service",
    "robot-voice.service",
    "robot-sensors.service",
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
            want("robot-battery.service")
        elif path == "src/robot_camera.py":
            want("robot-camera.service")
        elif path == "src/robot_motion.py":
            want("robot-motion.service")
        elif path == "src/robot_battery.py":
            want("robot-battery.service")
        elif path == "src/gamepad_teleop.py" or path.startswith("src/control/"):
            want("gamepad-teleop.service")
            want("robot-motion.service")
            want("robot-voice.service")
        elif path == "src/robot_vision.py":
            want("robot-vision.service")
        elif path == "src/robot_sensors.py":
            want("robot-sensors.service")
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
            want("robot-motion.service")
            want("gamepad-teleop.service")
        elif path == "src/drivers/camera.py":
            want("robot-camera.service")
            want("robot-vision.service")
        elif path == "src/drivers/range.py":
            want("robot-sensors.service")
        elif path == "src/drivers/respeaker.py":
            want("robot-voice.service")
        elif path.startswith("src/drivers/"):
            want("robot-brain.service")
            want("gamepad-teleop.service")
            want("robot-camera.service")
            want("robot-vision.service")
            want("robot-voice.service")
        elif path == "src/config/sensors.py":
            want("robot-sensors.service")
            want("robot-motion.service")
        elif path == "src/config/drive_tuning.py":
            want("gamepad-teleop.service")
            want("robot-motion.service")
            want("robot-web-dashboard.service")
        elif path.startswith("src/config/"):
            want("gamepad-teleop.service")
            want("robot-vision.service")
            want("robot-voice.service")
            want("robot-sensors.service")
            want("robot-web-dashboard.service")
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

    def test_dashboard_restart_defers_systemctl_when_status_file_set(self):
        body = SCRIPT.read_text()
        start = body.index('if [[ -n "${ROBOT_PET_REDEPLOY_STATUS_FILE:-}" ]]; then')
        end = body.index("    else", start)
        status_file_branch = body[start:end]
        self.assertIn('"restart_dashboard":true', status_file_branch)
        self.assertNotIn("systemctl restart", status_file_branch)
        self.assertIn("sudo systemctl restart --no-block robot-web-dashboard.service", body)

    def test_voice_paths_restart_voice_only(self):
        services = planned_services(["src/voice/assistant.py"])
        self.assertEqual(services, {"robot-voice.service"})

    def test_control_paths_restart_voice_for_calibration_constants(self):
        services = planned_services(["src/control/motion_intent.py"])
        self.assertIn("robot-voice.service", services)
        self.assertIn("robot-motion.service", services)
        self.assertIn("gamepad-teleop.service", services)

    def test_config_paths_restart_dashboard_too(self):
        services = planned_services(["src/config/voice.py"])
        self.assertIn("robot-web-dashboard.service", services)
        self.assertIn("robot-voice.service", services)

    def test_drive_tuning_config_restarts_motion_and_teleop(self):
        # robot-motion and gamepad-teleop both read drive_tuning at startup.
        services = planned_services(["src/config/drive_tuning.py"])
        self.assertEqual(
            services,
            {"gamepad-teleop.service", "robot-motion.service", "robot-web-dashboard.service"},
        )

    def test_pyproject_restarts_all_services(self):
        self.assertEqual(planned_services(["pyproject.toml"]), ALL_SERVICES)

    def test_battery_service_paths_restart_battery(self):
        self.assertEqual(planned_services(["src/robot_battery.py"]), {"robot-battery.service"})

    def test_setup_installs_and_manages_battery_service(self):
        body = SETUP.read_text()

        self.assertIn('for service_file in "$REPO_DIR/systemd/"*.service', body)
        self.assertIn('sudo install -m 0644 "$service_file"', body)
        self.assertIn("robot-battery.service", body)
        self.assertIn("$SYSTEMCTL_PATH enable robot-battery.service", body)
        self.assertIn("$SYSTEMCTL_PATH start robot-battery.service", body)
        self.assertIn("$SYSTEMCTL_PATH stop robot-battery.service", body)
        self.assertIn("$SYSTEMCTL_PATH restart robot-battery.service", body)

    def test_setup_does_not_fail_when_amixer_fails(self):
        body = SETUP.read_text()

        self.assertIn("if amixer -c 0 sset 'PCM' 100% >/dev/null; then", body)
        self.assertIn("WARNING: could not set PCM volume with amixer; continuing", body)

    def test_setup_installs_respeaker_usb_permissions(self):
        body = SETUP.read_text()

        self.assertIn("/etc/udev/rules.d/99-robot-pet-respeaker.rules", body)
        self.assertIn('ATTR{idVendor}=="2886"', body)
        self.assertIn('ATTR{idProduct}=="001e"', body)
        self.assertIn('GROUP="audio"', body)
        self.assertIn("udevadm control --reload-rules", body)
        self.assertIn("udevadm trigger --action=add --subsystem-match=usb", body)

    def test_docs_only_plans_no_restarts(self):
        self.assertEqual(
            planned_services(["docs/ARCHITECTURE.md", "README.md"]),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
