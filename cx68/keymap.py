"""Key remapping: layer entries and Fn-layer entries of the active profile.

A key entry is 4 bytes, the same in reads (8A / 90) and writes (0A / 10). For an ordinary
key (wY 'combo' in the bundle) it is [0, modifier, key, key2]: modifier is a modifier's own
HID usage (GY = {ctrl: 224, shift: 225, alt: 226, win: 227}), so one modifier per entry.
All zeros is 'forbidden' (key disabled) on this model. Fn (0A 01 00 00) is kept as-is.

Other actions, from the page's tables (CB, CY in the bundle):
- media / consumer keys [3, 0, usage lo, usage hi], e.g. volume up 03 00 E9 00
- system power, sleep and wake [2, 0x81..0x83, 0, 0]
- mouse buttons [1, 0, 240..244, 0]
- macros [9, mode, macro, 0] (see macros.py)
"""
import json
from pathlib import Path

from . import protocol as P

WRITES = (0x0A,)
_FACTORY = json.loads((Path(__file__).parent / "data" / "factory_keymap.json").read_text())
FACTORY_LAYER0 = [bytes.fromhex(e) for e in _FACTORY["layer0"]]
FACTORY_FN = {os_name: [bytes.fromhex(e) for e in v] for os_name, v in _FACTORY["fn"].items()}
FN_WRITES = (0x10,)
FN_ENTRY = bytes.fromhex("0a010000")
MODIFIERS = {"Ctrl": 224, "Shift": 225, "Alt": 226, "Win": 227}

# HID usage -> name, grouped for the picker
GROUPS = {
    "Letters": {0x04 + i: chr(ord("A") + i) for i in range(26)},
    "Numbers": {0x1E + i: str((i + 1) % 10) for i in range(10)},
    "Function": {0x3A + i: f"F{i + 1}" for i in range(12)} | {0x68 + i: f"F{i + 13}" for i in range(12)},
    "Editing": {0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space", 0x39: "Caps Lock",
                0x46: "Print Screen", 0x47: "Scroll Lock", 0x48: "Pause", 0x49: "Insert", 0x4C: "Delete",
                0x65: "Menu"},
    "Navigation": {0x4A: "Home", 0x4B: "Page Up", 0x4D: "End", 0x4E: "Page Down", 0x4F: "Right", 0x50: "Left",
                   0x51: "Down", 0x52: "Up"},
    "Symbols": {0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";", 0x34: "'", 0x35: "`",
                0x36: ",", 0x37: ".", 0x38: "/", 0x64: "ISO \\"},
    "Modifiers": {0xE0: "Left Ctrl", 0xE1: "Left Shift", 0xE2: "Left Alt", 0xE3: "Left Win",
                  0xE4: "Right Ctrl", 0xE5: "Right Shift", 0xE6: "Right Alt", 0xE7: "Right Win"},
    "Keypad": {0x53: "Num Lock", 0x54: "KP /", 0x55: "KP *", 0x56: "KP -", 0x57: "KP +", 0x58: "KP Enter",
               **{0x59 + i: f"KP {(i + 1) % 10}" for i in range(10)}, 0x63: "KP ."},
}
NAMES = {u: n for group in GROUPS.values() for u, n in group.items()}


# Linux input key code (Qt's nativeScanCode() - 8 on Wayland/X11) -> HID usage, for every key in NAMES
LINUX_TO_HID = {
    30: 0x04, 48: 0x05, 46: 0x06, 32: 0x07, 18: 0x08, 33: 0x09, 34: 0x0A, 35: 0x0B, 23: 0x0C, 36: 0x0D,
    37: 0x0E, 38: 0x0F, 50: 0x10, 49: 0x11, 24: 0x12, 25: 0x13, 16: 0x14, 19: 0x15, 31: 0x16, 20: 0x17,
    22: 0x18, 47: 0x19, 17: 0x1A, 45: 0x1B, 21: 0x1C, 44: 0x1D,
    **{2 + i: 0x1E + i for i in range(10)},
    28: 0x28, 1: 0x29, 14: 0x2A, 15: 0x2B, 57: 0x2C, 12: 0x2D, 13: 0x2E, 26: 0x2F, 27: 0x30, 43: 0x31,
    39: 0x33, 40: 0x34, 41: 0x35, 51: 0x36, 52: 0x37, 53: 0x38, 58: 0x39,
    **{59 + i: 0x3A + i for i in range(10)}, 87: 0x44, 88: 0x45,
    99: 0x46, 70: 0x47, 119: 0x48, 110: 0x49, 102: 0x4A, 104: 0x4B, 111: 0x4C, 107: 0x4D, 109: 0x4E,
    106: 0x4F, 105: 0x50, 108: 0x51, 103: 0x52,
    69: 0x53, 98: 0x54, 55: 0x55, 74: 0x56, 78: 0x57, 96: 0x58, 79: 0x59, 80: 0x5A, 81: 0x5B, 75: 0x5C,
    76: 0x5D, 77: 0x5E, 71: 0x5F, 72: 0x60, 73: 0x61, 82: 0x62, 83: 0x63, 86: 0x64, 127: 0x65,
    **{183 + i: 0x68 + i for i in range(12)},
    29: 0xE0, 42: 0xE1, 56: 0xE2, 125: 0xE3, 97: 0xE4, 54: 0xE5, 100: 0xE6, 126: 0xE7,
}
HID_TO_LINUX = {v: k for k, v in LINUX_TO_HID.items()}


