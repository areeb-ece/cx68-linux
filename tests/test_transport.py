import unittest

from cx68 import policy
from cx68 import protocol as P
from cx68.transport import CX68Error, DeviceGone, NoReply, Refused

from fakes import FakeDevice, FakeFirmware


class TransportTest(unittest.TestCase):
    def test_identify_through_echo_and_torn_replies(self):
        dev = FakeDevice(FakeFirmware(ready_after=0.0015)).open()
        self.assertEqual((dev.device_id, dev.firmware), (2427, 0x0400))

    def test_data_reply_never_returns_echo_or_torn_copy(self):
        fw = FakeFirmware(ready_after=0.001, torn=True)
        dev = FakeDevice(fw).open()
        req = policy.encode(P.get_magnetism_page(P.Param.ACTUATION, 0).msg, policy.BIT7)
        r = dev.transact(*P.get_magnetism_page(P.Param.ACTUATION, 0))
        self.assertEqual(r, fw.replies[req])
        self.assertEqual(r[:4], b"\xc8\x00\xc8\x00")  # 2.00 mm

    def test_echo_reply_identical_to_request(self):
        # GET_PROFILE for profile 0 replies byte-for-byte equal to the request.
        dev = FakeDevice().open()
        r = dev.transact(*P.get_profile())
        self.assertEqual(r[:8].hex(" "), "84 00 00 00 00 00 00 7b")

    def test_unresponsive_firmware_raises(self):
        dev = FakeDevice().open()
        dev.fw.respond = False
        with self.assertRaises(NoReply):
            dev.transact(*P.get_magnetism_page(P.Param.LIVE_TRAVEL, 0))

    def test_busy_read_is_resent(self):
        dev = FakeDevice().open()
        import time
        dev.fw.busy_until = time.perf_counter() + 0.08   # longer than one REPLY_TIMEOUT
        r = dev.transact(*P.get_magnetism_page(P.Param.ACTUATION, 0))
        self.assertEqual(r[:2], b"\xc8\x00")
        self.assertGreater(sum(1 for q in dev.fw.received if q[0] == 0xE5), 1)

    def test_writes_are_never_resent(self):
        dev = FakeDevice(allow=[0x1B]).open()
        dev.fw.respond = False
        dev.transact(*P.set_travel_report(True))
        self.assertEqual(sum(1 for q in dev.fw.received if q[0] == 0x1B), 1)

    def test_refused_packets_never_reach_device(self):
        dev = FakeDevice().open()
        before = len(dev.fw.received)
        for msg in ([0x01], [0x7F, 0x55, 0xAA, 0x55, 0xAA], [0xAC], [0x1C, 1], [0x50], [0x1D, 3, 0, 0], [0x1B, 1]):
            with self.subTest(msg=msg), self.assertRaises(Refused):
                dev.transact(bytes(msg))
        self.assertEqual(len(dev.fw.received), before)

    def test_travel_report_needs_explicit_allow(self):
        with self.assertRaises(Refused):
            FakeDevice().open().transact(*P.set_travel_report(True))
        dev = FakeDevice(allow=[0x1B]).open()
        dev.transact(*P.set_travel_report(True))
        self.assertEqual(dev.fw.received[-1][:2], b"\x1b\x01")

    def test_unplug_raises_device_gone_and_closes(self):
        dev = FakeDevice().open()
        dev.fw.unplugged = True
        with self.assertRaises(DeviceGone):
            dev.transact(*P.get_profile())
        self.assertIsNone(dev.fd)

    def test_wrong_model_is_rejected(self):
        fw = FakeFirmware()
        req = policy.encode(b"\x8f", policy.BIT7)
        fw.replies[req] = bytes([0x8F, 0x01, 0x02, 0, 0, 0, 0, 0, 0x04]).ljust(64, b"\0")
        with self.assertRaises(CX68Error):
            FakeDevice(fw).open()


if __name__ == "__main__":
    unittest.main()
