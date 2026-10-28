"""Minimal Linux uinput / evdev access through ioctls (no python-evdev needed).

Struct layouts from linux/uinput.h and linux/input.h (64-bit):
  uinput_setup      { input_id {u16 bustype, vendor, product, version}; char name[80]; u32 ff_effects_max }
  uinput_abs_setup  { u16 code; (2 pad) input_absinfo {s32 value, min, max, fuzz, flat, resolution} }
  input_event       { timeval {s64 sec, s64 usec}; u16 type, code; s32 value }
"""
import fcntl
import os
import struct
import time
from dataclasses import dataclass


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def _IO(typ, nr):
    return _IOC(0, typ, nr, 0)


def _IOW(typ, nr, size):
    return _IOC(1, typ, nr, size)


SETUP = struct.Struct("HHHH80sI")
ABS_SETUP = struct.Struct("Hxxiiiiii")
EVENT = struct.Struct("qqHHi")

UI_DEV_CREATE = _IO("U", 1)
UI_DEV_DESTROY = _IO("U", 2)
UI_DEV_SETUP = _IOW("U", 3, SETUP.size)
UI_ABS_SETUP = _IOW("U", 4, ABS_SETUP.size)
UI_SET_EVBIT = _IOW("U", 100, 4)
UI_SET_KEYBIT = _IOW("U", 101, 4)
UI_SET_ABSBIT = _IOW("U", 103, 4)
EVIOCGRAB = _IOW("E", 0x90, 4)

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BUS_USB, BUS_VIRTUAL = 0x03, 0x06

ABS = {"ABS_X": 0x00, "ABS_Y": 0x01, "ABS_Z": 0x02, "ABS_RX": 0x03, "ABS_RY": 0x04, "ABS_RZ": 0x05,
       "ABS_THROTTLE": 0x06, "ABS_RUDDER": 0x07, "ABS_WHEEL": 0x08, "ABS_GAS": 0x09, "ABS_BRAKE": 0x0A,
       "ABS_HAT0X": 0x10, "ABS_HAT0Y": 0x11}
BTN = {"BTN_SOUTH": 0x130, "BTN_EAST": 0x131, "BTN_NORTH": 0x133, "BTN_WEST": 0x134, "BTN_TL": 0x136,
       "BTN_TR": 0x137, "BTN_SELECT": 0x13A, "BTN_START": 0x13B, "BTN_MODE": 0x13C, "BTN_THUMBL": 0x13D,
       "BTN_THUMBR": 0x13E}
BTN |= {"BTN_A": BTN["BTN_SOUTH"], "BTN_B": BTN["BTN_EAST"], "BTN_X": BTN["BTN_NORTH"], "BTN_Y": BTN["BTN_WEST"]}
BTN |= {f"BTN_TRIGGER_HAPPY{i}": 0x2C0 + i - 1 for i in range(1, 41)}   # generic extra buttons


@dataclass(frozen=True)
class AbsAxis:
    code: int
    minimum: int
    maximum: int


class VirtualDevice:
    """A uinput device. Values are written with emit(); sync() ends a report."""

    def __init__(self, name, vendor, product, version, bustype, axes: list[AbsAxis], buttons: list[int],
                 path="/dev/uinput"):
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            if axes:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            if buttons:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for b in buttons:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, b)
            for a in axes:
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, a.code)
                # fuzz 0 and flat 0: every 0.01 mm reaches the game; deadzones are applied here
                fcntl.ioctl(self.fd, UI_ABS_SETUP, ABS_SETUP.pack(a.code, 0, a.minimum, a.maximum, 0, 0, 0))
            fcntl.ioctl(self.fd, UI_DEV_SETUP, SETUP.pack(bustype, vendor, product, version, name.encode()[:79], 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self.name = name
        self._pending = bytearray()

    def emit(self, typ, code, value):
        self._pending += EVENT.pack(0, 0, typ, code, value)

    def sync(self):
        """Send the pending events as one report; returns the monotonic time of the write."""
        self._pending += EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
        os.write(self.fd, self._pending)
        self._pending.clear()
        return time.monotonic()

    def close(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def find_event_node(name, timeout=2.0):
    """/dev/input/eventN of the input device with this name (e.g. our virtual pad), or None."""
    deadline = time.monotonic() + timeout
    while True:
        for entry in sorted(os.listdir("/sys/class/input")):
            if not entry.startswith("event"):
                continue
            try:
                with open(f"/sys/class/input/{entry}/device/name") as f:
                    if f.read().strip() == name:
                        return f"/dev/input/{entry}"
            except OSError:
                continue
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def read_events(fd):
    """Yield (timestamp seconds, type, code, value) for the events currently readable from an evdev fd."""
    try:
        data = os.read(fd, EVENT.size * 64)
    except BlockingIOError:
        return
    for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
        sec, usec, typ, code, value = EVENT.unpack_from(data, off)
        yield sec + usec / 1e6, typ, code, value
