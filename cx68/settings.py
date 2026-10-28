"""Per-key Hall-effect settings (active profile): read, edit, write, verify.

Writes use only 0x65 (SET_MAGNETISM_BY_ARR), whose packets are verified byte for byte
against the vendor (tests/test_replay_writes.py). Unlike the vendor page, only the selected
keys change: other keys' RT values are left as they are.
"""
from dataclasses import dataclass

from . import protocol as P
from .transport import Device

# Model limits (vendor model data for the CX68: travel max 3.5, deadzone 0-1, RT 0.01-2 mm;
# the UI's travel minimum is 0.1 mm).
LIMITS_MM = {
    "actuation": (0.10, 3.50),
    "release": (0.10, 3.50),
    "rt_press": (0.01, 2.00),
    "rt_release": (0.01, 2.00),
    "deadzone": (0.00, 1.00),
}
PARAMS = {
    "actuation": P.Param.ACTUATION,
    "release": P.Param.RELEASE,
    "rt_press": P.Param.RT_PRESS,
    "rt_release": P.Param.RT_RELEASE,
    "deadzone": P.Param.DEADZONE,
}
RT_BIT = 0x80
WRITES = (0x65,)


class VerifyError(Exception):
    """The keyboard does not read back what was written."""


@dataclass
class KeySettings:
    actuation: float
    release: float
    rt_press: float
    rt_release: float
    deadzone: float
    rapid_trigger: bool


def _read_u16(dev, param):
    return P.u16_slots([dev.transact(*P.get_magnetism_page(param, pg)) for pg in range(P.U16_PAGES)])


def _read_u8(dev, param):
    return P.u8_slots([dev.transact(*P.get_magnetism_page(param, pg)) for pg in range(P.U8_PAGES)])


def read(dev: Device) -> dict[int, KeySettings]:
    """Settings of every matrix slot, in mm."""
    unit = P.travel_unit_mm(dev.firmware)
    arrays = {name: _read_u16(dev, prm) for name, prm in PARAMS.items()}
    modes = _read_u8(dev, P.Param.KEY_MODE)
    return {s: KeySettings(*(round(arrays[n][s] * unit, 3) for n in PARAMS), bool(modes[s] & RT_BIT))
            for s in range(P.SLOTS)}


def plan(current_raw: dict, slots, unit, **changes):
    """Pure planning step: new raw arrays for the params that change.

    current_raw: {"actuation": [128 ints], ..., "mode": [128 ints]}
    changes: any of actuation/release/rt_press/rt_release/deadzone (mm) and rapid_trigger (bool).
    Returns {Param: new array} containing only params whose array actually changes.
    """
    unknown = set(changes) - set(PARAMS) - {"rapid_trigger"}
    if unknown:
        raise ValueError(f"unknown settings {sorted(unknown)}")
    slots = sorted(set(slots))
    if not slots:
        raise ValueError("no keys selected")
    out = {}
    for name, value in changes.items():
        if value is None:
            continue
        if name == "rapid_trigger":
            arr = list(current_raw["mode"])
            for s in slots:
                arr[s] = (arr[s] | RT_BIT) if value else (arr[s] & ~RT_BIT)
            if arr != current_raw["mode"]:
                out[P.Param.KEY_MODE] = arr
            continue
        lo, hi = LIMITS_MM[name]
        if not lo - 1e-9 <= value <= hi + 1e-9:
            raise ValueError(f"{name} {value} mm outside {lo}-{hi} mm")
        raw = round(value / unit)
        arr = list(current_raw[name])
        for s in slots:
            arr[s] = raw
        if arr != current_raw[name]:
            out[PARAMS[name]] = arr
    return out


def encode_array(param, values) -> bytes:
    if param in P.U8_PARAMS:
        return bytes(values)
    return b"".join(v.to_bytes(2, "little") for v in values)


def apply(dev: Device, slots, **changes) -> list[str]:
    """Change settings of `slots` on the active profile, then verify by reading back.

    dev must permit writes of 0x65 (Device(allow_writes=settings.WRITES) or write mode).
    Returns a description of what was written; raises VerifyError if the read-back differs.
    """
    unit = P.travel_unit_mm(dev.firmware)
    needed = [n for n in changes if n in PARAMS and changes[n] is not None]
    current = {n: _read_u16(dev, PARAMS[n]) for n in needed}
    current["mode"] = _read_u8(dev, P.Param.KEY_MODE)
    old = {PARAMS[n]: current[n] for n in needed} | {P.Param.KEY_MODE: current["mode"]}
    new = plan(current, slots, unit, **changes)
    if not new:
        return []
    # Vendor order: mode byte first, then values; commit flag on the very last page.
    order = sorted(new, key=lambda prm: (prm != P.Param.KEY_MODE, int(prm)))
    cmds = []
    for i, prm in enumerate(order):
        cmds += P.set_magnetism_array(prm, encode_array(prm, new[prm]), last=i == len(order) - 1)
    for c in cmds:
        dev.transact(*c)
    dev.wait_ready()
    for prm in order:
        got = _read_u8(dev, prm) if prm in P.U8_PARAMS else _read_u16(dev, prm)
        if got != new[prm]:
            bad = [s for s in range(P.SLOTS) if got[s] != new[prm][s]]
            raise VerifyError(f"{prm.name}: slots {bad[:8]} read back differently after writing")
    return [f"{prm.name}: {sum(a != b for a, b in zip(old[prm], new[prm]))} keys" for prm in order]
