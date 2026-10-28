"""Packet builders and reply parsers for the CX68 (RY5088 class chain wGa..AC).

Pure functions: no I/O. Each builder returns a Cmd (message bytes before padding/checksum,
checksum type, reply kind for Device.transact). Layouts are from the vendor bundle and are
cross-checked against captures by tests/test_replay.py. See docs/protocol.md.
"""
import struct
from dataclasses import dataclass
from enum import IntEnum

from .policy import BIT7, BIT8

SLOTS = 128            # matrix slots (68 keys used), column-major, 6 rows per column
KEYMAP_PAGES = 8       # 8 pages x 16 entries x 4 bytes
U16_PAGES = 4          # E5 u16 params: 4 pages x 32 slots
U8_PAGES = 2           # E5 u8 params: 2 pages x 64 slots
PROFILES = 4
LAYERS = 4             # per profile (model "layer: 16" = 4 profiles x 4 layers)
FN_OS = {"win": 0, "mac": 1}
USERPIC_PAGES = 6
MACROS = 50            # macro slots, shared by all profiles (the page allows 50 on this model)
MACRO_BYTES = 256      # one macro buffer: u16 repeat count + events
MACRO_READ_PAGES = 4   # 8B replies are 64 data bytes


class Param(IntEnum):
    """GET/SET_MAGNETISM_BY_ARR parameter (enum Bf in the bundle)."""
    ACTUATION = 0          # 触发行程
    RELEASE = 1            # 抬起行程 (semantics open: see docs/protocol.md section 11)
    RT_PRESS = 2           # RT触发行程
    RT_RELEASE = 3         # RT抬起行程
    DKS_START = 4          # 动态键程的起始行程 (per key)
    MT_HOLD_TIME = 5       # MT的长按时间 (per key)
    DEADZONE = 6           # 死区 (bottom)
    KEY_MODE = 7           # 按键模式 (u8)
    DKS_FLAGS = 8          # 动态键程可选标志 (per key)
    SNAP_PARTNER = 9       # Snap对号 (per key)
    ALL_DKS_FLAGS = 10     # 所有按键动态键程可选标志
    SWITCH_TYPE = 252      # 轴体类型 (u8)
    LIVE_TRAVEL = 254      # 按键按压行程数值
    LIVE_DYNAMIC = 255     # 按键按压动态数值


U16_PARAMS = (Param.ACTUATION, Param.RELEASE, Param.RT_PRESS, Param.RT_RELEASE, Param.DEADZONE,
              Param.LIVE_TRAVEL, Param.LIVE_DYNAMIC)
U8_PARAMS = (Param.KEY_MODE, Param.SWITCH_TYPE)

KEY_MODES = {0: "comfort", 1: "sensitive", 2: "gaming", 3: "custom"}


@dataclass(frozen=True)
class Cmd:
    msg: bytes
    checksum: int = BIT7
    reply: str | None = "echo"   # "echo", "data", or None (write-only)

    def __iter__(self):  # allows dev.transact(*cmd)
        return iter((self.msg, self.checksum, self.reply))


def _cmd(*msg, reply="echo"):
    return Cmd(bytes(msg), BIT7, reply)


# ---------------------------------------------------------------- reads

def get_rev():                       return _cmd(0x8F)
def get_battery():                   return _cmd(0x82)
def get_report_rate(profile):        return _cmd(0x83, profile)
def get_profile():                   return _cmd(0x84)
def get_led_onoff():                 return _cmd(0x85)
def get_debounce():                  return _cmd(0x86)
def get_led():                       return _cmd(0x87)
def get_side_led():                  return _cmd(0x88)
def get_kboption(profile):           return _cmd(0x89, profile)
def get_sleep():                     return _cmd(0x91)
def get_auto_os():                   return _cmd(0x97)
def get_key_mode():                  return _cmd(0x9D)


def get_keymap_page(profile, layer, page):
    return _cmd(0x8A, profile, 0xFF, page, layer, reply="data")


def get_fn_page(os_name, layer, page):
    return _cmd(0x90, FN_OS[os_name], layer, 0xFF, page, reply="data")


