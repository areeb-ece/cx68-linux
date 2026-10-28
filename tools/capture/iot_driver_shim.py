#!/usr/bin/env python3
"""Linux stand-in for the vendor's Windows "iot_driver" helper (driver.DriverGrpc).

iotdriver.qmk.top talks to the keyboard only through a native helper on
http://127.0.0.1:3814 speaking gRPC-web. This implements the subset the keyboard page needs
on top of hidraw, logs every message, and enforces policy.py (deny list, known opcodes
only, read-only by default). Firmware upgrade and raw feature access are refused.

    python3 tools/capture/iot_driver_shim.py [--mode readonly|write] [--log captures/x.jsonl]

Type a label + Enter to add a marker to the log.
"""
import argparse
import base64
import fcntl
import json
import os
import re
import select
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import policy  # noqa: E402

VID, PID = 0x3151, 0x502D
CONFIG_IFACE, VENDOR_IFACE = 2, 1
VENDOR_REPORT_ID = 5
ALLOWED_ORIGINS = {"https://iotdriver.qmk.top", "https://app.qmk.top"}
HELPER_VERSION = "2.2.2"  # matches the iot_v222 installer the page offers
ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- protobuf (minimal)

def _varint(n):
    out = bytearray()
    n &= (1 << 64) - 1
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb(*fields):
    """Encode (field_no, kind, value) tuples; kind is 'v' varint, 'b' bytes/str/message."""
    out = bytearray()
    for no, kind, val in fields:
        if kind == "v":
            if val:
                out += _varint(no << 3) + _varint(int(val))
        else:
            if isinstance(val, str):
                val = val.encode()
            out += _varint(no << 3 | 2) + _varint(len(val)) + val
    return bytes(out)


def pb_decode(buf):
    """Decode to {field_no: [values]} (varints as int, length-delimited as bytes)."""
    out, i = {}, 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        no, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _read_varint(buf, i)
        elif wt == 2:
            n, i = _read_varint(buf, i)
            val, i = buf[i:i + n], i + n
        elif wt == 5:
            val, i = buf[i:i + 4], i + 4
        elif wt == 1:
            val, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"wire type {wt}")
        out.setdefault(no, []).append(val)
    return out


def _read_varint(buf, i):
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def first(fields, no, default=None):
    return fields.get(no, [default])[0]


# ---------------------------------------------------------------- hidraw

def _ioc(dir_, nr, size):
    return dir_ << 30 | size << 16 | ord("H") << 8 | nr


HIDIOCSFEATURE = lambda n: _ioc(3, 0x06, n)  # noqa: E731
HIDIOCGFEATURE = lambda n: _ioc(3, 0x07, n)  # noqa: E731


def find_interfaces():
    """Map interface number -> /dev/hidrawN for the keyboard, via sysfs only."""
    want = f"0003:{VID:08X}:{PID:08X}"
    found = {}
    for hr in Path("/sys/class/hidraw").glob("hidraw*"):
        dev = (hr / "device").resolve()
        if f"HID_ID={want}" not in (dev / "uevent").read_text():
            continue
        m = re.search(r"/\d+-[\d.]+:\d+\.(\d+)/", str(dev))
        if m:
            found[int(m.group(1))] = f"/dev/{hr.name}"
    return found


class ConfigChannel:
    """Feature-report transport on the config interface. One transaction at a time."""

    def __init__(self, path, log, mode, allow=frozenset()):
        self.path, self.log, self.mode, self.allow = path, log, mode, allow
        self.fd = os.open(path, os.O_RDWR)
        self.lock = threading.Lock()
        self.last_op = 0

    def close(self):
        os.close(self.fd)

    def send(self, packet: bytes, checksum_type=None):
        reason = policy.check(packet, self.mode, checksum_type, self.allow)
        self.log({"type": "setFeature", "dev": self.path, "reportId": 0, "data": packet.hex(),
                  "ck": policy.CHECKSUM_NAMES.get(checksum_type, checksum_type),
                  **({"blocked": reason} if reason else {})})
        if reason:
            return reason
        with self.lock:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE(65), bytearray(b"\0" + packet))
            self.last_op = packet[0]
        return None

    def read(self) -> bytes:
        buf = bytearray(65)
        with self.lock:
            fcntl.ioctl(self.fd, HIDIOCGFEATURE(65), buf)
        data = bytes(buf[1:])
        # Replies don't always echo the opcode (e.g. GET_KEYMATRIX), so record the request.
        self.log({"type": "getFeature", "dev": self.path, "reportId": 0, "req": f"{self.last_op:02x}", "data": data.hex()})
        return data

    def identify(self):
        """GET_USB_VERSION (0x8F): device id u32 LE at [1..4]; same exchange the app does."""
        if self.send(policy.encode(b"\x8f", policy.BIT7), policy.BIT7):
            return None
        time.sleep(0.01)
        r = self.read()
        if r[0] != 0x8F:
            return None
        dev_id = struct.unpack_from("<I", r, 1)[0]
        return -1 if dev_id == 0xFFFFFFFF else dev_id


