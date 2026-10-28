"""Macros: 50 slots shared by all profiles, each a 256-byte buffer on the keyboard.

From the vendor bundle ($Y.setMacro / $Y.buffToMacroEvents, Uga._setMacro / getMacro):
- Buffer: u16le repeat count, then events. A key or mouse-button event is [code, flags]:
  flags bit 7 = down, low 7 bits = the delay after it in ms when that is 1..127. Without an
  inline delay the event is followed by a u16le delay (0 if none), so it takes 4 bytes. A
  mouse move is [249, delay, dx, dy]. Four zero bytes end the macro.
- Read: 8B [8B, macro, page] returns 64 data bytes; pages are read until one contains four
  zero bytes in a row (at most 4 pages).
- Write: 0B [0B, macro, page, 56, last, 0, 0, ck] + 56 bytes, BIT7, as many pages as the
  buffer has non-empty 56-byte slices; `last` on the final page commits.
- A key runs a macro with the entry [9, mode, macro, 0]; mode 0 = play it `repeat` times,
  1 = toggle (play until pressed again), 2 = repeat while held.
The encoder below follows the vendor's byte for byte, quirks included, so the keyboard sees
exactly what the vendor page would send.
"""
import struct
from dataclasses import dataclass, field

from . import keymap
from . import protocol as P

WRITES = (0x0B,)
CAPACITY = 248                    # the page refuses macros whose encoding reaches this length
MODES = {"repeat": 0, "toggle": 1, "hold": 2}
MODE_LABELS = {"repeat": "Play N times", "toggle": "Toggle on/off", "hold": "Repeat while held"}
MOUSE = {240: "Mouse left", 241: "Mouse right", 242: "Mouse middle", 243: "Mouse back", 244: "Mouse forward"}
MOVE = 249


class VerifyError(Exception):
    """The keyboard does not read back what was written."""


@dataclass(frozen=True)
class Event:
    kind: str                 # "key", "mouse", "delay", "move", or "raw" (a code the page can't name)
    code: int = 0             # HID usage for keys, 240..244 for mouse buttons
    down: bool = True
    ms: int = 0               # delay
    dx: int = 0
    dy: int = 0

    def describe(self) -> str:
        if self.kind == "delay":
            return f"wait {self.ms} ms"
        if self.kind == "move":
            return f"mouse move {self.dx:+d}, {self.dy:+d}"
        arrow = "↓" if self.down else "↑"
        if self.kind == "key":
            return f"{arrow} {keymap.NAMES.get(self.code, f'0x{self.code:02X}')}"
        if self.kind == "mouse":
            return f"{arrow} {MOUSE.get(self.code, self.code)}"
        return f"{arrow} code 0x{self.code:02X}"


@dataclass
class Macro:
    repeat: int = 1
    events: list = field(default_factory=list)

    @property
    def empty(self):
        return not self.events

    def summary(self, limit=6) -> str:
        downs = [e for e in self.events if e.kind in ("key", "mouse", "raw") and e.down]
        names = [e.describe()[2:] for e in downs[:limit]]
        return " ".join(names) + (" …" if len(downs) > limit else "") if names else "(empty)"


def key(usage, down=True) -> Event:
    return Event("key", usage, down)


def delay(ms) -> Event:
    return Event("delay", ms=ms)


def tap(usage, hold_ms=20, gap_ms=20) -> list:
    return [key(usage), delay(hold_ms), key(usage, False), delay(gap_ms)]


# ---------------------------------------------------------------- buffer format

def encode(m: Macro) -> bytes:
    """The 256-byte buffer, as $Y.setMacro builds it."""
    out, used = _pack(m)
    if used >= CAPACITY:
        raise ValueError("macro too long for the keyboard (about 60 key events at most)")
    return out[:P.MACRO_BYTES]


def size(m: Macro) -> int:
    """Bytes the macro takes (it fits while this is below CAPACITY)."""
    return _pack(m)[1]


def _pack(m: Macro):
    if not 0 <= m.repeat <= 0xFFFF:
        raise ValueError("repeat count must be 0-65535")
    ev = list(m.events)
    out = bytearray(512)                  # room to measure an over-long macro before refusing it
    out[0:2] = struct.pack("<H", m.repeat)
    i, pending, t = 2, False, 0           # pending: last event still needs its u16 delay

    def flags(e):
        nonlocal i, pending, t
        d = 0
        if t + 1 < len(ev) and ev[t + 1].kind == "delay" and ev[t + 1].ms <= 127:
            d = ev[t + 1].ms
        if e.kind != "move":
            d = d | 0x80 if e.down else d & 0x7F
        out[i] = d
        i += 1
        pending = True
        if d & 0x7F:
            pending = False
            t += 1                        # the delay went inline; skip its event

    while t < len(ev):
        e = ev[t]
        if i > CAPACITY:
            break
        if e.kind != "delay" and pending:
            out[i:i + 2] = b"\0\0"
            i += 2
        if e.kind == "delay":
            ms = min(max(e.ms, 0), 0xFFFF)
            if not pending:
                out[i:i + 2] = b"\0\0"
                i += 2
            out[i:i + 2] = struct.pack("<H", ms)
            i += 2
            pending = False
        elif e.kind in ("key", "mouse", "raw"):
            out[i] = e.code
            i += 1
            flags(e)
        elif e.kind == "move":
            out[i] = MOVE
            i += 1
            flags(e)
            out[i:i + 2] = struct.pack("<bb", e.dx, e.dy)
            i += 2
        else:
            raise ValueError(f"unknown macro event {e.kind}")
        t += 1
    return bytes(out), i