def get_magnetism_page(param, page):
    return _cmd(0xE5, int(param), 1, page, reply="data")


def get_magnetism_key(param, slot):
    """Per-key read (E5 with bulk flag 0) for the per-key-only params: DKS flags/start, MT time,
    snap partner. Reply data starts at byte 8 like the page reads."""
    return _cmd(0xE5, int(param), 0, slot, reply="data")


def get_macro_page(index, page):
    """8B [8B, macro, page]: 64 data bytes of the macro's 256-byte buffer ($Y/Uga.getMacro)."""
    assert 0 <= index < MACROS and 0 <= page < MACRO_READ_PAGES
    return _cmd(0x8B, index, page, reply="data")


def get_userpic_page(pic, page):
    return _cmd(0x8C, pic, 0xFF, page, reply="data")


def get_calibration_page(maximum: bool, page):
    return _cmd(0x9E if maximum else 0x9C, page, reply="data")


# ---------------------------------------------------------------- parsers

def parse_rev(r):
    """-> (device_id, firmware)"""
    return struct.unpack_from("<I", r, 1)[0], r[8] << 8 | r[7]


def parse_key_mode(r):
    return {"mode": KEY_MODES.get(r[1], r[1]), "keyboard_disable": bool(r[2]), "gamepad_enable": bool(r[3])}


def parse_sleep(r):
    bt, rf, deep_bt, deep_rf = struct.unpack_from("<4H", r, 8)
    return {"bt": bt, "rf": rf, "deep_bt": deep_bt, "deep_rf": deep_rf}


def u16_slots(pages: list[bytes]) -> list[int]:
    raw = b"".join(pages)
    return list(struct.unpack(f"<{len(raw) // 2}H", raw))[:SLOTS]


def u8_slots(pages: list[bytes]) -> list[int]:
    return list(b"".join(pages))[:SLOTS]


def keymap_entries(pages: list[bytes]) -> list[bytes]:
    """4-byte entry per slot; byte 2 is the HID usage for plain keys (00 00 <usage> 00)."""
    raw = b"".join(pages)
    return [raw[i:i + 4] for i in range(0, min(len(raw), SLOTS * 4), 4)]


def travel_unit_mm(firmware: int) -> float:
    """mm per raw travel unit (getMagnetPrecision in the bundle)."""
    if firmware >= 0x0500:
        return 0.005
    if firmware >= 0x0300:
        return 0.01
    return 0.1


KEY_OPTIONS = {0: "normal", 2: "dks", 3: "mt", 4: "tgl_hold", 5: "tgl_dots", 6: "gamepadKey", 7: "snap"}
OPTION_CODES = {v: k for k, v in KEY_OPTIONS.items()}
RT_BIT = 0x80


def key_mode_byte(b: int) -> dict:
    """Per-key mode byte (param 7): low nibble option, bit 7 rapid trigger. Option codes from
    this class's Uga.setMultiMagnetismModes (normal 0, dks 2, mt 3, tgl_hold 4, tgl_dots 5,
    gamepadKey 6, snap 7)."""
    return {"rapid_trigger": bool(b & RT_BIT), "full_bottom": bool(b & 0x40), "option": KEY_OPTIONS.get(b & 0x0F, b & 0x0F)}


# ---------------------------------------------------------------- writes

WRITE_CHUNK = 56   # data bytes per 0x65 packet (8-byte header + 56 = 64)


