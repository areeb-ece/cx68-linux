"""Tests for policy.py and iot_driver_shim.py without touching the keyboard.

    python3 -m unittest discover -s tools/capture -p 'test_*.py'
"""
import base64
import http.client
import struct
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import iot_driver_shim as shim_mod  # noqa: E402
import policy  # noqa: E402
from iot_driver_shim import first, frame, pb, pb_decode  # noqa: E402


def packet(msg, ck=policy.BIT7):
    return policy.encode(bytes(msg), ck)


class PolicyTest(unittest.TestCase):
    """Same cases as test-policy.mjs, so the JS and Python policies stay in step."""

    def assertAllowed(self, msg, mode, ck=policy.BIT7):
        self.assertIsNone(policy.check(packet(msg, ck), mode))

    def assertBlocked(self, pkt, mode="write"):
        self.assertIsNotNone(policy.check(pkt, mode))

    def test_reads_allowed_readonly(self):
        self.assertAllowed([0x8F], "readonly")
        self.assertAllowed([0xE5, 0, 1, 0], "readonly")
        self.assertAllowed([0x87], "readonly")

    def test_writes_need_write_mode(self):
        self.assertBlocked(packet([0x1B, 1]), "readonly")
        self.assertAllowed([0x1B, 1], "write")

    def test_checksum_type_must_match_opcode(self):
        self.assertAllowed([0x07, 1, 2, 3], "write")
        p = bytearray(packet([0x07, 1, 2, 3]))
        p[7] ^= 1
        self.assertBlocked(bytes(p))
        # Declared type must match the opcode's expected type (BIT7 unless listed).
        self.assertIsNotNone(policy.check(packet([0x0A, 1], policy.BIT8), "write", policy.BIT8))
        self.assertIsNone(policy.check(packet([0x07, 1], policy.BIT8), "write", policy.BIT8))
        self.assertIsNotNone(policy.check(packet([0x07, 1]), "write", policy.BIT7))
        self.assertIsNotNone(policy.check(packet([0x8F], policy.NONE), "readonly", policy.NONE))
        self.assertIsNone(policy.check(packet([0x8F]), "readonly", policy.BIT7))

    def test_none_only_for_listed_opcodes(self):
        saved = set(policy.NONE_OPCODES)
        try:
            policy.NONE_OPCODES.add(0x65)
            data = bytes([0x65, 7, 1, 0, 1]) + bytes(range(1, 60))
            self.assertIsNone(policy.check(policy.encode(data, policy.NONE), "write", policy.NONE))
            self.assertIsNotNone(policy.check(policy.encode(data, policy.NONE), "readonly", policy.NONE))
        finally:
            policy.NONE_OPCODES.clear()
            policy.NONE_OPCODES.update(saved)

    def test_allow_volatile_only(self):
        allow = policy.validate_allow([0x1B])
        self.assertIsNone(policy.check(packet([0x1B, 1]), "readonly", policy.BIT7, allow))
        # The shim passes a generator (int(x, 16) for x in args.allow); it must not be lost.
        self.assertEqual(policy.validate_allow(int(x, 16) for x in ["0x1B"]), frozenset({0x1B}))
        self.assertIsNotNone(policy.check(packet([0x1D, 3]), "readonly", policy.BIT7, allow))
        for op in (0x1D, 0x01, 0x7F, 0x8F, 0x50):  # write, deny, deny, read, unknown
            with self.subTest(op=op), self.assertRaises(ValueError):
                policy.validate_allow([op])

    def test_allow_individual_writes_only(self):
        allow = policy.validate_allow_writes(x for x in [0x65])
        self.assertIsNone(policy.check(packet([0x65, 0, 1, 0, 1]), "readonly", policy.BIT7, allow))
        self.assertIsNotNone(policy.check(packet([0x0A, 0, 0xFF]), "readonly", policy.BIT7, allow))
        for op in (0x01, 0x1C, 0x1B, 0x8F, 0x50):  # deny, deny, volatile, read, unknown
            with self.subTest(op=op), self.assertRaises(ValueError):
                policy.validate_allow_writes([op])

    def test_battery_read_known(self):
        self.assertAllowed([0x82], "readonly")

    def test_denylist(self):
        for msg in ([0x01], [0x02], [0x7F, 0x55, 0xAA, 0x55, 0xAA], [0xF8, 0x55, 0xAA, 0x55, 0xAA],
                    [0xBA, 0xC0, 1, 0], [0xAC], [0x1C, 1], [0x1E, 1], [0x30], [0x40], [0x32]):
            with self.subTest(msg=msg):
                self.assertBlocked(packet(msg))

    def test_unknown_opcodes(self):
        self.assertBlocked(packet([0x50]))
        self.assertBlocked(packet([0xE6]))  # v4-only opcode, not in this class chain
        self.assertBlocked(packet([0xD0]), "readonly")
        self.assertBlocked(packet([0xF1]))  # dongle id probe, not for wired keyboards

    def test_unchecksummed_chunk(self):
        raw = bytearray([0x5A] * 64)
        raw[0] = 0x8F
        self.assertBlocked(bytes(raw))

    def test_wrong_length(self):
        self.assertBlocked(packet([0x8F])[:63])

    def test_encode_matches_capture(self):
        # From captures/01-readonly-tour.jsonl: the app's GET_USB_VERSION request.
        self.assertEqual(packet([0x8F]).hex(), "8f00000000000070" + "00" * 56)