def _consumer(usage):
    return bytes([3, 0, usage & 0xFF, usage >> 8])


# whole-entry actions, grouped for the picker (no modifier possible)
SPECIAL = {
    "Media": {_consumer(0xCD): "Play/Pause", _consumer(0xB5): "Next track", _consumer(0xB6): "Previous track",
              _consumer(0xB7): "Stop", _consumer(0xE2): "Mute", _consumer(0xE9): "Volume up",
              _consumer(0xEA): "Volume down", _consumer(0x183): "Media player"},
    "Apps & system": {_consumer(0x6F): "Screen brightness up", _consumer(0x70): "Screen brightness down",
                      _consumer(0x192): "Calculator", _consumer(0x18A): "Mail", _consumer(0x194): "File manager",
                      _consumer(0x221): "Search", _consumer(0x223): "Browser home", _consumer(0x224): "Browser back",
                      _consumer(0x227): "Browser refresh", bytes([2, 0x81, 0, 0]): "System power",
                      bytes([2, 0x82, 0, 0]): "System sleep", bytes([2, 0x83, 0, 0]): "System wake"},
    "Mouse": {bytes([1, 0, 240, 0]): "Mouse left", bytes([1, 0, 241, 0]): "Mouse right",
              bytes([1, 0, 242, 0]): "Mouse middle", bytes([1, 0, 243, 0]): "Mouse back",
              bytes([1, 0, 244, 0]): "Mouse forward"},
}
SPECIAL_NAMES = {e: n for group in SPECIAL.values() for e, n in group.items()}


class VerifyError(Exception):
    """The keyboard does not read back what was written."""


def entry(usage: int, modifier: str | None = None, key2: int = 0) -> bytes:
    if usage not in NAMES:
        raise ValueError(f"unsupported key usage 0x{usage:02x}")
    if key2 and key2 not in NAMES:
        raise ValueError(f"unsupported second key 0x{key2:02x}")
    return bytes([0, MODIFIERS[modifier] if modifier else 0, usage, key2])


DISABLED = bytes(4)


def describe(e: bytes) -> str:
    if e == DISABLED:
        return "Disabled"
    if e == FN_ENTRY:
        return "Fn"
    if e[0] == 0 and e[2] in NAMES and (e[1] == 0 or e[1] in MODIFIERS.values()) and (e[3] == 0 or e[3] in NAMES):
        mod = next((m for m, u in MODIFIERS.items() if u == e[1]), None)
        parts = ([mod] if mod else []) + [NAMES[e[2]]] + ([NAMES[e[3]]] if e[3] else [])
        return "+".join(parts)
    if bytes(e) in SPECIAL_NAMES:
        return SPECIAL_NAMES[bytes(e)]
    if e[0] == 9:
        return f"Macro {e[2] + 1}" + {1: " (toggle)", 2: " (held)"}.get(e[1], "")
    return f"custom {e.hex()}"


def editable(e: bytes) -> bool:
    """Entries the picker can show and re-create exactly (ordinary keys, media/system/mouse
    actions, disabled). Macro keys are set from the Macros page."""
    d = describe(e)
    return not (d == "Fn" or d.startswith(("custom", "Macro")))


def read_layer(dev, profile, layer=0) -> list[bytes]:
    return P.keymap_entries([dev.transact(*P.get_keymap_page(profile, layer, pg)) for pg in range(P.KEYMAP_PAGES)])


def read_fn(dev, os_name="win", fn_layer=0) -> list[bytes]:
    return P.keymap_entries([dev.transact(*P.get_fn_page(os_name, fn_layer, pg)) for pg in range(P.KEYMAP_PAGES)])


def _write(dev, cmds, read_back, want):
    for c in cmds:
        dev.transact(*c)
        dev.wait_ready()          # each single-key write saves (b[5] = 1); never send into a busy firmware
    got = read_back()
    bad = [s for s, e in want.items() if got[s] != e]
    if bad:
        raise VerifyError(f"slots {bad[:8]} read back differently after writing")


def write_keys(dev, profile, layer, changes: dict) -> int:
    """Set {slot: entry} on a layer of the active profile, then verify. dev must allow 0x0A."""
    active = dev.transact(*P.get_profile())[1]
    if profile != active:
        raise ValueError("only the active profile can be edited; switch profiles first")
    current = read_layer(dev, profile, layer)
    todo = {s: bytes(e) for s, e in changes.items() if current[s] != bytes(e)}
    if todo:
        _write(dev, [P.set_key(profile, layer, s, e) for s, e in sorted(todo.items())],
               lambda: read_layer(dev, profile, layer), todo)
    return len(todo)


def write_fn_keys(dev, changes: dict, os_name="win", fn_layer=0) -> int:
    """Set {slot: entry} on the Fn layer, then verify. dev must allow 0x10."""
    current = read_fn(dev, os_name, fn_layer)
    todo = {s: bytes(e) for s, e in changes.items() if current[s] != bytes(e)}
    if todo:
        _write(dev, [P.set_fn_key(os_name, fn_layer, s, e) for s, e in sorted(todo.items())],
               lambda: read_fn(dev, os_name, fn_layer), todo)
    return len(todo)
