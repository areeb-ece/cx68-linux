"""python3 -m cx68bridge [run|check|calibrate|measure|config]: CX68 analog travel as a virtual gamepad."""
import argparse
import os
import signal
import subprocess
import sys

from . import mapping as M


def cmd_run(args):
    from .bridge import Bridge
    cfg = M.load(args.config)
    bridge = Bridge(cfg, log=lambda s: print(s, flush=True), always=args.always)
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop.set())
    signal.signal(signal.SIGINT, lambda *_: bridge.stop.set())
    bridge.run()


def cmd_check(args):
    from cx68.transport import Device, DeviceBusy, NotFound
    from .bridge import keyboard_nodes, read_layout
    ok = True
    try:
        cfg = M.load(args.config)
        print(f"config {args.config}: ok, style {cfg.style}, {len(cfg.axes)} axes, {len(cfg.buttons)} buttons")
    except M.ConfigError as err:
        print(f"config {args.config}: {err}")
        return 1
    print(f"/dev/uinput writable: {os.access('/dev/uinput', os.W_OK)}")
    ok &= os.access("/dev/uinput", os.W_OK)
    try:
        with Device() as dev:
            slots, _ = read_layout(dev)
        missing = sorted(cfg.keys() - set(slots))
        print("keys: " + ("all mapped keys found" if not missing else f"unknown names {missing}"))
        ok &= not missing
    except (NotFound, DeviceBusy) as err:
        print(f"keyboard: {err} (the bridge or cx68app may be running)")
    phantom = []
    for entry in sorted(os.listdir("/sys/class/input")):
        if not entry.startswith("event"):
            continue
        prop = subprocess.run(["udevadm", "info", "-q", "property", f"/dev/input/{entry}"],
                              capture_output=True, text=True).stdout
        if "ID_INPUT_JOYSTICK=1" in prop and "ID_VENDOR_ID=3151" in prop:
            phantom.append(entry)
    print("phantom joystick: " + (f"{', '.join(phantom)} (install udev/70-cx68.rules)" if phantom else "none"))
    nodes = keyboard_nodes()
    readable = [n for n, _ in nodes if os.access(n, os.R_OK)]
    print(f"keyboard input nodes readable (for [suppress] and native latency): {len(readable)}/{len(nodes)}")
    if cfg.suppress_mapped and len(readable) < len(nodes):
        print("  [suppress] mapped_keys = true needs the optional rule in udev/70-cx68.rules")
        ok = False
    print(f"pad identity: {cfg.device_id['name']} {cfg.device_id['vendor']:04x}:{cfg.device_id['product']:04x}")
    from . import access
    pol = access.load()
    if pol.mode == "always":
        print("access: always (any program can use the pad while the bridge runs)")
    else:
        running = access.matches(pol, access.running_programs())
        print(f"access: only while one of {pol.programs} runs; running now: {', '.join(running) or 'none'}")
    return 0 if ok else 1


def cmd_calibrate(args):
    from .bridge import calibrate
    calibrate()


def cmd_measure(args):
    from .bridge import measure
    measure(args.seconds)


def cmd_config(args):
    M.load(args.config)          # writes the default file if missing
    print(args.config)


def main():
    ap = argparse.ArgumentParser(prog="python3 -m cx68bridge", description=__doc__)
    ap.add_argument("--config", default=str(M.CONFIG_FILE))
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run the bridge (default)")
    r.add_argument("--always", action="store_true", help="ignore the program allowlist: pad always present")
    sub.add_parser("check", help="check permissions, config, phantom joystick")
    sub.add_parser("calibrate", help="measure rest and full-press travel of the mapped keys")
    m = sub.add_parser("measure", help="latency breakdown while you press the mapped keys")
    m.add_argument("--seconds", type=float, default=20)
    sub.add_parser("config", help="print the config path (creates the default config)")
    args = ap.parse_args()
    args.always = getattr(args, "always", False)
    fn = {"check": cmd_check, "calibrate": cmd_calibrate, "measure": cmd_measure, "config": cmd_config}.get(args.cmd, cmd_run)
    sys.exit(fn(args) or 0)


if __name__ == "__main__":
    main()
