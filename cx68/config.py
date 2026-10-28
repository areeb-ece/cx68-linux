"""Full configuration read and backup files.

A backup stores every raw reply (hex) keyed by request, plus a decoded view for humans.
The raw replies are what a restore uses; the decoded view is never written back.
"""
import hashlib
import json
import time
from pathlib import Path

from . import protocol as P
from .policy import encode

SCHEMA = 1


def _key(cmd: P.Cmd) -> str:
    """Stable key for a request: its message bytes, e.g. 'e5 00 01 03'."""
    return cmd.msg.hex(" ")


def plan_full_read(profile_count=P.PROFILES, layers=P.LAYERS):
    """Every read making up a backup, in order. All are source-confirmed GET commands."""
    cmds = [P.get_rev(), P.get_profile(), P.get_key_mode(), P.get_debounce(), P.get_sleep(),
            P.get_led(), P.get_side_led(), P.get_led_onoff(), P.get_auto_os(), P.get_battery()]
    cmds += [P.get_kboption(p) for p in range(profile_count)]
    cmds += [P.get_report_rate(p) for p in range(profile_count)]
    for p in range(profile_count):
        for layer in range(layers):
            cmds += [P.get_keymap_page(p, layer, pg) for pg in range(P.KEYMAP_PAGES)]
    for os_name in P.FN_OS:
        cmds += [P.get_fn_page(os_name, 0, pg) for pg in range(P.KEYMAP_PAGES)]
    # Per-key HE settings exist only for the active profile (E5 has no profile byte).
    for param in (P.Param.ACTUATION, P.Param.RELEASE, P.Param.RT_PRESS, P.Param.RT_RELEASE, P.Param.DEADZONE):
        cmds += [P.get_magnetism_page(param, pg) for pg in range(P.U16_PAGES)]
    for param in (P.Param.KEY_MODE, P.Param.SWITCH_TYPE):
        cmds += [P.get_magnetism_page(param, pg) for pg in range(P.U8_PAGES)]
    cmds += [P.get_userpic_page(0, pg) for pg in range(P.USERPIC_PAGES)]
    return cmds


def read_full_config(dev, progress=None) -> dict:
    """Read everything in plan_full_read() from an open transport.Device."""
    plan = plan_full_read()
    dev.wait_ready()  # never read state while the firmware is still committing a write
    raw = {}
    for i, cmd in enumerate(plan):
        raw[_key(cmd)] = dev.transact(*cmd).hex()
        if progress:
            progress(i + 1, len(plan))
    # Per-key-only values (snap partner, mod-tap time) for the keys that use those modes.
    for prm, slot in per_key_reads(raw):
        raw[_key(P.get_magnetism_key(prm, slot))] = dev.transact(*P.get_magnetism_key(prm, slot)).hex()
    cfg = {
        "schema": SCHEMA,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": {"id": dev.device_id, "firmware": f"0x{dev.firmware:04x}", "vid_pid": "3151:502d"},
        "raw": raw,
    }
    cfg["decoded"] = decode(cfg)
    return cfg


PER_KEY = {"snap": P.Param.SNAP_PARTNER, "mt": P.Param.MT_HOLD_TIME}


def per_key_reads(raw: dict) -> list:
    """(param, slot) of the per-key values a backup needs, from the mode array in `raw`."""
    modes = P.u8_slots([bytes.fromhex(raw[_key(P.get_magnetism_page(P.Param.KEY_MODE, pg))]) for pg in range(P.U8_PAGES)])
    return [(PER_KEY[opt], s) for s, b in enumerate(modes) if (opt := P.KEY_OPTIONS.get(b & 0x0F)) in PER_KEY]


def _get(cfg, cmd) -> bytes:
    return bytes.fromhex(cfg["raw"][_key(cmd)])