class ProtoTest(unittest.TestCase):
    def test_roundtrip(self):
        buf = pb((1, "b", "/dev/hidraw5"), (2, "b", b"\x8f\x00"), (3, "v", 1), (4, "v", 0))
        f = pb_decode(buf)
        self.assertEqual(first(f, 1), b"/dev/hidraw5")
        self.assertEqual(first(f, 2), b"\x8f\x00")
        self.assertEqual(first(f, 3), 1)
        self.assertNotIn(4, f)  # proto3 default omitted

    def test_large_varint(self):
        f = pb_decode(pb((4, "v", 2427), (7, "v", 0x3151)))
        self.assertEqual((first(f, 4), first(f, 7)), (2427, 0x3151))


class FakeChannel:
    """Stands in for ConfigChannel; records what would have reached the keyboard."""

    def __init__(self, mode, log):
        self.path, self.mode, self.log = "/dev/hidraw-test", mode, log
        self.sent = []
        self.reply = bytes([0x8F, 0x7B, 0x09, 0, 0, 0, 0, 0, 0x04]).ljust(64, b"\0")

    def send(self, pkt, checksum_type=None):
        reason = policy.check(pkt, self.mode, checksum_type)
        if not reason:
            self.sent.append(pkt)
        return reason

    def read(self):
        return self.reply


class ShimHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shim = shim_mod.Shim("readonly", Path(self.tmp.name) / "log.jsonl")
        self.shim.log = lambda e: None
        self.shim.db_dir = Path(self.tmp.name) / "db"
        self.fake = FakeChannel("readonly", self.shim.log)
        self.shim.cfg = self.fake
        self.shim.device = {"path": self.fake.path, "id": 2427, "vid": 0x3151, "pid": 0x502D}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), shim_mod.make_handler(self.shim))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, req=b"", text=True, origin="https://iotdriver.qmk.top"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = frame(req)
        ctype = "application/grpc-web-text" if text else "application/grpc-web+proto"
        conn.request("POST", f"/driver.DriverGrpc/{method}", base64.b64encode(body) if text else body,
                     {"Content-Type": ctype, "Origin": origin, "X-Grpc-Web": "1"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status != 200:
            return resp.status, None, None
        if text:
            data = b"".join(base64.b64decode(data[i:i + 4]) for i in range(0, len(data), 4))
        msgs, trailer, i = [], None, 0
        while i < len(data):
            flag, n = data[i], struct.unpack(">I", data[i + 1:i + 5])[0]
            (msgs.append if not flag & 0x80 else lambda m: None)(data[i + 5:i + 5 + n])
            if flag & 0x80:
                trailer = data[i + 5:i + 5 + n].decode()
            i += 5 + n
        return resp.status, msgs, trailer

    def send_msg(self, msg, ck=policy.BIT7, **kw):
        status, msgs, trailer = self.call("sendMsg", pb((1, "b", self.fake.path), (2, "b", bytes(msg)), (3, "v", ck)), **kw)
        self.assertIn("grpc-status:0", trailer)
        return first(pb_decode(msgs[0]), 1, b"").decode()

    def test_version_text_and_binary(self):
        for text in (True, False):
            status, msgs, trailer = self.call("getVersion", text=text)
            self.assertEqual(status, 200)
            self.assertEqual(first(pb_decode(msgs[0]), 1), b"2.2.2")

    def test_read_command_reaches_device(self):
        self.assertEqual(self.send_msg([0x8F]), "")
        self.assertEqual(self.fake.sent, [packet([0x8F])])
        status, msgs, _ = self.call("readMsg", pb((1, "b", self.fake.path)))
        self.assertEqual(first(pb_decode(msgs[0]), 2), self.fake.reply)

    def test_write_blocked_in_readonly(self):
        self.assertIn("read-only", self.send_msg([0x1B, 1]))
        self.assertEqual(self.fake.sent, [])

    def test_dangerous_blocked_even_in_write_mode(self):
        self.fake.mode = "write"
        for msg in ([0x01], [0x7F, 0x55, 0xAA, 0x55, 0xAA], [0xAC]):
            self.assertIn("denylisted", self.send_msg(msg))
        self.assertIn("checksum NONE", self.send_msg([0x8F], ck=policy.NONE))
        self.assertIn("unknown checksum type", self.send_msg([0x8F], ck=500))
        self.assertEqual(self.fake.sent, [])

    def test_raw_feature_and_ota_refused(self):
        _, msgs, _ = self.call("sendRawFeature", pb((1, "b", self.fake.path), (2, "b", b"\xf1"), (3, "v", 0)))
        self.assertIn(b"not supported", first(pb_decode(msgs[0]), 1))
        _, _, trailer = self.call("upgradeOTAGATT", b"")
        self.assertIn("grpc-status:12", trailer)
        self.assertEqual(self.fake.sent, [])

    def test_foreign_origin_rejected(self):
        status, _, _ = self.call("sendMsg", pb((1, "b", self.fake.path), (2, "b", b"\x8f")), origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.fake.sent, [])

    def test_wrong_device_path(self):
        _, msgs, _ = self.call("sendMsg", pb((1, "b", "/dev/hidraw0"), (2, "b", b"\x8f")))
        self.assertEqual(first(pb_decode(msgs[0]), 1), b"device not found")

    def test_devlist_message(self):
        dl = pb_decode(self.shim.devlist_msg(0))
        dev = pb_decode(first(pb_decode(first(dl, 1)), 1))
        self.assertEqual((first(dev, 3), first(dev, 4), first(dev, 6), first(dev, 8)),
                         (self.fake.path.encode(), 2427, 1, 0x502D))

    def test_db_roundtrip(self):
        self.call("insertDb", pb((1, "b", "cfg"), (2, "b", b"k"), (3, "b", b"v")))
        _, msgs, _ = self.call("getItemFromDb", pb((1, "b", "cfg"), (2, "b", b"k")))
        self.assertEqual(first(pb_decode(msgs[0]), 1), b"v")
        _, msgs, _ = self.call("getAllKeysFromDb", pb((1, "b", "cfg")))
        self.assertEqual(pb_decode(msgs[0])[1], [b"k"])


if __name__ == "__main__":
    unittest.main()
