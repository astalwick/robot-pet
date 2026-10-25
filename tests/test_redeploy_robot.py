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
    "robot-state-publisher.service",
    "robot-battery.service",
    "robot-pi-battery.service",
    "robot-motion.service",
    "robot-twist-mux.service",
    "robot-lidar.service",
    "robot-nav.service",
    "robot-nav-bridge.service",
    "robot-lidar-status.service",
    "robot-camera.service",
    "gamepad-teleop.service",
    "robot-vision.service",
    "robot-voice.service",
    "robot-sensors.service",
    "robot-web-dashboard.service",
    "robot-foxglove.service",
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
        elif path == "src/robot_telemetry.py":
            want("robot-telemetry.service")
            want("robot-battery.service")
            want("robot-pi-battery.service")
        elif path.startswith("src/telemetry/"):
            want_all()
        elif path == "src/robot_camera.py":
            want("robot-camera.service")
        elif path in ("src/robot_motion.py", "src/ros_nodes/motion_node.py"):
            want("robot-motion.service")
        elif path == "src/robot_battery.py":
            want("robot-battery.service")
        elif path in ("src/robot_pi_battery.py", "src/ros_nodes/pi_battery_node.py"):
            want("robot-pi-battery.service")
        elif path == "src/ros_nodes/lidar_status_node.py":
            want("robot-lidar-status.service")
        elif path in ("src/robot_nav.py", "src/ros_nodes/nav_node.py"):
            want("robot-nav-bridge.service")
        elif path == "src/ros_nodes/gamepad_teleop_node.py":
            want("gamepad-teleop.service")
        elif path in ("src/gamepad_teleop.py", "src/robot_model.py") or path.startswith("src/control/"):
            want("gamepad-teleop.service")
            want("robot-motion.service")
            want("robot-voice.service")
        elif path == "src/robot_vision.py":
            want("robot-vision.service")
        elif path in ("src/robot_sensors.py", "src/ros_nodes/sensors_node.py"):
            want("robot-sensors.service")
        elif path == "src/ros_nodes/ros_messages.py":
            want("robot-sensors.service")
            want("robot-motion.service")
            want("robot-pi-battery.service")
        elif path == "src/ros_nodes/service_thread.py":
            want("robot-sensors.service")
            want("robot-motion.service")
            want("gamepad-teleop.service")
            want("robot-pi-battery.service")
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
        elif path == "src/drivers/imu.py":
            want("robot-sensors.service")
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
        elif path in ("urdf/robot.urdf", "scripts/generate-urdf.py"):
            want("robot-state-publisher.service")
        elif path == "config/twist_mux.yaml":
            want("robot-twist-mux.service")
        elif path in ("config/nav.launch.py", "config/nav2.yaml", "config/slam_toolbox.yaml"):
            want("robot-nav.service")
        elif path == "config/rplidar.launch.py":
            want("robot-lidar.service")
        elif path == "scripts/ros-node.sh":
            want("robot-sensors.service")
            want("robot-motion.service")
            want("gamepad-teleop.service")
            want("robot-pi-battery.service")
            want("robot-lidar-status.service")
            want("robot-nav-bridge.service")
        elif path.startswith("systemd/"):
            want(Path(path).name)

    return services


