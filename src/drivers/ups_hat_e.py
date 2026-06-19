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
        id_value = self.bus.read_byte_data(self.address, REG_ID)
        power_on_value = self.bus.read_byte_data(self.address, REG_POWER_ON)
        charge_state = self.bus.read_byte_data(self.address, REG_CHARGE_STATE)
        comm_state = self.bus.read_byte_data(self.address, REG_COMM_STATE)

        vbus_mv, vbus_mv_low, vbus_mv_high = self._read_u16(REG_USB_C_VBUS_MV)
        vbus_ma, vbus_ma_low, vbus_ma_high = self._read_u16(REG_USB_C_VBUS_MA)
        vbus_mw, vbus_mw_low, vbus_mw_high = self._read_u16(REG_USB_C_VBUS_MW)
        battery_mv, battery_mv_low, battery_mv_high = self._read_u16(REG_BATTERY_MV)
        battery_ma_raw, battery_ma_low, battery_ma_high = self._read_u16(REG_BATTERY_MA)
        battery_percent, battery_percent_low, battery_percent_high = self._read_u16(REG_BATTERY_PERCENT)
        remaining_mah, remaining_mah_low, remaining_mah_high = self._read_u16(REG_BATTERY_REMAINING_MAH)
        runtime_min, runtime_min_low, runtime_min_high = self._read_u16(REG_BATTERY_RUNTIME_MIN)
        charge_time_min, charge_time_min_low, charge_time_min_high = self._read_u16(REG_BATTERY_CHARGE_TIME_MIN)

        cells = []
        raw_cells = []
        for register in (REG_CELL1_MV, REG_CELL2_MV, REG_CELL3_MV, REG_CELL4_MV):
            cell_mv, low, high = self._read_u16(register)
            cells.append(cell_mv)
            raw_cells.append((register, low, high))

        raw = {
            REG_ID: id_value,
            REG_POWER_ON: power_on_value,
            REG_CHARGE_STATE: charge_state,
            REG_COMM_STATE: comm_state,
            REG_USB_C_VBUS_MV: vbus_mv_low,
            REG_USB_C_VBUS_MV + 1: vbus_mv_high,
            REG_USB_C_VBUS_MA: vbus_ma_low,
            REG_USB_C_VBUS_MA + 1: vbus_ma_high,
            REG_USB_C_VBUS_MW: vbus_mw_low,
            REG_USB_C_VBUS_MW + 1: vbus_mw_high,
            REG_BATTERY_MV: battery_mv_low,
            REG_BATTERY_MV + 1: battery_mv_high,
            REG_BATTERY_MA: battery_ma_low,
            REG_BATTERY_MA + 1: battery_ma_high,
            REG_BATTERY_PERCENT: battery_percent_low,
            REG_BATTERY_PERCENT + 1: battery_percent_high,
            REG_BATTERY_REMAINING_MAH: remaining_mah_low,
            REG_BATTERY_REMAINING_MAH + 1: remaining_mah_high,
            REG_BATTERY_RUNTIME_MIN: runtime_min_low,
            REG_BATTERY_RUNTIME_MIN + 1: runtime_min_high,
            REG_BATTERY_CHARGE_TIME_MIN: charge_time_min_low,
            REG_BATTERY_CHARGE_TIME_MIN + 1: charge_time_min_high,
            **{register: low for register, low, _high in raw_cells},
            **{register + 1: high for register, _low, high in raw_cells},
        }

        return UpsHatEReading(
            device_id=id_value,
            power_on_register=power_on_value,
            charge_state=charge_state,
            comm_state=comm_state,
            vbus_mv=vbus_mv,
            vbus_ma=signed_16(vbus_ma),
            vbus_mw=vbus_mw,
            battery_mv=battery_mv,
            battery_ma=signed_16(battery_ma_raw),
            battery_percent=battery_percent,
            remaining_mah=remaining_mah,
            runtime_min=available_minutes(runtime_min),
            charge_time_min=available_minutes(charge_time_min),
            cells_mv=tuple(cells),
            raw=raw,
        )

    def cleanup(self) -> None:
        self.bus.close()

    def _read_u16(self, register: int) -> tuple[int, int, int]:
        low = self.bus.read_byte_data(self.address, register)
        high = self.bus.read_byte_data(self.address, register + 1)
        return (high << 8) | low, low, high


def signed_16(value: int) -> int:
    if value & 0x8000:
        return value - 0x10000
    return value


def available_minutes(value: int) -> int | None:
    return None if value == UNAVAILABLE_U16 else value
