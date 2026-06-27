#!/usr/bin/env python3
"""
Check a Slamtec RPLIDAR A1 over USB serial.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/rplidar-a1-scan.py
    python scripts/diagnostics/rplidar-a1-scan.py --port /dev/ttyUSB0
    python scripts/diagnostics/rplidar-a1-scan.py --scan-seconds 10 --samples 50
"""

import argparse
import os
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

SYNC_BYTE = 0xA5
CMD_STOP = 0x25
CMD_SCAN = 0x20
CMD_GET_INFO = 0x50
CMD_GET_HEALTH = 0x52

DESCRIPTOR_START = b"\xA5\x5A"
INFO_RESPONSE_TYPE = 0x04
HEALTH_RESPONSE_TYPE = 0x06
SCAN_RESPONSE_TYPE = 0x81

COMMON_PORTS = (
    "/dev/rplidar",
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyACM0",
    "/dev/ttyACM1",
)


def candidate_ports():
    ports = [port for port in COMMON_PORTS if os.path.exists(port)]
    if list_ports is None:
        return ports

    for port_info in list_ports.comports():
        if port_info.device not in ports and (
            "USB" in port_info.device or "ACM" in port_info.device
        ):
            ports.append(port_info.device)
    return ports


def send_command(lidar, command):
    lidar.write(bytes((SYNC_BYTE, command)))
    lidar.flush()


def read_exact(lidar, size, label):
    data = lidar.read(size)
    if len(data) != size:
        raise RuntimeError(f"timed out reading {label}")
    return data


def read_descriptor(lidar):
    header = read_exact(lidar, 2, "response descriptor header")
    if header != DESCRIPTOR_START:
        raise RuntimeError(f"bad response descriptor header: {header.hex(' ')}")

    data = read_exact(lidar, 5, "response descriptor")
    response_len = data[0] | (data[1] << 8) | (data[2] << 16) | ((data[3] & 0x3F) << 24)
    send_mode = data[3] >> 6
    return response_len, send_mode, data[4]


def stop_scan(lidar):
    send_command(lidar, CMD_STOP)
    time.sleep(0.1)


def read_device_info(lidar):
    send_command(lidar, CMD_GET_INFO)
    response_len, _send_mode, response_type = read_descriptor(lidar)
    if response_type != INFO_RESPONSE_TYPE or response_len != 20:
        raise RuntimeError("unexpected device-info response")

    data = read_exact(lidar, 20, "device info")
    return {
        "model": data[0],
        "firmware": f"{data[2]}.{data[1]}",
        "hardware": data[3],
        "serial": data[4:].hex(),
    }


def read_health(lidar):
    send_command(lidar, CMD_GET_HEALTH)
    response_len, _send_mode, response_type = read_descriptor(lidar)
    if response_type != HEALTH_RESPONSE_TYPE or response_len != 3:
        raise RuntimeError("unexpected health response")

    data = read_exact(lidar, 3, "health")
    return data[0], data[1] | (data[2] << 8)


def read_scan_points(lidar, samples, scan_seconds):
    points = []
    valid_nodes = 0
    bad_nodes = 0
    deadline = time.monotonic() + scan_seconds

    while len(points) < samples and time.monotonic() < deadline:
        data = read_exact(lidar, 5, "scan point")
        start_flag = data[0] & 0x01
        inverse_start_flag = (data[0] >> 1) & 0x01
        check_bit = data[1] & 0x01
        if start_flag == inverse_start_flag or check_bit != 1:
            bad_nodes += 1
            continue

        valid_nodes += 1
        distance_mm = (data[3] | (data[4] << 8)) / 4.0
        if distance_mm <= 0:
            continue

        points.append(
            {
                "quality": data[0] >> 2,
                "angle": ((data[1] >> 1) | (data[2] << 7)) / 64.0,
                "distance_mm": distance_mm,
            }
        )

    return points, valid_nodes, bad_nodes