class TestRedeployRobot(unittest.TestCase):
    def test_script_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_exits_when_already_up_to_date(self):
        body = SCRIPT.read_text()
        self.assertIn('echo "Already up to date on $branch."', body)
        self.assertRegex(body, r'Already up to date on \$branch\."\n\s+exit 0')

    def test_up_to_date_exit_requires_last_deploy_finished(self):
        # A redeploy that fast-forwarded but failed (e.g. red tests) must be
        # retried on the next run, not skipped as "already up to date".
        body = SCRIPT.read_text()
        self.assertIn("DEPLOYED_REV_FILE", body)
        self.assertIn('if [[ "$baseline_rev" == "$local_rev" ]]; then', body)
        self.assertIn("Retrying deploy on $branch", body)

    def test_deployed_rev_recorded_only_after_success(self):
        body = SCRIPT.read_text()
        record_index = body.index('> "$DEPLOYED_REV_FILE"')
        tests_index = body.index("Running tests...")
        restarts_index = body.index("Restarting robot services...")
        self.assertGreater(record_index, tests_index)
        self.assertGreater(record_index, restarts_index)

    def test_restart_planning_diffs_from_last_deployed_rev(self):
        body = SCRIPT.read_text()
        self.assertIn('changed_files="$(git diff --name-only "$baseline_rev" "$new_rev")"', body)

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

    def test_robot_model_restarts_everything_that_uses_wheel_geometry(self):
        services = planned_services(["src/robot_model.py"])
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

    def test_telemetry_shared_modules_restart_all_consumers(self):
        # telemetry/messages.py and socket_client.py are imported everywhere;
        # a schema change must restart the whole fleet.
        self.assertEqual(planned_services(["src/telemetry/messages.py"]), ALL_SERVICES)

    def test_telemetry_hub_paths_restart_hub_and_battery_services(self):
        self.assertEqual(
            planned_services(["src/robot_telemetry.py"]),
            {"robot-telemetry.service", "robot-battery.service", "robot-pi-battery.service"},
        )

    def test_imu_driver_restarts_sensors_service(self):
        self.assertEqual(planned_services(["src/drivers/imu.py"]), {"robot-sensors.service"})

    def test_setup_installs_and_manages_battery_service(self):
        body = SETUP.read_text()

        self.assertIn('for service_file in "$REPO_DIR/systemd/"*.service', body)
        self.assertIn('sudo install -m 0644 "$service_file"', body)
        self.assertIn("robot-battery.service", body)
        self.assertIn('for service in "${ROBOT_SERVICES[@]}"; do', body)
        self.assertIn("for verb in enable start stop restart; do", body)
        self.assertIn('echo "$USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH $verb $service"', body)

    def test_setup_service_lists_cover_every_unit(self):
        body = SETUP.read_text()
        units = {path.name for path in (REPO_ROOT / "systemd").glob("*.service")}
        for unit in units:
            self.assertIn(f"  {unit}\n", body, unit)
        self.assertEqual(units, ALL_SERVICES)

        self.assertEqual(planned_services(["src/ros_nodes/sensors_node.py"]), {"robot-sensors.service"})
        self.assertEqual(planned_services(["src/ros_nodes/motion_node.py"]), {"robot-motion.service"})
        self.assertEqual(
            planned_services(["src/ros_nodes/ros_messages.py"]),
            {"robot-sensors.service", "robot-motion.service", "robot-pi-battery.service"},
        )
        self.assertEqual(planned_services(["src/ros_nodes/gamepad_teleop_node.py"]), {"gamepad-teleop.service"})
        self.assertEqual(
            planned_services(["src/ros_nodes/service_thread.py"]),
            {"robot-sensors.service", "robot-motion.service", "gamepad-teleop.service", "robot-pi-battery.service"},
        )
        self.assertEqual(planned_services(["config/twist_mux.yaml"]), {"robot-twist-mux.service"})
        self.assertEqual(planned_services(["config/rplidar.launch.py"]), {"robot-lidar.service"})
        self.assertEqual(planned_services(["src/ros_nodes/pi_battery_node.py"]), {"robot-pi-battery.service"})
        self.assertEqual(planned_services(["src/ros_nodes/lidar_status_node.py"]), {"robot-lidar-status.service"})
        self.assertEqual(planned_services(["src/ros_nodes/nav_node.py"]), {"robot-nav-bridge.service"})
        self.assertEqual(planned_services(["config/nav.launch.py"]), {"robot-nav.service"})
        self.assertEqual(planned_services(["config/slam_toolbox.yaml"]), {"robot-nav.service"})
        self.assertEqual(planned_services(["urdf/robot.urdf"]), {"robot-state-publisher.service"})
        self.assertEqual(
            planned_services(["scripts/ros-node.sh"]),
            {
                "robot-sensors.service",
                "robot-motion.service",
                "gamepad-teleop.service",
                "robot-pi-battery.service",
                "robot-lidar-status.service",
                "robot-nav-bridge.service",
            },
        )

    def test_dashboard_restart_does_not_kill_systemctl_child(self):
        body = (REPO_ROOT / "systemd" / "robot-web-dashboard.service").read_text()

        self.assertIn("KillMode=mixed", body)

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