def decode(cfg) -> dict:
    """Human-readable view of a backup. Informational only; restore uses cfg['raw']."""
    fw = int(cfg["device"]["firmware"], 16)
    unit = P.travel_unit_mm(fw)
    active = _get(cfg, P.get_profile())[1]

    def u16(param):
        return P.u16_slots([_get(cfg, P.get_magnetism_page(param, pg)) for pg in range(P.U16_PAGES)])

    def u8(param):
        return P.u8_slots([_get(cfg, P.get_magnetism_page(param, pg)) for pg in range(P.U8_PAGES)])

    keymaps = {}
    for p in range(P.PROFILES):
        for layer in range(P.LAYERS):
            entries = P.keymap_entries([_get(cfg, P.get_keymap_page(p, layer, pg)) for pg in range(P.KEYMAP_PAGES)])
            keymaps[f"profile{p}/layer{layer}"] = {s: e.hex(" ") for s, e in enumerate(entries) if any(e)}
    used = sorted(keymaps[f"profile{active}/layer0"])
    mags = {name: u16(param) for name, param in (("actuation", P.Param.ACTUATION), ("release", P.Param.RELEASE),
                                               ("rt_press", P.Param.RT_PRESS), ("rt_release", P.Param.RT_RELEASE),
                                               ("deadzone", P.Param.DEADZONE))}
    modes = u8(P.Param.KEY_MODE)
    return {
        "active_profile": active,
        "key_mode": P.parse_key_mode(_get(cfg, P.get_key_mode())),
        "debounce": _get(cfg, P.get_debounce())[1],
        "sleep": P.parse_sleep(_get(cfg, P.get_sleep())),
        "led": _get(cfg, P.get_led())[1:8].hex(" "),
        "led_on": bool(_get(cfg, P.get_led_onoff())[1]),
        "travel_unit_mm": unit,
        "per_key_active_profile": {
            str(s): {**{k: round(v[s] * unit, 3) for k, v in mags.items()}, "mode": P.key_mode_byte(modes[s])}
            for s in used
        },
        "keymaps": keymaps,
    }


def save(cfg: dict, path) -> str:
    """Write a backup; returns its sha256 (also stored inside)."""
    # Normalise through JSON first (int dict keys become strings) so the digest matches
    # what load() will see.
    body = json.loads(json.dumps({k: v for k, v in cfg.items() if k != "sha256"}))
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    Path(path).write_text(json.dumps({**body, "sha256": digest}, indent=1, sort_keys=True))
    return digest


def load(path) -> dict:
    cfg = json.loads(Path(path).read_text())
    body = {k: v for k, v in cfg.items() if k != "sha256"}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != cfg.get("sha256"):
        raise ValueError(f"{path}: checksum mismatch, backup is corrupt or was edited")
    if cfg.get("schema") != SCHEMA:
        raise ValueError(f"{path}: unsupported schema {cfg.get('schema')}")
    return cfg


def request_packets() -> list[bytes]:
    """The exact 64-byte reports a full read sends (used by the replay tests)."""
    return [encode(c.msg, c.checksum) for c in plan_full_read()]


def diff(old: dict, new: dict) -> list[str]:
    """Human-readable differences between two backups (raw replies), ignoring battery level."""
    out = []
    for key in sorted(set(old["raw"]) | set(new["raw"])):
        if key.startswith("82"):
            continue
        a, b = old["raw"].get(key), new["raw"].get(key)
        if a == b:
            continue
        if a is None or b is None:
            out.append(f"{key}: only in {'new' if a is None else 'old'}")
            continue
        ab, bb = bytes.fromhex(a), bytes.fromhex(b)
        idx = [i for i in range(len(ab)) if ab[i] != bb[i]]
        out.append(f"{key}: bytes {idx[:12]}{'…' if len(idx) > 12 else ''}  "
                   f"old {ab[idx[0]:idx[0] + 8].hex(' ')}  new {bb[idx[0]:idx[0] + 8].hex(' ')}")
    return out


RESTORABLE_U16 = (P.Param.ACTUATION, P.Param.RELEASE, P.Param.RT_PRESS, P.Param.RT_RELEASE, P.Param.DEADZONE)
# the only write opcodes a restore may use: 65/1D verified by replay tests, 0A/10/07 from the vendor source
RESTORE_WRITES = (0x0A, 0x10, 0x07, 0x65, 0x1D)


