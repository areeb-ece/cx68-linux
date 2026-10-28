"""Advanced key modes of the active profile: snap tap, toggle, mod-tap and DKS.

From Uga.setMultiMagnetismModes and its callers in the vendor bundle:
- The mode byte (param 7) holds the option in its low nibble (normal 0, dks 2, mt 3,
  tgl_hold 4, tgl_dots 5, snap 7) and rapid trigger in bit 7, which is kept as it is.
- A key's actions live in layers 0..3 of the profile at its own slot. Mod-tap uses layers 0 and
  1 (the page's first two action rows) and clears 2-3; toggle uses layer 0 and clears 1-3; snap
  tap uses no layers.
- Per-key values are written one key at a time ([65, param, 0, slot, last, 0, 0, ck] + data):
  the mod-tap hold time (1 byte, x10 ms, 1..100) and the snap partner (1 byte: the other key's slot).
- DKS (dynamic keystroke, the page's DKS editor and its row component): up to four actions in
  layers 0..3. Each action has a flag byte with a 2-bit code for each of four stages of the
  stroke: bits 0-1 pressing past the start depth, 2-3 bottoming out (3.5 mm), 4-5 lifting off
  the bottom, 6-7 rising back past the start depth. Codes: 0 nothing, 1 tap, 2 press here and
  release at the next stage, 3 press here and stay held through the next stage (that stage is
  then 2 or 3). The last stage can only tap. The four flag bytes are one per-key value (4 B);
  the start depth is another (u16le, 0.1 mm units, 0.5-2.5 mm, page default 0.7 mm).
- Order: layer entries, then the mode array, then the per-key values; the commit flag is on the
  very last packet, as in the page's parameter list [mode, ..., DKS start, DKS flags, MT time,
  ..., snap partner].
"""
from dataclasses import dataclass

from . import keymap
from . import protocol as P

WRITES = (0x0A, 0x65)
HOLD_UNIT_MS = 10
HOLD_RANGE = (1, 100)
DEFAULT_HOLD = 20
NO_PARTNER = 0xFF
EDITABLE = ("normal", "snap", "tgl_hold", "tgl_dots", "mt", "dks")
DKS_STAGES = ("Press past start", "Bottom out", "Lift off bottom", "Release past start")
DKS_CODES = {0: "—", 1: "Tap", 2: "Hold to next stage", 3: "Hold through next stage"}
DKS_START_RANGE = (5, 25)         # x0.1 mm
DKS_DEFAULT_START = 7
LABELS = {"normal": "Normal", "snap": "Snap tap", "tgl_hold": "Toggle (hold variant)",
          "tgl_dots": "Toggle (tap variant)", "mt": "Mod-tap", "dks": "DKS", "gamepadKey": "Gamepad key"}


class VerifyError(Exception):
    """The keyboard does not read back what was written."""


@dataclass(frozen=True)
class KeyMode:
    option: str = "normal"
    partner: int | None = None     # snap tap: the other key's slot
    tap: bytes | None = None       # mod-tap: layer 0 entry
    hold: bytes | None = None      # mod-tap: layer 1 entry
    hold_units: int | None = None  # mod-tap: hold time, x10 ms
    actions: tuple | None = None   # DKS: 4 entries (layers 0..3), None = leave that layer as it is
    flags: tuple | None = None     # DKS: 4 flag bytes, one per action
    start: int | None = None       # DKS: start depth, x0.1 mm


def dks_flag(codes) -> int:
    """Flag byte from 4 stage codes."""
    return codes[0] | codes[1] << 2 | codes[2] << 4 | codes[3] << 6


def dks_codes(flag: int) -> tuple:
    return tuple((flag >> (2 * i)) & 3 for i in range(4))


def check_dks(km: KeyMode):
    """Raise ValueError if a DKS setting can't be what the page's editor produces."""
    if km.actions is None or km.flags is None or len(km.actions) != 4 or len(km.flags) != 4:
        raise ValueError("DKS needs four actions and four flag bytes")
    start = km.start if km.start is not None else DKS_DEFAULT_START
    if not DKS_START_RANGE[0] <= start <= DKS_START_RANGE[1]:
        raise ValueError("DKS start depth must be 0.5-2.5 mm")
    used = 0
    for i, flag in enumerate(km.flags):
        codes = dks_codes(flag)
        if codes[3] > 1:
            raise ValueError(f"action {i + 1}: the last stage can only tap")
        for st in range(3):
            if codes[st] == 3 and codes[st + 1] not in (2, 3):
                raise ValueError(f"action {i + 1}: 'hold through' at {DKS_STAGES[st].lower()} needs a hold on the "
                                 f"next stage")
        if flag:
            used += 1
            e = km.actions[i]
            if e is None or e == keymap.DISABLED:
                raise ValueError(f"action {i + 1} has stages set but no key")
    if not used:
        raise ValueError("set at least one stage for one action")


def _per_key(dev, param, slot) -> bytes:
    return dev.transact(*P.get_magnetism_key(param, slot))


