"""Labels for keymap entries (4 bytes per slot; plain keys are 00 00 <HID usage> 00)."""

_HID = {0x28: "Enter", 0x29: "Esc", 0x2A: "Bksp", 0x2B: "Tab", 0x2C: "Space", 0x2D: "-", 0x2E: "=",
        0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";", 0x34: "'", 0x35: "`", 0x36: ",", 0x37: ".",
        0x38: "/", 0x39: "Caps", 0x49: "Ins", 0x4A: "Home", 0x4B: "PgUp", 0x4C: "Del", 0x4D: "End",
        0x4E: "PgDn", 0x4F: "Right", 0x50: "Left", 0x51: "Down", 0x52: "Up",
        0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LWin", 0xE4: "RCtrl", 0xE5: "RShift",
        0xE6: "RAlt", 0xE7: "RWin"}
_HID.update({0x04 + i: chr(ord("A") + i) for i in range(26)})
_HID.update({0x1E + i: str((i + 1) % 10) for i in range(10)})
_HID.update({0x3A + i: f"F{i + 1}" for i in range(12)})


def hid_name(usage: int) -> str:
    return _HID.get(usage, f"0x{usage:02X}")


def name_for(entry: bytes) -> str:
    """Label a 4-byte keymap entry; non-plain entries (Fn, macros, ...) show their raw bytes."""
    if entry[0] == 0 and entry[1] == 0 and entry[3] == 0:
        return hid_name(entry[2])
    return entry.hex()
