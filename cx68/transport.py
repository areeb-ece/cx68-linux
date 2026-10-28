"""hidraw transport for the CX68 config channel.

One Device owns the keyboard's config interface. Every packet goes through policy.check,
and every transaction handles the firmware's reply quirks (docs/protocol.md section 3):
until a command is processed, GET_FEATURE returns the request itself, and the firmware
then overwrites that echo front to back while we may be reading it.
"""
import errno
import fcntl
import os
import re
import select
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from . import policy

VID, PID = 0x3151, 0x502D
DEVICE_ID = 2427  # CamelTech CT-M68 / ry5088_cx68bug_8k_002_dm
CONFIG_IFACE, VENDOR_IFACE = 2, 1
VENDOR_REPORT_ID = 5
REPORT_LEN = policy.REPORT_LEN

# Reply readiness: measured 0.13 ms median, 0.32 ms max (firmware 0x0400).
SETTLE_DATA = 0.0      # data replies: no fixed wait, readiness is detected from the bytes
SETTLE_ECHO = 0.002    # opcode-echo replies: the true reply may equal the request
REPLY_TIMEOUT = 0.05
# Write pacing: the vendor sleeps 10 ms before every write (writeFeatureCmd default). Sent
# faster, the firmware drops packets (observed: 1 of 10 lost when sent back to back).
WRITE_GAP = 0.010
# After a committing write the firmware is busy (flash) for >200 ms and returns unprocessed
# echoes; wait_ready() polls identify until it answers correctly.
READY_TIMEOUT = 3.0


def _ioc(direction, nr, size):
    return direction << 30 | size << 16 | ord("H") << 8 | nr


HIDIOCSFEATURE = _ioc(3, 0x06, REPORT_LEN + 1)
HIDIOCGFEATURE = _ioc(3, 0x07, REPORT_LEN + 1)

# Errors meaning the device vanished or is resetting. On unplug, HIDIOC*FEATURE fails with
# EPROTO (observed), others with ENODEV/ESHUTDOWN. If the device is in fact still there,
# reconnect() just reopens it.
GONE_ERRNOS = {errno.ENODEV, errno.ENOENT, errno.EIO, errno.EPIPE, errno.ENXIO,
               errno.EPROTO, errno.ESHUTDOWN, errno.ETIMEDOUT}


class CX68Error(Exception):
    pass


class NotFound(CX68Error):
    pass


class DeviceBusy(CX68Error):
    """Another of our processes holds the device, or (with strict=True) anything else has it open."""


class DeviceGone(CX68Error):
    """The keyboard was unplugged or reset mid-operation."""


class Refused(CX68Error):
    """policy.check rejected the packet; it was not sent."""


class NoReply(CX68Error):
    pass


@dataclass(frozen=True)
class Nodes:
    config: str            # /dev/hidrawN of interface 2
    vendor: str | None     # /dev/hidrawN of interface 1 (notifications)
    busnum: int
    devnum: int


def find(vid=VID, pid=PID) -> Nodes:
    """Locate the keyboard's hidraw nodes through sysfs only (no device I/O)."""
    want = f"0003:{vid:08X}:{pid:08X}"
    found, bus = {}, None
    for hr in Path("/sys/class/hidraw").glob("hidraw*"):
        dev = (hr / "device").resolve()
        try:
            if f"HID_ID={want}" not in (dev / "uevent").read_text():
                continue
        except OSError:
            continue
        m = re.search(r"/(\d+-[\d.]+):\d+\.(\d+)/", str(dev))
        if m:
            found[int(m.group(2))] = f"/dev/{hr.name}"
            usb = Path("/sys/bus/usb/devices") / m.group(1)
            try:
                bus = (int((usb / "busnum").read_text()), int((usb / "devnum").read_text()))
            except OSError:
                pass
    if CONFIG_IFACE not in found:
        raise NotFound(f"keyboard {vid:04x}:{pid:04x} not connected")
    return Nodes(found[CONFIG_IFACE], found.get(VENDOR_IFACE), *(bus or (0, 0)))


