"""Command line: python -m cx68 <command>

  info                     identify the keyboard, show mode/gamepad state and who else has it open
  backup [FILE]            read the full configuration (reads only) and save it
  show FILE                summarise a backup
  diff FILE                read the keyboard now (reads only) and list differences from a backup
  restore FILE [--yes]     write back per-key HE settings + trigger mode from a backup, then verify
  watch [--seconds N]      live per-key travel by polling (read-only)
  stream [--seconds N]     live travel via the 0x1B notification stream (toggles it on/off)
"""
import argparse
import sys
import time
from pathlib import Path

from . import config, live
from . import protocol as P
from .transport import Device

ROOT = Path(__file__).resolve().parents[1]


def _names(dev):
    """slot -> key label from the active profile's layer 0 (device's own keymap)."""
    from .keynames import name_for
    profile = dev.transact(*P.get_profile())[1]
    pages = [dev.transact(*P.get_keymap_page(profile, 0, pg)) for pg in range(P.KEYMAP_PAGES)]
    return {s: name_for(e) for s, e in enumerate(P.keymap_entries(pages)) if any(e)}


def cmd_info(args):
    with Device() as dev:
        mode = P.parse_key_mode(dev.transact(*P.get_key_mode()))
        print(f"config node   {dev.nodes.config} (vendor {dev.nodes.vendor}), bus {dev.nodes.busnum} dev {dev.nodes.devnum}")
        print(f"device id     {dev.device_id}   firmware 0x{dev.firmware:04x}   "
              f"travel unit {P.travel_unit_mm(dev.firmware)} mm")
        print(f"active profile {dev.transact(*P.get_profile())[1]}   key mode {mode['mode']}   "
              f"keyboard disabled {mode['keyboard_disable']}   gamepad {mode['gamepad_enable']}")
        print(f"other processes with the config node open: {dev.others or 'none'}")


def cmd_backup(args):
    path = Path(args.file or ROOT / "backups" / time.strftime("cx68-%Y%m%d-%H%M%S.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with Device() as dev:
        cfg = config.read_full_config(dev, progress=lambda i, n: print(f"\rreading {i}/{n}", end="", flush=True))
    digest = config.save(cfg, path)
    config.load(path)  # verify it reads back
    print(f"\nsaved {path}  ({len(cfg['raw'])} replies, sha256 {digest[:16]}…)")
    _summary(cfg)


def _summary(cfg):
    d = cfg["decoded"]
    keys = d["per_key_active_profile"]
    first = next(iter(keys.values()))
    same = all({k: v for k, v in x.items()} == first for x in keys.values())
    print(f"device {cfg['device']}  active profile {d['active_profile']}  key mode {d['key_mode']}")
    print(f"debounce {d['debounce']}  sleep {d['sleep']}  led {d['led']} on={d['led_on']}")
    print(f"{len(keys)} keys; per-key settings {'identical on all keys: ' + str(first) if same else 'vary per key'}")
    layers = {k: len(v) for k, v in d["keymaps"].items() if v}
    print(f"non-empty keymap layers: {layers}")


def cmd_show(args):
    _summary(config.load(args.file))


def cmd_restore(args):
    backup = config.load(args.file)
    with Device(allow_writes=config.RESTORE_WRITES) as dev:
        cmds = config.plan_restore(backup, config.read_full_config(dev))
        if not cmds:
            print("nothing to restore: per-key settings and mode already match the backup")
        else:
            print("restore plan:\n  " + "\n  ".join(config.describe_restore(cmds)))
            if not args.yes and input("send these writes? [y/N] ").strip().lower() != "y":
                print("aborted, nothing written")
                return
            config.execute(dev, cmds)
            time.sleep(0.5)      # committing writes keeps the firmware busy for a while
            dev.wait_ready()
        after = config.read_full_config(dev)
    changes = config.diff(backup, after)
    print("verified: keyboard matches the backup exactly" if not changes else
          "still different (not covered by restore):\n  " + "\n  ".join(changes))
    sys.exit(1 if changes else 0)


def cmd_diff(args):
    old = config.load(args.file)
    with Device() as dev:
        new = config.read_full_config(dev)
    changes = config.diff(old, new)
    print("\n".join(changes) if changes else f"keyboard matches {args.file} exactly")
    sys.exit(1 if changes else 0)


def _bars(values, names, unit, width=12):
    active = [(s, v) for s, v in enumerate(values) if v]
    return "  ".join(f"{names.get(s, s)!s:>5} {'█' * int(v * unit / 3.5 * width):<{width}}{v * unit:4.2f}"
                     for s, v in active[:6]) or "(no keys pressed)"


def cmd_watch(args):
    with Device() as dev:
        names, unit = _names(dev), P.travel_unit_mm(dev.firmware)
        start, frames, last = time.monotonic(), 0, 0.0
        for frame in live.poll(dev, on_status=lambda s: print(f"\n[{s}]")):
            frames += 1
            if frame.t - last > 0.05:
                last = frame.t
                rate = frames / (frame.t - start)
                print(f"\r{rate:6.0f} fps  {_bars(frame.travel, names, unit):<110}", end="", flush=True)
            if args.seconds and frame.t - start > args.seconds:
                break
        print()


def cmd_stream(args):
    with Device(allow=[0x1B]) as dev:
        names, unit = _names(dev), P.travel_unit_mm(dev.firmware)
        depth = {}
        end = time.monotonic() + args.seconds
        with live.TravelStream(dev) as stream:
            print("travel stream ON (Ctrl+C or timeout turns it off)")
            while time.monotonic() < end:
                for _t, slot, value in stream.events(0.05):
                    depth[slot] = value
                print(f"\r{_bars([depth.get(s, 0) for s in range(P.SLOTS)], names, unit):<110}", end="", flush=True)
        print("\ntravel stream OFF")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m cx68", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    b = sub.add_parser("backup")
    b.add_argument("file", nargs="?")
    s = sub.add_parser("show")
    s.add_argument("file")
    d = sub.add_parser("diff")
    d.add_argument("file")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    w = sub.add_parser("watch")
    w.add_argument("--seconds", type=float, default=0)
    st = sub.add_parser("stream")
    st.add_argument("--seconds", type=float, default=20)
    args = ap.parse_args(argv)
    try:
        {"info": cmd_info, "backup": cmd_backup, "show": cmd_show, "diff": cmd_diff, "restore": cmd_restore, "watch": cmd_watch, "stream": cmd_stream}[args.cmd](args)
    except KeyboardInterrupt:
        print()
    except Exception as err:  # noqa: BLE001 - CLI boundary
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
