#!/usr/bin/env python3
"""Read-only probe: gamepad state (0x9D) and whole-board live travel (0xE5 param 254).

    python3 tools/capture/probe_travel.py [--seconds 20] [--log captures/x.jsonl]

Prints every key whose travel is non-zero, as a bar in mm, and the achieved poll rate.
Uses only read commands, through the same policy as the shim. Do not run it while the
shim (or anything else) is talking to the keyboard: two processes interleaving
SET_FEATURE/GET_FEATURE pairs would mix up replies.
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import policy  # noqa: E402
from iot_driver_shim import CONFIG_IFACE, ROOT, ConfigChannel, find_interfaces  # noqa: E402

LIVE_TRAVEL = 254
PAGES = 4
UNIT_MM = 0.01  # firmware 0x0400; see commands.json 0xE5


def transact(cfg, msg, data_reply=False, timeout=0.02):
    """Send one command and return its stable reply.

    Until the firmware has processed a command, GET_FEATURE returns the request itself, and
    it then overwrites that echo front to back while we read (torn replies seen). Measured
    readiness: 0.13 ms median, 0.32 ms max.
    data_reply (0xE5 etc.): reply carries no opcode echo, so wait until byte 7 (the
      request checksum, last echo byte) has changed, then for two identical reads.
    otherwise (0x8F, 0x9D, 0x84, ...): the true reply can be byte-identical to the request
      (9D in comfort mode is), so wait a fixed 2 ms like the vendor app, then read stably.
    """
    req = policy.encode(bytes(msg), policy.BIT7)
    if (reason := cfg.send(req, policy.BIT7)):
        raise SystemExit(f"refused: {reason}")
    if not data_reply:
        time.sleep(0.002)
    deadline = time.perf_counter() + timeout
    prev = None
    while time.perf_counter() < deadline:
        reply = cfg.read()
        if (reply[7] != req[7] or not data_reply) and reply == prev:
            return reply
        prev = reply
    raise TimeoutError(f"no stable reply to {req[:8].hex(' ')}")


def live_travel(cfg):
    raw = b"".join(transact(cfg, [0xE5, LIVE_TRAVEL, 1, page], data_reply=True) for page in range(PAGES))
    return struct.unpack(f"<{len(raw) // 2}H", raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--log", default=str(ROOT / "captures" / time.strftime("%Y-%m-%dT%H-%M-%S-probe.jsonl")))
    args = ap.parse_args()

    ifaces = find_interfaces()
    if CONFIG_IFACE not in ifaces:
        raise SystemExit("keyboard not found")
    log_file = open(args.log, "a", buffering=1)
    quiet = {"setFeature", "getFeature"}
    verbose = [True]

    def log(e):
        e = {"t": time.time() * 1000, **e}
        log_file.write(json.dumps(e) + "\n")
        if verbose[0] or e["type"] not in quiet:
            print(json.dumps(e)[:160])

    cfg = ConfigChannel(ifaces[CONFIG_IFACE], log, "readonly")
    dev_id = cfg.identify()
    mode = transact(cfg, [0x9D])
    names = {0: "comfort", 1: "sensitive", 2: "gaming", 3: "custom"}
    print(f"\ndevice id {dev_id}; magnetism mode {names.get(mode[1], mode[1])}, "
          f"keyboardDisable={mode[2]}, gamepadEnable={mode[3]}\n")
    verbose[0] = False  # keep logging every packet, but only draw bars from here on

    print("press keys now (slowly, fully, and hold some)…")
    start = last_print = time.monotonic()
    frames = 0
    while time.monotonic() - start < args.seconds:
        t0 = time.monotonic()
        values = live_travel(cfg)
        frames += 1
        log({"type": "travel", "dt_ms": round((time.monotonic() - t0) * 1000, 3),
             "nonzero": {i: v for i, v in enumerate(values) if v}})
        if time.monotonic() - last_print > 0.1:
            last_print = time.monotonic()
            active = [(i, v) for i, v in enumerate(values) if v]
            bars = "  ".join(f"[{i:3}] {'█' * int(v * UNIT_MM * 4):<14}{v * UNIT_MM:4.2f}mm" for i, v in active[:4])
            rate = frames / (time.monotonic() - start)
            print(f"\r{rate:6.1f} frames/s  {bars or '(no keys pressed)':<100}", end="", flush=True)
    elapsed = time.monotonic() - start
    print(f"\n\n{frames} whole-board frames in {elapsed:.1f}s = {frames / elapsed:.1f} Hz "
          f"({PAGES} feature transactions per frame)")
    cfg.close()


if __name__ == "__main__":
    main()
