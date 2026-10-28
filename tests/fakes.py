"""A fake CX68 that reproduces the firmware's measured reply behaviour (docs/protocol.md 3).

Time-based like the real firmware: for `ready_after` seconds after a SET_FEATURE, GET_FEATURE
returns the request echoed (with byte 0 cleared in the second half of that window); the first
read after that is a torn copy (first half reply, second half echo); then the real reply.
Replies come from captured request->reply pairs where available.
"""
import errno
import json
import time
from pathlib import Path

from cx68 import transport
from cx68.transport import Device, Nodes

FIXTURES = Path(__file__).with_name("fixtures")


def captured_pairs():
    data = json.loads((FIXTURES / "vendor_session04.json").read_text())
    return {bytes.fromhex(p["request"]): bytes.fromhex(p["reply"]) for p in data["pairs"]}


class FakeFirmware:
    def __init__(self, replies=None, ready_after=0.0003, torn=True, respond=True):
        self.replies = dict(captured_pairs() if replies is None else replies)
        self.ready_after, self.torn, self.respond = ready_after, torn, respond
        self.received = []          # every report that reached the "device"
        self.pending, self.sent_at, self.torn_done = None, 0.0, False
        self.unplugged = False
        self.min_write_gap = 0.005     # writes closer together than this are dropped
        self.commit_busy = 0.05        # after a committing 0x65 page, echo-only for this long
        self.busy_until = 0.0
        self.last_write = 0.0
        self.dropped = []

    @staticmethod
    def _e5(param, page):
        from cx68 import policy
        return policy.encode(bytes([0xE5, param, 1, page]), policy.BIT7)

    @staticmethod
    def _enc(msg):
        from cx68 import policy
        return policy.encode(msg, policy.BIT7)

    def _set_entry(self, read_msg, slot, entry):
        key = self._enc(read_msg)
        page = bytearray(self.reply_for(key))
        page[(slot % 16) * 4:(slot % 16) * 4 + 4] = entry
        self.replies[key] = bytes(page)

    def reply_for(self, req: bytes) -> bytes:
        if req in self.replies:
            return self.replies[req]
        if req[0] == 0xE5 and req[1] == 254:   # live travel: all keys up
            return bytes(64)
        if req[0] in (0x8A, 0x90, 0x8C, 0xE5, 0x9C, 0x9E):
            return bytes(64)                  # data reply: empty
        return req                            # opcode-echo style reply with no state

    def set_feature(self, buf: bytes):
        req = bytes(buf[1:])
        self.received.append(req)
        now = time.perf_counter()
        if req[0] < 0x80:  # writes
            too_soon = now - self.last_write < self.min_write_gap or now < self.busy_until
            self.last_write = now
            if too_soon:
                self.dropped.append(req)
                return
        if req[0] == 0x65 and req[4] == 1:
            self.busy_until = now + self.commit_busy
        if req[0] == 0x65 and req[2] == 0:   # per-key value: later per-key E5 reads return it
            self.replies[self._enc(bytes([0xE5, req[1], 0, req[3]]))] = req[8:64].ljust(64, b"\0")
            self.pending, self.sent_at, self.torn_done = req, time.perf_counter(), False
            return
        if req[0] == 0x0A:    # single key: [0A, profile, slot, 0, 0, save, layer, ck, e0..e3]
            self._set_entry(bytes([0x8A, req[1], 0xFF, req[2] // 16, req[6]]), req[2], req[8:12])
        if req[0] == 0x10:    # Fn key: [10, os, fn layer, slot, 0, 0, 0, ck, e0..e3]
            self._set_entry(bytes([0x90, req[1], req[2], 0xFF, req[3] // 16]), req[3], req[8:12])
        if req[0] == 0x07:    # lighting: the 87 reply carries b[1..7]
            self.replies[self._enc(bytes([0x87]))] = bytes([0x87]) + req[1:8] + bytes(56)
        if req[0] == 0x04:
            self.replies[self._enc(bytes([0x84]))] = bytes([0x84, req[1]]) + bytes(62)
        if req[0] == 0x65:  # apply bulk magnetism writes so later E5 reads reflect them
            size = 128 if req[1] in (7, 252) else 256
            arr = bytearray(b"".join(self.reply_for(bytes(self._e5(req[1], pg))) for pg in range(size // 64)))
            arr[req[3] * 56:req[3] * 56 + 56] = req[8:64]
            for pg in range(size // 64):
                self.replies[self._e5(req[1], pg)] = bytes(arr[pg * 64:pg * 64 + 64])
        self.pending, self.sent_at, self.torn_done = req, time.perf_counter(), False

    def get_feature(self) -> bytes:
        req = self.pending or bytes(64)
        age = time.perf_counter() - self.sent_at
        if not self.respond or age < self.ready_after / 2 or time.perf_counter() < self.busy_until:
            return req
        if age < self.ready_after:
            return b"\0" + req[1:]
        real = self.reply_for(req)
        if self.torn and not self.torn_done:
            self.torn_done = True
            return real[:4] + req[4:]
        return real


class FakeDevice(Device):
    """transport.Device wired to a FakeFirmware instead of hidraw."""

    def __init__(self, firmware=None, **kw):
        super().__init__(nodes=Nodes("/dev/fake-hidraw5", "/dev/fake-hidraw4", 1, 3), **kw)
        self.fw = firmware or FakeFirmware()

    def open(self):
        self.fd = -1
        self._identify()
        return self

    def close(self):
        self.fd = None

    def reconnect(self, timeout=10.0):
        self.fw.unplugged = False
        return self.open()

    def _raw_ioctl(self, request, buf):
        # Only the raw ioctl is faked; transport.Device._ioctl's real error mapping runs.
        if self.fw.unplugged:
            raise OSError(errno.EPROTO, "Protocol error")  # what an unplugged CX68 returns
        if request == transport.HIDIOCSFEATURE:
            self.fw.set_feature(bytes(buf))
        elif request == transport.HIDIOCGFEATURE:
            buf[1:] = self.fw.get_feature()
        else:
            raise AssertionError(request)