def run_probe(port, args):
    print(f"Connecting to RPLIDAR at {port} ({args.baud} baud)...")
    lidar = serial.Serial(port, args.baud, timeout=args.timeout)

    try:
        if not args.no_motor_control:
            lidar.setDTR(False)

        stop_scan(lidar)
        lidar.reset_input_buffer()

        info = read_device_info(lidar)
        print(
            "Device: "
            f"model={info['model']} "
            f"firmware={info['firmware']} "
            f"hardware={info['hardware']} "
            f"serial={info['serial']}"
        )

        status, error_code = read_health(lidar)
        status_text = {0: "OK", 1: "WARNING", 2: "ERROR"}.get(status, f"UNKNOWN {status}")
        print(f"Health: {status_text} (error code {error_code})")
        if status == 2:
            raise RuntimeError("RPLIDAR reports health ERROR")

        print("")
        print(f"Starting scan for up to {args.scan_seconds:.1f}s...")
        time.sleep(args.spinup_seconds)
        lidar.reset_input_buffer()
        send_command(lidar, CMD_SCAN)
        response_len, _send_mode, response_type = read_descriptor(lidar)
        if response_type != SCAN_RESPONSE_TYPE or response_len != 5:
            raise RuntimeError("unexpected scan response")

        points, valid_nodes, bad_nodes = read_scan_points(
            lidar, args.samples, args.scan_seconds
        )
    finally:
        try:
            stop_scan(lidar)
            if not args.no_motor_control:
                lidar.setDTR(True)
        finally:
            lidar.close()

    print(f"Received {valid_nodes} valid scan node(s), {bad_nodes} rejected node(s).")
    if not points:
        raise RuntimeError("scan stream started, but no nonzero distances arrived")

    print("")
    print(" sample  angle_deg  distance_mm  quality")
    print(" ------  ---------  -----------  -------")
    for index, point in enumerate(points, start=1):
        print(
            f"{index:>7}  "
            f"{point['angle']:>9.1f}  "
            f"{point['distance_mm']:>11.0f}  "
            f"{point['quality']:>7}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Check an RPLIDAR A1 over USB serial."
    )
    parser.add_argument("--port", default=None, help="Serial port (default: auto)")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--timeout", type=float, default=1.0, help="Serial timeout seconds")
    parser.add_argument(
        "--spinup-seconds",
        type=float,
        default=1.0,
        help="Seconds to let the lidar motor spin up before scanning",
    )
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=5.0,
        help="Maximum seconds to read scan data",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=20,
        help="Nonzero distance samples to print",
    )
    parser.add_argument(
        "--no-motor-control",
        action="store_true",
        help="Do not toggle DTR for the USB adapter motor control",
    )
    args = parser.parse_args()

    if serial is None:
        print("ERROR: pyserial not installed.")
        print("Run: pip install -e . from the repo venv on the Pi")
        sys.exit(1)

    print("=== RPLIDAR A1 USB diagnostic ===")
    print("")

    ports = [args.port] if args.port else candidate_ports()
    if not ports:
        print("ERROR: no likely USB serial ports found.")
        print("")
        print("Troubleshooting:")
        print("  - Is the RPLIDAR USB adapter plugged into the Pi?")
        print("  - Does ls /dev/ttyUSB* or ls /dev/ttyACM* show a port?")
        print("  - Is your user in the dialout group? (setup.sh should do this)")
        sys.exit(1)

    last_error = None
    for port in ports:
        try:
            run_probe(port, args)
        except (OSError, serial.SerialException, RuntimeError) as error:
            last_error = error
            if args.port:
                break
            print(f"No RPLIDAR response on {port}: {error}")
            print("")
            continue

        print("")
        print("OK — RPLIDAR is responding and returning distance samples.")
        return

    print(f"ERROR: could not prove the RPLIDAR is working: {last_error}")
    print("")
    print("Troubleshooting:")
    print("  - Try an explicit port: python scripts/diagnostics/rplidar-a1-scan.py --port /dev/ttyUSB0")
    print("  - Check the USB cable and adapter board.")
    print("  - The lidar head should spin during the scan.")
    print("  - If it spins before the script runs, try --no-motor-control.")
    sys.exit(1)


if __name__ == "__main__":
    main()
