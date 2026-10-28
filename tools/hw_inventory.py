#!/usr/bin/env python3
"""Read-only hardware inventory for the iCAN CX68 (or any HID device).

Maps USB interfaces -> hidraw -> input/event nodes, saves each raw report
descriptor and prints a short-item decode. Reads sysfs only; it never opens
or writes to the device itself, so it is safe to re-run after a firmware update.

    python3 tools/hw_inventory.py [--vid 3151 --pid 502d] [--out docs/descriptors]
"""
import argparse
import os
import re
from pathlib import Path

MAIN = {0x8: "Input", 0x9: "Output", 0xB: "Feature", 0xA: "Collection", 0xC: "End Collection"}
GLOBAL = {0x0: "Usage Page", 0x1: "Logical Minimum", 0x2: "Logical Maximum", 0x3: "Physical Minimum",
          0x4: "Physical Maximum", 0x5: "Unit Exponent", 0x6: "Unit", 0x7: "Report Size",
          0x8: "Report ID", 0x9: "Report Count", 0xA: "Push", 0xB: "Pop"}
LOCAL = {0x0: "Usage", 0x1: "Usage Minimum", 0x2: "Usage Maximum"}
SIGNED = {"Logical Minimum", "Logical Maximum", "Physical Minimum", "Physical Maximum"}
COLLECTION = {0: "Physical", 1: "Application", 2: "Logical"}


def decode(desc: bytes):
    """Yield (indent, hex, text) for each short item."""
    i, depth = 0, 0
    while i < len(desc):
        b = desc[i]
        size = (0, 1, 2, 4)[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        raw = desc[i + 1:i + 1 + size]
        val = int.from_bytes(raw, "little") if raw else 0
        name = {0: MAIN, 1: GLOBAL, 2: LOCAL}.get(typ, {}).get(tag, f"?{typ}:{tag:x}")
        if name in SIGNED and size:
            val = int.from_bytes(raw, "little", signed=True)
        if name == "End Collection":
            depth -= 1
        if name == "Collection":
            text = f"Collection ({COLLECTION.get(val, val)})"
        elif name in ("Input", "Output", "Feature"):
            flags = ["Cnst" if val & 1 else "Data", "Var" if val & 2 else "Arr", "Rel" if val & 4 else "Abs"]
            text = f"{name} ({','.join(flags)})"
        elif name == "End Collection":
            text = name
        else:
            text = f"{name} ({val:#x})" if name.startswith("Usage") else f"{name} ({val})"
        yield depth, desc[i:i + 1 + size].hex(" "), text
        if name == "Collection":
            depth += 1
        i += 1 + size


def uevent(path: Path) -> dict:
    out = {}
    for line in (path / "uevent").read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vid", default="3151")
    ap.add_argument("--pid", default="502d")
    ap.add_argument("--out", help="directory to save raw descriptors (.bin) and decodes (.txt)")
    args = ap.parse_args()
    want = f"0003:{int(args.vid, 16):08X}:{int(args.pid, 16):08X}"

    found = False
    for hr in sorted(Path("/sys/class/hidraw").iterdir(), key=lambda p: int(p.name[6:])):
        dev = (hr / "device").resolve()
        ue = uevent(dev)
        if ue.get("HID_ID") != want:
            continue
        found = True
        # .../usb1/1-5/1-5:1.2/0003:... -> config 1, interface 2
        iface = re.search(r"/\d+-[\d.]+:\d+\.(\d+)/", str(dev)).group(1)
        desc = (dev / "report_descriptor").read_bytes()
        print(f"== interface {iface}: /dev/{hr.name}  ({ue.get('HID_PHYS')}, descriptor {len(desc)} bytes)")
        inputs = sorted((dev / "input").glob("input*")) if (dev / "input").exists() else []
        for inp in inputs:
            nodes = [n.name for n in inp.iterdir() if re.match(r"(event|js|mouse)\d+", n.name)]
            abs_caps = (inp / "capabilities/abs").read_text().strip()
            print(f"   {inp.name}: {', '.join('/dev/input/' + n for n in nodes)}  "
                  f"name={(inp / 'name').read_text().strip()!r} abs={abs_caps}")
        lines = [f"{'  ' * d}{t:<40} # {h}" for d, h, t in decode(desc)]
        print("\n".join("   " + l for l in lines))
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"if{iface}.bin").write_bytes(desc)
            (out / f"if{iface}.txt").write_text("\n".join(lines) + "\n")
    if not found:
        raise SystemExit(f"no hidraw device with HID_ID {want}")
    js = sorted(Path("/dev/input").glob("js*"))
    print(f"\n/dev/input/js*: {', '.join(map(str, js)) or 'none'}")


if __name__ == "__main__":
    main()