# ---------------------------------------------------------------- shim state

class Shim:
    def __init__(self, mode, log_path, allow=frozenset()):
        self.mode, self.allow = mode, allow
        self.log_file = open(log_path, "a", buffering=1)
        self.log_lock = threading.Lock()
        self.cfg = None          # ConfigChannel
        self.device = None       # dict for the Device message
        self.dev_waiters = []    # streams waiting for device list changes
        self.vendor_streams = []
        self.state_lock = threading.Lock()
        self.db_dir = Path(__file__).with_name("shim-db")

    # logging ----------------------------------------------------------------
    def log(self, e):
        e = {"t": time.time() * 1000, **e}
        with self.log_lock:
            self.log_file.write(json.dumps(e) + "\n")
            print(fmt(e), flush=True)

    # device management ------------------------------------------------------
    def poll_devices(self):
        """Hot-plug loop: (re)open the config channel and notify device-list watchers."""
        vendor_thread = None
        while True:
            ifaces = find_interfaces()
            present = CONFIG_IFACE in ifaces
            if present and self.cfg is None:
                try:
                    cfg = ConfigChannel(ifaces[CONFIG_IFACE], self.log, self.mode, self.allow)
                    dev_id = cfg.identify()
                except OSError as err:
                    self.log({"type": "error", "where": "open", "error": str(err)})
                    time.sleep(1)
                    continue
                if dev_id is None:
                    self.log({"type": "error", "where": "identify", "error": "no 0x8F reply"})
                    cfg.close()
                    time.sleep(1)
                    continue
                with self.state_lock:
                    self.cfg = cfg
                    self.device = {"path": cfg.path, "id": dev_id, "vid": VID, "pid": PID}
                self.log({"type": "device", "event": "add", **self.device})
                self.notify_devlist(1)
                if VENDOR_IFACE in ifaces and (vendor_thread is None or not vendor_thread.is_alive()):
                    vendor_thread = threading.Thread(target=self.read_vendor, args=(ifaces[VENDOR_IFACE],), daemon=True)
                    vendor_thread.start()
            elif not present and self.cfg is not None:
                with self.state_lock:
                    self.cfg.close()
                    self.cfg = None
                self.log({"type": "device", "event": "remove", **self.device})
                self.notify_devlist(2)
            time.sleep(1)

    def devlist_msg(self, change_type):
        devs = []
        if self.device:
            d = self.device
            dev = pb((1, "v", 0), (3, "b", d["path"]), (4, "v", d["id"]), (6, "v", 1),
                     (7, "v", d["vid"]), (8, "v", d["pid"]))
            devs.append(pb((1, "b", dev)))
        return pb(*[(1, "b", x) for x in devs], (2, "v", change_type))

    def notify_devlist(self, change_type):
        msg = self.devlist_msg(change_type)
        for q in list(self.dev_waiters):
            q.append(msg)

    def read_vendor(self, path):
        """Forward vendor notifications (interface 1, report 5). Typing reports on the same
        node (NKRO report 1 etc.) are read and discarded, never logged."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as err:
            self.log({"type": "error", "where": "vendor-open", "error": str(err)})
            return
        try:
            while self.cfg is not None:
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                try:
                    data = os.read(fd, 64)
                except OSError:
                    break
                if not data or data[0] != VENDOR_REPORT_ID:
                    continue
                self.log({"type": "input", "dev": path, "reportId": data[0], "data": data.hex()})
                msg = pb((1, "b", data))
                for q in list(self.vendor_streams):
                    q.append(msg)
        finally:
            os.close(fd)

    # db (the page's settings cache) -----------------------------------------
    def _db(self, dbpath):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", dbpath or "default")
        f = self.db_dir / f"{safe}.json"
        return f, (json.loads(f.read_text()) if f.exists() else {})

    def _db_save(self, f, data):
        self.db_dir.mkdir(exist_ok=True)
        f.write_text(json.dumps(data, indent=1))

    # rpc handlers: request bytes -> response bytes -------------------------
    def rpc(self, method, req):
        f = pb_decode(req)
        if method == "getVersion":
            return pb((1, "b", HELPER_VERSION), (2, "b", "linux-shim"))
        if method in ("changeWirelessLoopStatus", "setLightType"):
            return b""
        if method == "sendMsg":
            path = first(f, 1, b"").decode()
            msg = bytes(first(f, 2, b""))
            ck = first(f, 3, 0)
            cfg = self.cfg
            if cfg is None or path != cfg.path:
                return pb((1, "b", "device not found"))
            if ck not in policy.CHECKSUM_NAMES:
                return pb((1, "b", f"unknown checksum type {ck}"))
            try:
                packet = policy.encode(msg, ck)
            except ValueError as err:
                return pb((1, "b", str(err)))
            try:
                reason = cfg.send(packet, ck)
            except OSError as err:
                reason = str(err)
            return pb((1, "b", reason or ""))
        if method == "readMsg":
            path = first(f, 1, b"").decode()
            cfg = self.cfg
            if cfg is None or path != cfg.path:
                return pb((1, "b", "device not found"))
            try:
                return pb((1, "b", ""), (2, "b", cfg.read()))
            except OSError as err:
                return pb((1, "b", str(err)))
        if method in ("sendRawFeature", "readRawFeature", "cleanDev"):
            self.log({"type": "refused", "method": method, "request": req.hex()})
            return pb((1, "b", f"{method} not supported by shim"))
        if method == "insertDb":
            fpath, data = self._db(first(f, 1, b"").decode())
            data[base64.b64encode(first(f, 2, b"")).decode()] = base64.b64encode(first(f, 3, b"")).decode()
            self._db_save(fpath, data)
            return pb((1, "b", ""))
        if method == "deleteItemFromDb":
            fpath, data = self._db(first(f, 1, b"").decode())
            data.pop(base64.b64encode(first(f, 2, b"")).decode(), None)
            self._db_save(fpath, data)
            return pb((1, "b", ""))
        if method == "getItemFromDb":
            _, data = self._db(first(f, 1, b"").decode())
            v = data.get(base64.b64encode(first(f, 2, b"")).decode())
            return pb((1, "b", base64.b64decode(v))) if v is not None else pb((2, "b", "not found"))
        if method in ("getAllKeysFromDb", "getAllValuesFromDb"):
            _, data = self._db(first(f, 1, b"").decode())
            items = data.keys() if method == "getAllKeysFromDb" else data.values()
            return pb(*[(1, "b", base64.b64decode(x)) for x in items])
        if method == "getMicrophoneMute":
            return b""
        if method in ("muteMicrophone", "toggleMicrophoneMute"):
            return pb((1, "b", "not supported"))
        return None  # unimplemented (incl. upgradeOTAGATT, getWeather)


# ---------------------------------------------------------------- gRPC-web over HTTP

STREAMS = {"watchDevList", "watchVender", "watchSystemInfo"}


def frame(msg, trailer=False):
    return bytes([0x80 if trailer else 0]) + struct.pack(">I", len(msg)) + msg


def make_handler(shim):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _cors(self):
            origin = self.headers.get("Origin", "")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Expose-Headers", "grpc-status, grpc-message")
            self.send_header("Vary", "Origin")

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                return True
            shim.log({"type": "refused", "method": self.path, "origin": origin})
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")  # unread body must not be parsed as a request
            self.end_headers()
            self.close_connection = True
            return False

        def do_OPTIONS(self):
            if not self._origin_ok():
                return
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _chunk(self, data, text):
            if text:
                data = base64.b64encode(data)
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def do_POST(self):
            if not self._origin_ok():
                return
            method = self.path.rsplit("/", 1)[-1]
            ctype = self.headers.get("Content-Type", "")
            text = "grpc-web-text" in ctype
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if text:
                body = base64.b64decode(body)
            req = body[5:5 + struct.unpack(">I", body[1:5])[0]] if len(body) >= 5 else b""
            if method not in ("sendMsg", "readMsg"):
                shim.log({"type": "rpc", "method": method, "request": req.hex()})

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/grpc-web-text" if text else "application/grpc-web+proto")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                if method in STREAMS:
                    self._stream(method, text)
                else:
                    resp = shim.rpc(method, req)
                    if resp is None:
                        shim.log({"type": "refused", "method": method, "reason": "unimplemented"})
                        self._chunk(frame(b"grpc-status:12\r\ngrpc-message:unimplemented\r\n", True), text)
                    else:
                        self._chunk(frame(resp), text)
                        self._chunk(frame(b"grpc-status:0\r\ngrpc-message:\r\n", True), text)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _stream(self, method, text):
            queue = []
            registry = {"watchDevList": shim.dev_waiters, "watchVender": shim.vendor_streams}.get(method)
            if method == "watchDevList":
                queue.append(shim.devlist_msg(0))
            if registry is not None:
                registry.append(queue)
            try:
                while True:
                    while queue:
                        self._chunk(frame(queue.pop(0)), text)
                    # Idle streams (watchSystemInfo) never write, so detect the page going away.
                    r, _, _ = select.select([self.connection], [], [], 0.005)
                    if r and not self.connection.recv(1, socket.MSG_PEEK):
                        return
            finally:
                if registry is not None:
                    registry.remove(queue)

    return Handler


# ---------------------------------------------------------------- terminal output

def fmt(e):
    t = time.strftime("%H:%M:%S", time.localtime(e["t"] / 1000)) + f".{int(e['t']) % 1000:03d}"
    data = e.get("data", "")
    spaced = " ".join(data[i:i + 2] for i in range(0, min(len(data), 48), 2)) + (" …" if len(data) > 48 else "")
    kind = e["type"]
    if kind == "setFeature":
        op = int(data[:2], 16)
        head = "\x1b[31mBLOCK\x1b[0m" if e.get("blocked") else "\x1b[32mSET  \x1b[0m"
        line = f"{t} {head} [{policy.name(op)}] {spaced}"
        return line + (f"\n             \x1b[31m{e['blocked']}\x1b[0m" if e.get("blocked") else "")
    if kind == "getFeature":
        return f"{t} \x1b[36mGET  \x1b[0m [{policy.name(int(e.get('req') or data[:2], 16))}] {spaced}"
    if kind == "input":
        return f"{t} \x1b[33mIN   \x1b[0m id{e['reportId']} {spaced}"
    if kind == "mark":
        return f"\x1b[33m──── {e['label']} ────\x1b[0m"
    rest = {k: v for k, v in e.items() if k not in ("t", "type")}
    return f"{t} \x1b[2m{kind} {json.dumps(rest)}\x1b[0m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["readonly", "write"], default="readonly")
    ap.add_argument("--log", default=str(ROOT / "captures" / time.strftime("%Y-%m-%dT%H-%M-%S-shim.jsonl")))
    ap.add_argument("--port", type=int, default=3814)
    ap.add_argument("--allow", action="append", default=[], metavar="0xNN",
                    help="permit one volatile opcode in read-only mode (e.g. 0x1B travel report)")
    ap.add_argument("--allow-write", action="append", default=[], metavar="0xNN",
                    help="permit one config-write opcode in read-only mode (needs explicit approval)")
    args = ap.parse_args()
    try:
        allow = (policy.validate_allow(int(x, 16) for x in args.allow)
                 | policy.validate_allow_writes(int(x, 16) for x in args.allow_write))
    except ValueError as err:
        ap.error(str(err))
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)

    shim = Shim(args.mode, args.log, allow)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(shim))
    server.daemon_threads = True
    shim.log({"type": "session", "mode": args.mode, "allow": [f"0x{o:02X}" for o in sorted(allow)],
              "log": args.log, "listen": f"127.0.0.1:{args.port}"})
    threading.Thread(target=shim.poll_devices, daemon=True).start()

    def marks():
        for line in sys.stdin:
            if line.strip():
                shim.log({"type": "mark", "label": line.strip()})
    threading.Thread(target=marks, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
