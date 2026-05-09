"""
Xbox 360 controller driver using evdev.

Reads joystick/button inputs and provides a clean interface.
Works with wired controllers or wireless via the USB receiver.

The controller shows up as /dev/input/eventX - this driver finds it automatically.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable

try:
    from evdev import InputDevice, ecodes, list_devices
except ModuleNotFoundError:
    InputDevice = None
    list_devices = None

    class ecodes:
        EV_KEY = 1
        EV_ABS = 3
        ABS_X = 0
        ABS_Y = 1
        ABS_Z = 2
        ABS_RX = 3
        ABS_RY = 4
        ABS_RZ = 5
        ABS_HAT0X = 16
        ABS_HAT0Y = 17
        BTN_SOUTH = 304
        BTN_EAST = 305
        BTN_WEST = 307
        BTN_NORTH = 308
        BTN_TL = 310
        BTN_TR = 311
        BTN_SELECT = 314
        BTN_START = 315
        BTN_MODE = 316
        BTN_THUMBL = 317
        BTN_THUMBR = 318


@dataclass
class ControllerState:
    """Current state of all controller inputs."""
    
    # Sticks: -1.0 to 1.0
    left_stick_x: float = 0.0
    left_stick_y: float = 0.0
    right_stick_x: float = 0.0
    right_stick_y: float = 0.0
    
    # Triggers: 0.0 to 1.0
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    
    # D-pad: -1, 0, or 1
    dpad_x: int = 0
    dpad_y: int = 0
    
    # Buttons: True if pressed
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    lb: bool = False
    rb: bool = False
    back: bool = False
    start: bool = False
    guide: bool = False
    left_stick_click: bool = False
    right_stick_click: bool = False


class ControllerDriver:
    """
    Xbox 360 controller input handler.
    
    Runs a background thread that reads events and updates state.
    """
    
    # Xbox 360 controller identifiers
    XBOX_NAMES = ["xbox", "x-box", "microsoft"]
    
    # Axis codes and ranges (Xbox 360)
    AXIS_LEFT_X = ecodes.ABS_X
    AXIS_LEFT_Y = ecodes.ABS_Y
    AXIS_RIGHT_X = ecodes.ABS_RX
    AXIS_RIGHT_Y = ecodes.ABS_RY
    AXIS_LEFT_TRIGGER = ecodes.ABS_Z
    AXIS_RIGHT_TRIGGER = ecodes.ABS_RZ
    AXIS_DPAD_X = ecodes.ABS_HAT0X
    AXIS_DPAD_Y = ecodes.ABS_HAT0Y
    
    # Stick range
    STICK_MIN = -32768
    STICK_MAX = 32767
    
    # Trigger range
    TRIGGER_MAX = 255
    
    # Button codes
    BTN_A = ecodes.BTN_SOUTH
    BTN_B = ecodes.BTN_EAST
    BTN_X = ecodes.BTN_WEST
    BTN_Y = ecodes.BTN_NORTH
    BTN_LB = ecodes.BTN_TL
    BTN_RB = ecodes.BTN_TR
    BTN_BACK = ecodes.BTN_SELECT
    BTN_START = ecodes.BTN_START
    BTN_GUIDE = ecodes.BTN_MODE
    BTN_LEFT_STICK = ecodes.BTN_THUMBL
    BTN_RIGHT_STICK = ecodes.BTN_THUMBR
    
    def __init__(
        self,
        deadzone: float = 0.1,
        device_path: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Initialize the controller driver.
        
        Args:
            deadzone: Stick deadzone (values below this are treated as 0)
        """
        self.deadzone = deadzone
        self.device_path = device_path
        self.clock = clock
        self.state = ControllerState()
        self.device: InputDevice | None = None
        self.last_event_at: float | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._on_disconnect: Callable[[], None] | None = None
    
    def find_controller(self) -> str | None:
        """Find an Xbox 360 controller device path."""
        if self.device_path:
            return self.device_path

        if list_devices is None:
            raise RuntimeError("evdev is not installed; run setup.sh to install controller support.")

        for path in list_devices():
            device = None
            try:
                device = InputDevice(path)
                name = device.name.lower()
                if any(xbox in name for xbox in self.XBOX_NAMES):
                    return path
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied reading {path}; run setup.sh and log back in.") from exc
            except Exception:
                continue
            finally:
                if device is not None:
                    device.close()
        return None
    
    def connect(self) -> bool:
        """
        Find and connect to an Xbox 360 controller.
        
        Returns:
            True if connected, False if no controller found.
        """
        path = self.find_controller()
        if path is None:
            return False
        
        if InputDevice is None:
            raise RuntimeError("evdev is not installed; run setup.sh to install controller support.")

        try:
            self.device = InputDevice(path)
        except PermissionError as exc:
            raise RuntimeError(f"Permission denied reading {path}; run setup.sh and log back in.") from exc
        except OSError:
            return False

        self.state = ControllerState()
        self.last_event_at = self.clock()
        return True

    def input_age(self, now: float | None = None) -> float | None:
        if self.last_event_at is None:
            return None
        return (now if now is not None else self.clock()) - self.last_event_at
    
    def start(self, on_disconnect: Callable[[], None] | None = None):
        """
        Start reading controller input in a background thread.
        
        Args:
            on_disconnect: Optional callback when controller disconnects
        """
        if self.device is None:
            raise RuntimeError("Not connected - call connect() first")
        
        self._on_disconnect = on_disconnect
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop reading controller input."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
    
    def _apply_deadzone(self, value: float) -> float:
        """Apply deadzone to stick value."""
        if abs(value) < self.deadzone:
            return 0.0
        return value
    
    def _normalize_stick(self, value: int) -> float:
        """Convert raw stick value to -1.0..1.0 range."""
        normalized = (value - self.STICK_MIN) / (self.STICK_MAX - self.STICK_MIN)
        normalized = (normalized * 2) - 1  # Convert 0..1 to -1..1
        return self._apply_deadzone(normalized)
    
    def _normalize_trigger(self, value: int) -> float:
        """Convert raw trigger value to 0.0..1.0 range."""
        return value / self.TRIGGER_MAX
    
    def _read_loop(self):
        """Background thread that reads controller events."""
        try:
            for event in self.device.read_loop():
                if not self._running:
                    break

                self.last_event_at = self.clock()
                
                if event.type == ecodes.EV_ABS:
                    self._handle_axis(event.code, event.value)
                elif event.type == ecodes.EV_KEY:
                    self._handle_button(event.code, event.value)
        except OSError:
            # Controller disconnected
            if self._on_disconnect:
                self._on_disconnect()
    
    def _handle_axis(self, code: int, value: int):
        """Handle axis (stick/trigger/dpad) event."""
        if code == self.AXIS_LEFT_X:
            self.state.left_stick_x = self._normalize_stick(value)
        elif code == self.AXIS_LEFT_Y:
            self.state.left_stick_y = self._normalize_stick(value)
        elif code == self.AXIS_RIGHT_X:
            self.state.right_stick_x = self._normalize_stick(value)
        elif code == self.AXIS_RIGHT_Y:
            self.state.right_stick_y = self._normalize_stick(value)
        elif code == self.AXIS_LEFT_TRIGGER:
            self.state.left_trigger = self._normalize_trigger(value)
        elif code == self.AXIS_RIGHT_TRIGGER:
            self.state.right_trigger = self._normalize_trigger(value)
        elif code == self.AXIS_DPAD_X:
            self.state.dpad_x = value
        elif code == self.AXIS_DPAD_Y:
            self.state.dpad_y = value
    
    def _handle_button(self, code: int, value: int):
        """Handle button press/release event."""
        pressed = value == 1
        
        if code == self.BTN_A:
            self.state.a = pressed
        elif code == self.BTN_B:
            self.state.b = pressed
        elif code == self.BTN_X:
            self.state.x = pressed
        elif code == self.BTN_Y:
            self.state.y = pressed
        elif code == self.BTN_LB:
            self.state.lb = pressed
        elif code == self.BTN_RB:
            self.state.rb = pressed
        elif code == self.BTN_BACK:
            self.state.back = pressed
        elif code == self.BTN_START:
            self.state.start = pressed
        elif code == self.BTN_GUIDE:
            self.state.guide = pressed
        elif code == self.BTN_LEFT_STICK:
            self.state.left_stick_click = pressed
        elif code == self.BTN_RIGHT_STICK:
            self.state.right_stick_click = pressed
    
    def cleanup(self):
        """Stop reading and release resources."""
        self.stop()
        if self.device is not None:
            self.device.close()
        self.device = None
