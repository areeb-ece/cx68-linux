#!/usr/bin/env python3
"""Cross-check a usbmon capture against a shim/probe JSONL log, byte for byte.

    python3 tools/capture/crosscheck.py captures/04-readonly-tour.pcapng captures/04-readonly-tour-shim.jsonl

Parses pcapng directly (linktype 220, usbmon mmapped header) so it does not depend on
tshark field names. Compares, in order: config-interface SET_REPORT payloads, GET_REPORT
replies, and interface-1 report-5 notifications. Blocked packets in the log never reached
the device, so they are expected to be absent from the capture.
"""
import json
import struct
import sys

CONFIG_IFACE = 2
SET_REPORT, GET_REPORT = 0x09, 0x01
FEATURE_REPORT_0 = 0x0300


def pcapng_packets(path):
    data = open(path, "rb").read()
    i, endian = 0, "<"
    while i + 8 <= len(data):
        btype, blen = struct.unpack_from(endian + "II", data, i)
        if btype == 0x0A0D0D0A:  # section header: learn byte order
            endian = "<" if data[i + 8:i + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            blen = struct.unpack_from(endian + "I", data, i + 4)[0]
        elif btype == 6:  # enhanced packet block
            caplen = struct.unpack_from(endian + "I", data, i + 20)[0]
            yield data[i + 28:i + 28 + caplen]
        i += blen


def usb_events(path):
    """Yield (kind, bytes) in capture order: ('set', payload), ('get', reply), ('in', report)."""
    pending = {}
    for pkt in pcapng_packets(path):
        if len(pkt) < 64:
            continue
        urb_id, typ, xfer, ep = struct.unpack_from("<QcBB", pkt, 0)
        setup_flag = pkt[14]
        length, len_cap = struct.unpack_from("<iI", pkt, 32)
        setup = pkt[40:48]
        payload = pkt[64:64 + len_cap]
        if xfer == 2:  # control
            if typ == b"S" and setup_flag == 0:
                bm, req, wval, widx = struct.unpack_from("<BBHH", setup)
                if widx == CONFIG_IFACE and wval == FEATURE_REPORT_0:
                    if bm == 0x21 and req == SET_REPORT:
                        yield "set", payload
                    elif bm == 0xA1 and req == GET_REPORT:
                        pending[urb_id] = True
            elif typ == b"C" and pending.pop(urb_id, None):
                yield "get", payload
        elif xfer == 1 and typ == b"C" and ep == 0x82 and payload[:1] == b"\x05":
            yield "in", payload


def log_events(path):
    for line in open(path):
        e = json.loads(line)
        if e["type"] == "setFeature" and not e.get("blocked"):
            yield "set", bytes.fromhex(e["data"])
        elif e["type"] == "getFeature":
            yield "get", bytes.fromhex(e["data"])
        elif e["type"] == "input":
            yield "in", bytes.fromhex(e["data"])


def main():
    usb = list(usb_events(sys.argv[1]))
    log = list(log_events(sys.argv[2]))
    print(f"usbmon: {len(usb)} events  log: {len(log)} events")
    # The capture may start before or end after the log; align on the first log event.
    start = next((i for i, ev in enumerate(usb) if ev == log[0]), None) if log else None
    if start is None:
        raise SystemExit("first log event not found in capture")
    mismatches = 0
    for n, (a, b) in enumerate(zip(usb[start:], log)):
        if a[0] != b[0] or a[1].rstrip(b"\0") != b[1].rstrip(b"\0"):
            mismatches += 1
            if mismatches <= 5:
                print(f"#{n}: usb {a[0]} {a[1][:16].hex(' ')} != log {b[0]} {b[1][:16].hex(' ')}")
    compared = min(len(usb) - start, len(log))
    print(f"compared {compared} events from capture index {start}: {mismatches} mismatches")
    if compared < len(log):
        print(f"{len(log) - compared} log events have no capture counterpart (capture ended first)")
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
