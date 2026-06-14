# Input Options

Ideas for controlling the robot wirelessly.

## Current: Xbox 360 Controller

Requires the USB wireless receiver dongle. Driver exists at `src/drivers/controller.py`.

## Alternatives

### PS5 DualSense via Bluetooth

- Works on recent Raspberry Pi OS (kernel 5.12+)
- Bluetooth pairing can be fiddly, may need re-pairing
- Would need to update `controller.py` for different button mappings

### iPhone Web App

- Pi runs a web server with virtual joystick UI
- iPhone opens browser, no app install needed
- WebSocket sends commands in real-time
- No pairing, no drivers, no dongles
- Can add camera feed and telemetry to the same UI
- Works for any guest with a phone

### 8BitDo Controller

- Excellent Linux Bluetooth support out of the box
- ~$25-40
- Various styles (SNES, Xbox-like, etc.)

### Buy Xbox 360 Wireless Receiver

- ~$15
- Just use the existing Xbox 360 controllers
