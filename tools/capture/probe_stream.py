#!/usr/bin/env python3
"""Travel-notification probe: enable the firmware's 0x1B travel report, print what arrives.

    python3 tools/capture/probe_stream.py [--seconds 20] [--log captures/x.jsonl]

Sends exactly two commands besides the 0x8F identify read: 1B 01 (start) and 1B 00 (stop,
always sent on exit, including Ctrl+C). Notifications arrive on interface 1 as input report
5 with byte 1 = 0x1B. Other reports on that node (typing) are discarded and never logged.
Don't run it while the shim or another probe is talking to the keyboard.
"""
import argparse
import json
import os
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import policy  # noqa: E402
from iot_driver_shim import CONFIG_IFACE, ROOT, VENDOR_IFACE, VENDOR_REPORT_ID, ConfigChannel, find_interfaces  # noqa: E402

TRAVEL_REPORT = 0x1B


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--log", default=str(ROOT / "captures" / time.strftime("%Y-%m-%dT%H-%M-%S-stream.jsonl")))
    args = ap.parse_args()

    ifaces = find_interfaces()
    if CONFIG_IFACE not in ifaces or VENDOR_IFACE not in ifaces:
        raise SystemExit(f"keyboard interfaces not found: {ifaces}")
    log_file = open(args.log, "a", buffering=1)

    def log(e, show=True):
        e = {"t": time.time() * 1000, **e}
        log_file.write(json.dumps(e) + "\n")
        if show:
            print(json.dumps(e)[:150])

    cfg = ConfigChannel(ifaces[CONFIG_IFACE], log, "readonly", policy.validate_allow([TRAVEL_REPORT]))
    vendor = os.open(ifaces[VENDOR_IFACE], os.O_RDONLY | os.O_NONBLOCK)
    print(f"device id {cfg.identify()}")

    def report(on):
        reason = cfg.send(policy.encode(bytes([TRAVEL_REPORT, int(on)]), policy.BIT7), policy.BIT7)
        if reason:
            raise SystemExit(f"refused: {reason}")

    count, first, distinct = 0, None, {}
    try:
        report(True)
        print("\ntravel report ON - press keys now (slowly, fully, hold some)…")
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            r, _, _ = select.select([vendor], [], [], 0.2)
            if not r:
                continue
            data = os.read(vendor, 64)
            if not data or data[0] != VENDOR_REPORT_ID:
                continue  # typing etc. on the same node: never logged
            now = time.monotonic()
            first = first or now
            count += 1
            log({"type": "input", "reportId": data[0], "data": data.hex()}, show=False)
            distinct.setdefault(data[1:6].hex(" "), data.hex(" "))
            if data[1] == TRAVEL_REPORT:
                u16 = data[2] | data[3] << 8
                print(f"\r{count:6} reports  raw {data[1:8].hex(' ')}  u8={data[2]:3}  u16={u16:5} "
                      f"(={u16 / 100:4.2f}mm if 0.01mm)  {'█' * min(40, data[2] // 4):<40}", end="", flush=True)
    finally:
        report(False)
        print("\ntravel report OFF")
        os.close(vendor)
        cfg.close()

    span = (time.monotonic() - first) if first else 0
    print(f"\n{count} notifications" + (f" over {span:.1f}s = {count / span:.0f}/s while active" if span else ""))
    print("first examples of distinct headers:")
    for head, full in list(distinct.items())[:10]:
        print("  ", full[:60])


if __name__ == "__main__":
    main()
