#!/usr/bin/env python3
"""
power.py — read the Pi's battery backup (Waveshare UPS HAT (C)).

The UPS carries an INA219 on the battery line, on I2C bus 1 at address 0x43. It
reports the battery voltage and, across a 0.01 ohm shunt, the current: positive
while charging, negative while the battery is feeding the Pi.

Measured on 2026-10-09 with the Pi Zero 2 W, the OLED and the 4G modem attached:
about 0.5 A from the battery when idle (~2 W), peaking at 1.07 A while the modem
transmits; 0.44 A going in while charging near full.

No dependencies: it talks to /dev/i2c-1 directly, with a combined write+read so
several processes (monitor, display, power guard) can poll the chip at once.

  python3 power.py        # print one reading
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from dataclasses import dataclass

I2C_BUS = int(os.environ.get("RIKO_UPS_I2C_BUS", "1"))
I2C_ADDR = int(os.environ.get("RIKO_UPS_I2C_ADDR", "0x43"), 0)
SHUNT_OHMS = 0.01
BATTERY_MAH = float(os.environ.get("RIKO_UPS_BATTERY_MAH", "1000"))
FULL_V, EMPTY_V = 4.2, 3.0          # single Li-po cell
ON_BATTERY_BELOW_MA = -50.0         # more negative than this = running on the battery
CHARGING_ABOVE_MA = 50.0

_I2C_RDWR, _I2C_M_RD = 0x0707, 0x0001


class _Msg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8))]


class _Rdwr(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_Msg)), ("nmsgs", ctypes.c_uint32)]


@dataclass(frozen=True)
class Ups:
    volts: float      # battery voltage (sags ~0.1 V under load)
    ma: float         # + charging, - discharging

    @property
    def on_battery(self) -> bool:
        return self.ma < ON_BATTERY_BELOW_MA

    @property
    def charging(self) -> bool:
        return self.ma > CHARGING_ABOVE_MA

    @property
    def percent(self) -> int:
        """Rough charge from voltage alone; reads a little low while under load."""
        return max(0, min(100, round((self.volts - EMPTY_V) / (FULL_V - EMPTY_V) * 100)))

    @property
    def minutes_left(self) -> int | None:
        """Very rough runtime at the present draw; None unless on battery."""
        if not self.on_battery:
            return None
        return round(BATTERY_MAH * self.percent / 100 / -self.ma * 60)


def _read_reg(fd: int, reg: int) -> bytes:
    out = (ctypes.c_uint8 * 1)(reg)
    back = (ctypes.c_uint8 * 2)()
    msgs = (_Msg * 2)(_Msg(I2C_ADDR, 0, 1, out), _Msg(I2C_ADDR, _I2C_M_RD, 2, back))
    fcntl.ioctl(fd, _I2C_RDWR, _Rdwr(msgs, 2))
    return bytes(back)


def decode(bus_raw: bytes, shunt_raw: bytes) -> Ups:
    volts = (struct.unpack(">H", bus_raw)[0] >> 3) * 0.004
    shunt_mv = struct.unpack(">h", shunt_raw)[0] * 0.01
    return Ups(volts=round(volts, 3), ma=round(shunt_mv / SHUNT_OHMS, 1))


def read_ups() -> Ups | None:
    """One reading, or None if there is no UPS (or the bus can't be read)."""
    try:
        fd = os.open(f"/dev/i2c-{I2C_BUS}", os.O_RDWR)
    except OSError:
        return None
    try:
        return decode(_read_reg(fd, 0x02), _read_reg(fd, 0x01))
    except OSError:
        return None
    finally:
        os.close(fd)


if __name__ == "__main__":
    ups = read_ups()
    if ups is None:
        print("no UPS found on I2C bus %d at 0x%02x" % (I2C_BUS, I2C_ADDR))
    else:
        state = "on battery" if ups.on_battery else "charging" if ups.charging else "on wall power, battery full"
        left = f", ~{ups.minutes_left} min left" if ups.minutes_left is not None else ""
        print(f"{ups.volts:.3f} V  {ups.ma:+.0f} mA  ~{ups.percent}%  ({state}{left})")