def set_magnetism_array(param, raw: bytes, last: bool):
    """0x65 bulk write of one parameter for all slots: [65, param, 1, page, last, 0, 0, ck] + 56 B.

    raw is the whole array as read by E5 (256 bytes for u16 params, 128 for u8 ones).
    last=True marks the final page of the final parameter in a batch, which commits the
    batch (the vendor sets it once per Apply). Verified byte for byte against captures.
    """
    pages = -(-len(raw) // WRITE_CHUNK)
    return [Cmd(bytes([0x65, int(param), 1, page, int(last and page == pages - 1), 0, 0, 0])
                + raw[page * WRITE_CHUNK:(page + 1) * WRITE_CHUNK], BIT7, None)
            for page in range(pages)]


def set_magnetism_key(param, slot, data: bytes, last: bool):
    """0x65 per-key write [65, param, 0, slot, last, 0, 0, ck] + data (Uga.setMultiMagnetismModes):
    MT time 1 byte (x10 ms), snap partner 1 byte (slot, FF none), DKS flags 4 bytes, DKS start u16le."""
    return Cmd(bytes([0x65, int(param), 0, slot, int(last), 0, 0, 0]) + bytes(data), BIT7, None)


def set_key(profile, layer, slot, entry: bytes):
    """0x0A single key [0A, profile, slot, 0, 0, save=1, layer, ck, e0..e3] (Uga.setKeyConfigSimple;
    on magnetism boards the page's currentProfile is 4 x profile and byte 6 the layer offset)."""
    assert len(entry) == 4 and 0 <= layer < LAYERS and 0 <= profile < PROFILES
    return Cmd(bytes([0x0A, profile, slot, 0, 0, 1, layer, 0]) + bytes(entry), BIT7, None)


def set_fn_key(os_name, fn_layer, slot, entry: bytes):
    """0x10 [10, os, fn layer, slot, 0, 0, 0, ck, e0..e3] (Uga.setFnKeyConfigSimple)."""
    assert len(entry) == 4
    return Cmd(bytes([0x10, FN_OS[os_name], fn_layer, slot, 0, 0, 0, 0]) + bytes(entry), BIT7, None)


def set_macro(index, buf: bytes):
    """0B pages [0B, macro, page, 56, last, 0, 0, ck] + 56 B (Uga._setMacro).

    Like the vendor, only as many pages as the buffer has non-empty 56-byte slices are sent
    (so an all-zero buffer sends nothing); `last` on the final page commits.
    """
    assert 0 <= index < MACROS and len(buf) <= MACRO_BYTES
    buf = bytes(buf).ljust(MACRO_BYTES, b"\0")
    used = sum(1 for i in range(5) if any(buf[i * WRITE_CHUNK:(i + 1) * WRITE_CHUNK]))
    return [Cmd(bytes([0x0B, index, page, WRITE_CHUNK, int(page == used - 1), 0, 0, 0])
                + buf[page * WRITE_CHUNK:(page + 1) * WRITE_CHUNK].ljust(WRITE_CHUNK, b"\0"), BIT7, None)
            for page in range(used)]


def set_led(mode, speed_raw, brightness, option, r, g, b):
    """0x07 [07, mode, speed (stored inverted), brightness, option, r, g, b, ck] with the BIT8
    checksum (Uga.setLightSetting). White must be sent as FA FF FA (done by lighting.encode)."""
    return Cmd(bytes([0x07, mode, speed_raw, brightness, option, r, g, b]), BIT8, None)


def set_profile(profile):
    """0x04 [04, profile] (Uga.setCurrentProfile: page value / 4 on magnetism boards)."""
    assert 0 <= profile < PROFILES
    return Cmd(bytes([0x04, profile]), BIT7, None)


def set_key_mode(mode: str | int, keyboard_disable=False, gamepad_enable=False):
    """0x1D [mode, keyboardDisable, gamepadEnable]. No reply."""
    m = mode if isinstance(mode, int) else {v: k for k, v in KEY_MODES.items()}[mode]
    return Cmd(bytes([0x1D, m, int(keyboard_disable), int(gamepad_enable)]), BIT7, None)


# ---------------------------------------------------------------- volatile

def set_travel_report(on: bool):
    """Start/stop 0x1B travel notifications (interface 1, report 5). No reply."""
    return Cmd(bytes([0x1B, int(on)]), BIT7, None)


def parse_travel_notification(report: bytes):
    """05 1B lo hi slot -> (slot, raw travel) or None for other vendor reports."""
    if len(report) >= 5 and report[0] == 0x05 and report[1] == 0x1B:
        return report[4], report[2] | report[3] << 8
    return None