def read(dev, profile) -> tuple[dict, list[int], list[list[bytes]]]:
    """({slot: KeyMode} for keys that aren't normal, raw mode bytes, layers[0..3] entries)."""
    modes = P.u8_slots([dev.transact(*P.get_magnetism_page(P.Param.KEY_MODE, pg)) for pg in range(P.U8_PAGES)])
    layers = [keymap.read_layer(dev, profile, layer) for layer in range(P.LAYERS)]
    out = {}
    for s, b in enumerate(modes):
        opt = P.KEY_OPTIONS.get(b & 0x0F, f"0x{b & 0x0F:x}")
        if opt == "normal":
            continue
        if opt == "snap":
            p = _per_key(dev, P.Param.SNAP_PARTNER, s)[0]
            out[s] = KeyMode(opt, partner=None if p == NO_PARTNER else p)
        elif opt == "mt":
            out[s] = KeyMode(opt, tap=layers[0][s], hold=layers[1][s], hold_units=_per_key(dev, P.Param.MT_HOLD_TIME, s)[0])
        elif opt == "dks":
            flags = _per_key(dev, P.Param.DKS_FLAGS, s)[:4]
            start = int.from_bytes(_per_key(dev, P.Param.DKS_START, s)[:2], "little")
            out[s] = KeyMode(opt, actions=tuple(layers[layer][s] for layer in range(P.LAYERS)),
                             flags=tuple(flags), start=start)
        else:
            out[s] = KeyMode(opt)
    return out, modes, layers


def plan(changes: dict, modes: list[int], layers: list[list[bytes]], profile) -> list[P.Cmd]:
    """Commands that apply {slot: KeyMode}. Pure; `modes`/`layers` are the current state."""
    for s, km in changes.items():
        if km.option not in EDITABLE:
            raise ValueError(f"{km.option} can't be set from here")
        if km.option == "snap" and (km.partner is None or km.partner == s):
            raise ValueError("snap tap needs a partner key")
        if km.option == "mt":
            if km.tap is None or km.hold is None:
                raise ValueError("mod-tap needs a tap and a hold action")
            units = km.hold_units if km.hold_units is not None else DEFAULT_HOLD
            if not HOLD_RANGE[0] <= units <= HOLD_RANGE[1]:
                raise ValueError(f"hold time must be {HOLD_RANGE[0] * 10}-{HOLD_RANGE[1] * 10} ms")
        if km.option == "dks":
            check_dks(km)
    key_cmds = []
    for s, km in sorted(changes.items()):
        if km.option == "mt":
            want = {0: km.tap, 1: km.hold, 2: keymap.DISABLED, 3: keymap.DISABLED}
        elif km.option in ("tgl_hold", "tgl_dots"):
            want = {1: keymap.DISABLED, 2: keymap.DISABLED, 3: keymap.DISABLED}
        elif km.option == "dks":
            want = {layer: e if e is not None else keymap.DISABLED
                    for layer, e in enumerate(km.actions) if e is not None or layer > 0}
        elif P.KEY_OPTIONS.get(modes[s] & 0x0F) in ("mt", "tgl_hold", "tgl_dots", "dks"):
            want = {1: keymap.DISABLED, 2: keymap.DISABLED, 3: keymap.DISABLED}   # back to a plain key
        else:
            want = {}
        key_cmds += [P.set_key(profile, layer, s, e) for layer, e in want.items() if layers[layer][s] != e]

    new_modes = list(modes)
    for s, km in changes.items():
        new_modes[s] = (modes[s] & P.RT_BIT) | P.OPTION_CODES[km.option]
    per_key = []
    dks = [(s, km) for s, km in sorted(changes.items()) if km.option == "dks"]
    for s, km in dks:
        start = km.start if km.start is not None else DKS_DEFAULT_START
        per_key.append((P.Param.DKS_START, s, start.to_bytes(2, "little")))
    for s, km in dks:
        per_key.append((P.Param.DKS_FLAGS, s, bytes(km.flags)))
    for s, km in sorted(changes.items()):
        if km.option == "mt":
            per_key.append((P.Param.MT_HOLD_TIME, s, bytes([km.hold_units or DEFAULT_HOLD])))
    for s, km in sorted(changes.items()):
        if km.option == "snap":
            per_key.append((P.Param.SNAP_PARTNER, s, bytes([km.partner])))
    cmds = list(key_cmds)
    if new_modes != modes:
        cmds += P.set_magnetism_array(P.Param.KEY_MODE, bytes(new_modes), last=not per_key)
    cmds += [P.set_magnetism_key(prm, s, data, last=i == len(per_key) - 1) for i, (prm, s, data) in enumerate(per_key)]
    return cmds


def apply(dev, changes: dict) -> int:
    """Apply {slot: KeyMode} on the active profile and verify. dev must allow 0x0A and 0x65."""
    profile = dev.transact(*P.get_profile())[1]
    _, modes, layers = read(dev, profile)
    cmds = plan(changes, modes, layers, profile)
    for c in cmds:
        dev.transact(*c)
        if c.msg[0] == 0x0A:
            dev.wait_ready()
    dev.wait_ready()
    got, _, _ = read(dev, profile)
    for s, km in changes.items():
        have = got.get(s, KeyMode())
        if have.option != km.option:
            raise VerifyError(f"slot {s}: mode reads back as {have.option}, wrote {km.option}")
        if km.option == "snap" and have.partner != km.partner:
            raise VerifyError(f"slot {s}: snap partner reads back as {have.partner}")
        if km.option == "mt" and (have.tap, have.hold, have.hold_units) != (km.tap, km.hold, km.hold_units or DEFAULT_HOLD):
            raise VerifyError(f"slot {s}: mod-tap reads back differently")
        if km.option == "dks":
            start = km.start if km.start is not None else DKS_DEFAULT_START
            acts = all(a is None or a == b for a, b in zip(km.actions, have.actions))
            if (have.flags, have.start) != (tuple(km.flags), start) or not acts:
                raise VerifyError(f"slot {s}: DKS reads back differently")
    return len(cmds)
