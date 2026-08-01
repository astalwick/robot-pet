"""Waveshare UPS HAT (E) I2C reader."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


UPS_ADDRESS = 0x2D
GROVE_MUX_ADDRESS = 0x70

REG_ID = 0x00
REG_POWER_ON = 0x01
REG_CHARGE_STATE = 0x02
REG_COMM_STATE = 0x03
REG_USB_C_VBUS_MV = 0x10
REG_USB_C_VBUS_MA = 0x12
REG_USB_C_VBUS_MW = 0x14
REG_BATTERY_MV = 0x20
REG_BATTERY_MA = 0x22
REG_BATTERY_PERCENT = 0x24
REG_BATTERY_REMAINING_MAH = 0x26
REG_BATTERY_RUNTIME_MIN = 0x28
REG_BATTERY_CHARGE_TIME_MIN = 0x2A
REG_CELL1_MV = 0x30
REG_CELL2_MV = 0x32
REG_CELL3_MV = 0x34
REG_CELL4_MV = 0x36

UNAVAILABLE_U16 = 0xFFFF

CHARGE_STAGES = {
    0: "standby",
    1: "trickle",
    2: "constant_current",
    3: "constant_voltage",
    4: "charge_wait",
    5: "full",
    6: "charge_timeout",
}


@dataclass(frozen=True)
class UpsHatEReading:
    device_id: int
    power_on_register: int
    charge_state: int
    comm_state: int
    vbus_mv: int
    vbus_ma: int
    vbus_mw: int
    battery_mv: int
    battery_ma: int
    battery_percent: int
    remaining_mah: int
    runtime_min: int | None
    charge_time_min: int | None
    cells_mv: tuple[int, int, int, int]
    raw: dict[int, int]

    @property
    def charging(self) -> bool:
        return bool(self.charge_state & 0x80)

    @property
    def fast_charging(self) -> bool:
        return bool(self.charge_state & 0x40)

    @property
    def vbus_present(self) -> bool:
        return bool(self.charge_state & 0x20)

    @property
    def charge_stage(self) -> str:
        return CHARGE_STAGES.get(self.charge_state & 0x07, "unknown")

    @property
    def bq4050_ok(self) -> bool:
        return bool(self.comm_state & 0x02)

    @property
    def ip2368_ok(self) -> bool:
        return bool(self.comm_state & 0x01)


class UpsHatEDriver:
    def __init__(
        self,
        bus: int = 1,
        address: int = UPS_ADDRESS,
        bus_factory: Callable[[int], Any] | None = None,
    ):
        if bus_factory is None:
            from smbus2 import SMBus

            bus_factory = SMBus

        self.address = address
        self.bus = bus_factory(bus)

    def read(self) -> UpsHatEReading:
        # Block reads: one I2C transaction per register group instead of ~30
        # byte reads, and each 16-bit value arrives atomically (paired byte
        # reads could tear a value rolling across a byte boundary).
        status = self.bus.read_i2c_block_data(self.address, REG_ID, 4)
        vbus = self.bus.read_i2c_block_data(self.address, REG_USB_C_VBUS_MV, 6)
        battery = self.bus.read_i2c_block_data(self.address, REG_BATTERY_MV, 12)
        cells = self.bus.read_i2c_block_data(self.address, REG_CELL1_MV, 8)

        raw = {}
        for base, block in (
            (REG_ID, status),
            (REG_USB_C_VBUS_MV, vbus),
            (REG_BATTERY_MV, battery),
            (REG_CELL1_MV, cells),
        ):
            for offset, value in enumerate(block):
                raw[base + offset] = value

        return UpsHatEReading(
            device_id=status[0],
            power_on_register=status[1],
            charge_state=status[2],
            comm_state=status[3],
            vbus_mv=u16(vbus, 0),
            vbus_ma=signed_16(u16(vbus, 2)),
            vbus_mw=u16(vbus, 4),
            battery_mv=u16(battery, 0),
            battery_ma=signed_16(u16(battery, 2)),
            battery_percent=u16(battery, 4),
            remaining_mah=u16(battery, 6),
            runtime_min=available_minutes(u16(battery, 8)),
            charge_time_min=available_minutes(u16(battery, 10)),
            cells_mv=(u16(cells, 0), u16(cells, 2), u16(cells, 4), u16(cells, 6)),
            raw=raw,
        )

    def cleanup(self) -> None:
        self.bus.close()


def u16(block: list[int], offset: int) -> int:
    return block[offset] | (block[offset + 1] << 8)


def signed_16(value: int) -> int:
    if value & 0x8000:
        return value - 0x10000
    return value


def available_minutes(value: int) -> int | None:
    return None if value == UNAVAILABLE_U16 else value