def _param_raw(cfg, param) -> bytes:
    pages = P.U8_PAGES if param in P.U8_PARAMS else P.U16_PAGES
    return b"".join(_get(cfg, P.get_magnetism_page(param, pg)) for pg in range(pages))[:256 if pages == 4 else 128]


def plan_restore(backup: dict, current: dict) -> list[P.Cmd]:
    """Write commands that bring `current` back to `backup` for what restore covers, all for the
    active profile: keymap layers 0-3 (0A), Fn layers (10), lighting (07), per-key HE settings,
    modes and per-key values (65, commit on the last packet), and the trigger/gamepad mode (1D).
    Raises if the active profile differs (E5 data and edits are per active profile)."""
    profile = _get(backup, P.get_profile())[1]
    if profile != _get(current, P.get_profile())[1]:
        raise ValueError("active profile differs from the backup's; switch profiles first")
    cmds = []
    for layer in range(P.LAYERS):
        want, have = (P.keymap_entries([_get(c, P.get_keymap_page(profile, layer, pg)) for pg in range(P.KEYMAP_PAGES)])
                      for c in (backup, current))
        cmds += [P.set_key(profile, layer, s, want[s]) for s in range(len(want)) if want[s] != have[s]]
    for os_name in P.FN_OS:
        want, have = (P.keymap_entries([_get(c, P.get_fn_page(os_name, 0, pg)) for pg in range(P.KEYMAP_PAGES)])
                      for c in (backup, current))
        cmds += [P.set_fn_key(os_name, 0, s, want[s]) for s in range(len(want)) if want[s] != have[s]]
    want, have = _get(backup, P.get_led()), _get(current, P.get_led())
    if want[1:8] != have[1:8]:
        cmds.append(P.set_led(*want[1:8]))
    changed = [prm for prm in (*RESTORABLE_U16, P.Param.KEY_MODE) if _param_raw(backup, prm) != _param_raw(current, prm)]
    per_key = []
    for key, value in sorted(backup["raw"].items()):
        msg = bytes.fromhex(key.replace(" ", ""))
        if msg[0] == 0xE5 and len(msg) == 4 and msg[2] == 0 and current["raw"].get(key) != value:
            n = 1 if msg[1] in (P.Param.SNAP_PARTNER, P.Param.MT_HOLD_TIME) else None
            if n:
                per_key.append(P.set_magnetism_key(msg[1], msg[3], bytes.fromhex(value)[:n], last=False))
    for i, prm in enumerate(changed):
        cmds += P.set_magnetism_array(prm, _param_raw(backup, prm), last=not per_key and i == len(changed) - 1)
    if per_key:
        per_key[-1] = P.set_magnetism_key(per_key[-1].msg[1], per_key[-1].msg[3], per_key[-1].msg[8:], last=True)
    cmds += per_key
    want, have = _get(backup, P.get_key_mode()), _get(current, P.get_key_mode())
    if want[1:4] != have[1:4]:
        cmds.append(P.set_key_mode(want[1], bool(want[2]), bool(want[3])))
    return cmds


def describe_restore(cmds) -> list[str]:
    from . import keymap, lighting
    out = []
    for c in cmds:
        op = c.msg[0]
        if op == 0x0A:
            out.append(f"0A key slot {c.msg[2]} layer {c.msg[6]} → {keymap.describe(c.msg[8:12])}")
        elif op == 0x10:
            out.append(f"10 Fn key slot {c.msg[3]} ({'win' if c.msg[1] == 0 else 'mac'}) → {keymap.describe(c.msg[8:12])}")
        elif op == 0x07:
            out.append(f"07 lighting → {lighting.parse(c.msg).describe()}")
        elif op == 0x65 and c.msg[2] == 0:
            out.append(f"65 {P.Param(c.msg[1]).name} slot {c.msg[3]} = {c.msg[8]}{' (commit)' if c.msg[4] else ''}")
        elif op == 0x65:
            out.append(f"65 param {P.Param(c.msg[1]).name} page {c.msg[3]}{' (commit)' if c.msg[4] else ''}")
        else:
            out.append(f"1D mode {P.KEY_MODES.get(c.msg[1], c.msg[1])} keyboard_disable={c.msg[2]} gamepad={c.msg[3]}")
    return out
