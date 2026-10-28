"""Physical key layout of the CX68 and its mapping to matrix slots.

Geometry is the vendor's own (cx68/data/layout_cx68.json, extracted from the iotdriver
bundle). Each key is matched to its matrix slot through the keyboard's current keymap
(layer 0), so the HUD follows remaps instead of assuming a fixed matrix.
"""
import json
from dataclasses import dataclass
from pathlib import Path

LAYOUT_FILE = Path(__file__).resolve().parents[1] / "cx68" / "data" / "layout_cx68.json"

# HID usage -> Linux input keycode (subset of the kernel's hid_keyboard[] table, covering
# every key on the CX68). Qt reports nativeScanCode() = Linux keycode + 8 on Wayland/X11.
HID_TO_LINUX = {
    **dict(zip(range(0x04, 0x1E), (30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25,
                                     16, 19, 31, 20, 22, 47, 17, 45, 21, 44))),
    **dict(zip(range(0x1E, 0x28), range(2, 12))),
    0x28: 28, 0x29: 1, 0x2A: 14, 0x2B: 15, 0x2C: 57, 0x2D: 12, 0x2E: 13, 0x2F: 26, 0x30: 27,
    0x31: 43, 0x33: 39, 0x34: 40, 0x35: 41, 0x36: 51, 0x37: 52, 0x38: 53, 0x39: 58,
    **dict(zip(range(0x3A, 0x46), (59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88))),
    0x49: 110, 0x4A: 102, 0x4B: 104, 0x4C: 111, 0x4D: 107, 0x4E: 109,
    0x4F: 106, 0x50: 105, 0x51: 108, 0x52: 103,
    0xE0: 29, 0xE1: 42, 0xE2: 56, 0xE3: 125, 0xE4: 97, 0xE5: 54, 0xE6: 100, 0xE7: 126,
}
LINUX_TO_HID = {v: k for k, v in HID_TO_LINUX.items()}


@dataclass
class Key:
    name: str
    value: int          # vendor "value": HID usage for plain keys, raw entry for others (Fn)
    x: float            # top-left, in layout units
    y: float
    w: float
    h: float
    slot: int | None = None
    usage: int | None = None


SHORT_NAMES = {"BackSpace": "Bksp", "(space)": "Space", "Caps Lock": "Caps", "r_Shift": "Shift",
               "r_Alt": "Alt", "r_Ctrl": "Ctrl", "Page Up": "PgUp", "Page Down": "PgDn", "Delete": "Del",
               "fn": "Fn"}


def entry_value(entry: bytes) -> int:
    """The vendor layout's `value` for a 4-byte keymap entry."""
    if entry[0] == 0 and entry[1] == 0 and entry[3] == 0:
        return entry[2]
    return int.from_bytes(entry, "big")


def load_keys(keymap_entries: list[bytes] | None = None) -> list[Key]:
    """Physical keys with their matrix slot. Slots come from the factory keymap (they are
    physical, so remapping a key never moves it). `usage` is the HID usage the key currently
    sends, from `keymap_entries` (layer 0 of the active profile) when given, for plain keys."""
    from cx68.keymap import FACTORY_LAYER0
    raw = json.loads(LAYOUT_FILE.read_text())["keys"]
    keys = []
    for k in raw:
        name = k["keyName"].replace("\n", " ")
        keys.append(Key(SHORT_NAMES.get(name, name), k["value"], k["x"], -k["y"], k["w"], k["h"]))
    by_value = {entry_value(e): s for s, e in enumerate(FACTORY_LAYER0) if any(e)}
    for k in keys:
        k.slot = by_value.get(k.value)
        k.usage = k.value if k.value < 0x100 else None
        if keymap_entries and k.slot is not None:
            e = keymap_entries[k.slot]
            k.usage = e[2] if e[0] == 0 and e[1] == 0 and e[3] == 0 and e[2] else None
    return keys


def bounds(keys):
    x0 = min(k.x for k in keys)
    y0 = min(k.y for k in keys)
    return x0, y0, max(k.x + k.w for k in keys) - x0, max(k.y + k.h for k in keys) - y0