def other_openers(path) -> list[tuple[int, str]]:
    """(pid, command) of other processes with `path` open. Only sees our own user's processes."""
    target = os.stat(path).st_rdev
    me, out = os.getpid(), []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == me:
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    st = os.stat(fd)
                except OSError:
                    continue
                if st.st_rdev == target and (st.st_mode & 0o170000) == 0o020000:
                    comm = (proc / "comm").read_text().strip()
                    out.append((int(proc.name), comm))
                    break
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
    return out


class Device:
    """Exclusive, policy-checked access to the config channel.

    mode: "readonly" (default) or "write"; allow: volatile opcodes permitted in readonly
    (see policy.validate_allow). With strict=True, opening fails if any other process
    (e.g. a browser on the vendor page) has the config node open; otherwise they are only
    reported in .others, since interleaved transactions from two processes corrupt replies.
    """

    def __init__(self, mode="readonly", allow=(), allow_writes=(), strict=False, log=None, nodes: Nodes | None = None):
        self.mode = mode
        # Read-only plus individually approved opcodes: volatile ones (allow) and, for a
        # specific job such as a restore, individual config writes (allow_writes).
        self.allow = policy.validate_allow(allow) | policy.validate_allow_writes(allow_writes)
        self.strict = strict
        self.log = log or (lambda event: None)
        self.nodes = nodes
        self.fd = None
        self.others = []
        self.device_id = None
        self.firmware = None

    # lifecycle ---------------------------------------------------------------
    def open(self):
        self.nodes = find() if self.nodes is None else self.nodes
        self.others = other_openers(self.nodes.config)
        if self.strict and self.others:
            raise DeviceBusy(f"{self.nodes.config} is open in: {self.others}")
        fd = os.open(self.nodes.config, os.O_RDWR | os.O_CLOEXEC)
        try:
            # Advisory lock: keeps two cx68 processes from interleaving transactions.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DeviceBusy(f"{self.nodes.config} is locked by another cx68 process") from None
        self.fd = fd
        self.log({"type": "open", "path": self.nodes.config, "others": self.others})
        self._identify()
        return self

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def __enter__(self):
        return self.open() if self.fd is None else self

    def __exit__(self, *exc):
        self.close()

    def reconnect(self, timeout=10.0):
        """Wait for the keyboard to come back, reopen, and check it is the same model."""
        self.close()
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.nodes = find()
                return self.open()
            except (NotFound, DeviceGone, OSError):
                if time.monotonic() > deadline:
                    raise NotFound("keyboard did not come back") from None
                wait_for_hidraw_event(min(1.0, max(0.05, deadline - time.monotonic())))

    def _identify(self):
        reply = self.wait_ready()
        dev_id = struct.unpack_from("<I", reply, 1)[0]
        self.device_id, self.firmware = dev_id, reply[8] << 8 | reply[7]
        if dev_id != DEVICE_ID:
            self.close()
            raise CX68Error(f"device id {dev_id} is not a CX68 ({DEVICE_ID}); command table does not apply")

    def wait_ready(self, timeout=READY_TIMEOUT) -> bytes:
        """Poll GET_REV until the firmware answers with a real reply (not its echo).

        An unprocessed 8F echo is `8f 00 00 00 00 00 00 70`: device id 0. A real reply has a
        non-zero id. Used on open and after committing writes, when the firmware is busy.
        """
        deadline = time.monotonic() + timeout
        while True:
            reply = self.transact([0x8F], reply="echo")
            if reply[0] == 0x8F and any(reply[1:5]):
                return reply
            if time.monotonic() > deadline:
                raise NoReply(f"keyboard not ready after {timeout}s (last reply {reply[:9].hex(' ')})")
            time.sleep(0.02)

    # raw feature I/O ---------------------------------------------------------
    def _raw_ioctl(self, request, buf):
        fcntl.ioctl(self.fd, request, buf)

    def _ioctl(self, request, buf):
        if self.fd is None:
            raise DeviceGone("device not open")
        try:
            self._raw_ioctl(request, buf)
        except OSError as err:
            if err.errno in GONE_ERRNOS:
                self.close()
                raise DeviceGone(str(err)) from err
            raise

    def send(self, packet: bytes, checksum_type: int):
        reason = policy.check(packet, self.mode, checksum_type, self.allow)
        self.log({"type": "setFeature", "data": packet.hex(), **({"blocked": reason} if reason else {})})
        if reason:
            raise Refused(reason)
        self._ioctl(HIDIOCSFEATURE, bytearray(b"\0" + packet))

    def read(self) -> bytes:
        buf = bytearray(REPORT_LEN + 1)
        self._ioctl(HIDIOCGFEATURE, buf)
        return bytes(buf[1:])

    def transact(self, msg, checksum=policy.BIT7, reply="echo", timeout=REPLY_TIMEOUT, attempts=4) -> bytes | None:
        """Send one command and return its stable reply (see _transact_once).

        Reads are idempotent, so a read that gets no stable reply (firmware busy, e.g. still
        committing a write) is re-sent up to `attempts` times with backoff. Writes are never
        re-sent automatically.
        """
        for attempt in range(attempts if reply is not None else 1):
            try:
                return self._transact_once(msg, checksum, reply, timeout)
            except NoReply:
                if attempt == attempts - 1:
                    raise
                self.log({"type": "retry", "msg": bytes(msg)[:8].hex(" "), "attempt": attempt + 1})
                time.sleep(0.05 * (attempt + 1))

    def _transact_once(self, msg, checksum, reply, timeout) -> bytes | None:
        """Send one command and return its stable reply.

        reply="data": the reply carries no opcode echo (E5, 8A, 90, 8B, 8C, 9C, 9E);
            ready once byte 7 differs from the request's checksum byte and two reads agree.
        reply="echo": the reply starts with the opcode and may equal the request byte for
            byte (9D in comfort mode, 84 for profile 0), so wait SETTLE_ECHO, then two reads agree.
        reply=None: write-only command, no read.
        """
        req = policy.encode(bytes(msg), checksum)
        if reply is None:
            time.sleep(WRITE_GAP)
        self.send(req, checksum)
        if reply is None:
            time.sleep(WRITE_GAP)
            return None
        t0 = time.perf_counter()
        if reply == "echo" or SETTLE_DATA:
            time.sleep(SETTLE_DATA if reply == "data" else SETTLE_ECHO)
        prev = None
        while time.perf_counter() - t0 < timeout:
            r = self.read()
            if reply == "data":
                # Never accept the request echo (whole, or with byte 0 already cleared) as data.
                # byte 7 unchanged is normally "not ready", but after 5 ms of stability it is
                # taken as real data that happens to equal the checksum byte.
                ready = r[1:8] != req[1:8] and (r[7] != req[7] or time.perf_counter() - t0 > 0.005)
            else:
                # An opcode-echo reply always starts with the opcode; byte 0 cleared means
                # the firmware is mid-copy.
                ready = r[0] == req[0]
            if r == prev and ready:
                self.log({"type": "getFeature", "req": f"{req[0]:02x}", "data": r.hex()})
                return r
            prev = r
        raise NoReply(f"no stable reply to {req[:8].hex(' ')}")


def wait_for_hidraw_event(timeout):
    """Block until a hidraw add/remove uevent arrives (or timeout). Falls back to sleeping."""
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 15)  # NETLINK_KOBJECT_UEVENT
        sock.bind((0, 1))
    except OSError:
        time.sleep(timeout)
        return False
    with sock:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            r, _, _ = select.select([sock], [], [], left)
            if not r:
                break
            msg = sock.recv(8192)
            if b"SUBSYSTEM=hidraw" in msg:
                return True
    return False


class VendorReports:
    """Non-blocking reader for interface 1 report 5 (travel notifications etc.).

    The same node carries typing (NKRO report 1 and others); those are discarded here and
    never returned or logged.
    """

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, timeout) -> list[bytes]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        out = []
        while r:
            try:
                data = os.read(self.fd, 64)
            except BlockingIOError:
                break
            except OSError as err:
                if err.errno in GONE_ERRNOS:
                    raise DeviceGone(str(err)) from err
                raise
            if data and data[0] == VENDOR_REPORT_ID:
                out.append(data)
            r = select.select([self.fd], [], [], 0)[0]
        return out