def decode(buf: bytes) -> Macro:
    """Events of a buffer, as $Y.buffToMacroEvents reads them (zero delays dropped). Codes the
    page can't name are kept as "raw" events so editing a macro never loses them."""
    buf = bytes(buf).ljust(P.MACRO_BYTES + 8, b"\0")
    repeat = struct.unpack_from("<H", buf, 0)[0]
    events = []
    a = 2
    n = buf[a:a + 4]
    a += 4
    while a < P.MACRO_BYTES:
        if n[0] == MOVE:
            events.append(Event("move", dx=struct.unpack("b", n[2:3])[0], dy=struct.unpack("b", n[3:4])[0]))
            if n[1]:
                ms = n[1] >> 1
            else:
                ms = struct.unpack_from("<H", buf, a)[0]
                a += 2
            events.append(delay(ms))
        else:
            down = bool(n[1] & 0x80)
            if 4 <= n[0] <= 239:
                events.append(Event("key", n[0], down))
            elif n[0] in MOUSE:
                events.append(Event("mouse", n[0], down))
            elif n[0]:
                events.append(Event("raw", n[0], down))
            if n[1] & 0x7F:
                ms = n[1] & 0x7F
                a -= 2
            else:
                ms = struct.unpack_from("<H", n, 2)[0]
            events.append(delay(ms))
        n = buf[a:a + 4]
        if n == bytes(4):
            break
        a += 4
    return Macro(repeat, [e for e in events if not (e.kind == "delay" and e.ms == 0)])


def fits(m: Macro) -> bool:
    try:
        encode(m)
        return True
    except ValueError:
        return False


def has_end(page: bytes) -> bool:
    """The page's stop test (KB): four zero bytes in a row anywhere in the reply."""
    return any(page[i:i + 4] == bytes(4) for i in range(len(page) - 3))


# ---------------------------------------------------------------- key entries

def entry(index: int, mode="repeat") -> bytes:
    if not 0 <= index < P.MACROS:
        raise ValueError(f"macro slots are 1-{P.MACROS}")
    return bytes([9, MODES[mode], index, 0])


def parse_entry(e: bytes):
    """(macro index, mode) for a macro key entry, else None."""
    if len(e) == 4 and e[0] == 9 and e[1] in MODES.values():
        return e[2], next(m for m, v in MODES.items() if v == e[1])
    return None


def references(entries_by_layer: dict) -> dict:
    """{macro index: [(layer name, slot), ...]} from {layer name: 128 entries}."""
    out = {}
    for name, entries in entries_by_layer.items():
        for s, e in enumerate(entries):
            if (p := parse_entry(e)) is not None:
                out.setdefault(p[0], []).append((name, s))
    return out


def free_slot(used) -> int:
    """Lowest macro index no key uses (the page's rule when it creates a macro)."""
    for i in range(P.MACROS):
        if i not in used:
            return i
    raise ValueError(f"all {P.MACROS} macro slots are in use")


# ---------------------------------------------------------------- device

def read_raw(dev, index) -> bytes:
    data = b""
    for page in range(P.MACRO_READ_PAGES):
        reply = dev.transact(*P.get_macro_page(index, page))
        data += reply
        if has_end(reply):
            break
    return data.ljust(P.MACRO_BYTES, b"\0")[:P.MACRO_BYTES]


def read(dev, index) -> Macro:
    return decode(read_raw(dev, index))


def read_all(dev, progress=None) -> dict:
    """{index: Macro} for every slot that holds events."""
    out = {}
    for i in range(P.MACROS):
        m = read(dev, i)
        if not m.empty:
            out[i] = m
        if progress:
            progress(i + 1, P.MACROS)
    return out


def write(dev, index, m: Macro) -> int:
    """Write a macro into a slot and verify. dev must allow 0x0B. Returns packets sent."""
    if m.empty:
        raise ValueError("a macro needs at least one event")
    cmds = P.set_macro(index, encode(m))
    for c in cmds:
        dev.transact(*c)
    dev.wait_ready()
    got = read(dev, index)
    want = decode(encode(m))
    if (got.repeat, got.events) != (want.repeat, want.events):
        raise VerifyError(f"macro {index + 1} reads back differently after writing")
    return len(cmds)


# ---------------------------------------------------------------- recording


class Recorder:
    """Turns timed key down/up events into macro events (delays rounded to whole ms)."""

    def __init__(self, max_gap_ms=5000):
        self.events, self.last, self.max_gap = [], None, max_gap_ms
        self.held = set()

    def feed(self, usage, down, t_ms):
        if usage is None or (down and usage in self.held):     # auto-repeat
            return
        if self.last is not None:
            gap = min(int(round(t_ms - self.last)), self.max_gap)
            if gap > 0:
                self.events.append(delay(gap))
        self.last = t_ms
        (self.held.add if down else self.held.discard)(usage)
        self.events.append(key(usage, down))

    def finish(self) -> list:
        """Recorded events, releasing anything still held."""
        return self.events + [key(u, False) for u in sorted(self.held)]
