"""Key remapping: layer entries and Fn-layer entries of the active profile.

A key entry is 4 bytes, the same in reads (8A / 90) and writes (0A / 10). For an ordinary
key (wY 'combo' in the bundle) it is [0, modifier, key, key2]: modifier is a modifier's own
HID usage (GY = {ctrl: 224, shift: 225, alt: 226, win: 227}), so one modifier per entry.
All zeros is 'forbidden' (key disabled) on this model. Other entries (Fn 0A 01 00 00, macros,
media keys) are kept as-is but not offered for editing.
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
    if e[0] == 9:
        return f"Macro {e[2] + 1}"
    return f"custom {e.hex()}"


def editable(e: bytes) -> bool:
    """Entries this module can show and re-create exactly (ordinary keys, disabled)."""
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
