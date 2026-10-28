"""Physical key layout of the CX68 and its mapping to matrix slots.

Geometry is the vendor's own (cx68/data/layout_cx68.json, extracted from the iotdriver
bundle). Each key is matched to its matrix slot through the keyboard's current keymap
(layer 0), so the HUD follows remaps instead of assuming a fixed matrix.
"""
import json
from dataclasses import dataclass
from pathlib import Path

LAYOUT_FILE = Path(__file__).resolve().parents[1] / "cx68" / "data" / "layout_cx68.json"

from cx68.keymap import HID_TO_LINUX, LINUX_TO_HID  # noqa: F401  (used by the HUD and the bridge)


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
