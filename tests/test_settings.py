import unittest

from cx68 import protocol as P
from cx68 import settings
from cx68.transport import Refused

from fakes import FakeDevice, FakeFirmware

W, A = 14, 9


def seeded_firmware():
    """Fake with the real board's factory values on every slot (2026-10-27 backup)."""
    fw = FakeFirmware()
    for prm, val in ((P.Param.ACTUATION, 200), (P.Param.RELEASE, 280), (P.Param.RT_PRESS, 50),
                     (P.Param.RT_RELEASE, 50), (P.Param.DEADZONE, 30)):
        for pg in range(4):
            fw.replies[fw._e5(prm, pg)] = val.to_bytes(2, "little") * 32
    for pg in range(2):
        fw.replies[fw._e5(P.Param.KEY_MODE, pg)] = bytes(64)
    return fw


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.fw = seeded_firmware()
        self.dev = FakeDevice(self.fw, allow_writes=settings.WRITES).open()

    def test_apply_changes_only_selected_keys_and_verifies(self):
        settings.apply(self.dev, [W, A], actuation=1.5, rapid_trigger=True, rt_press=0.2)
        s = settings.read(self.dev)
        self.assertEqual((s[W].actuation, s[A].actuation, s[15].actuation), (1.5, 1.5, 2.0))
        self.assertTrue(s[W].rapid_trigger and s[A].rapid_trigger)
        self.assertFalse(s[15].rapid_trigger)
        self.assertEqual((s[W].rt_press, s[15].rt_press, s[15].rt_release), (0.2, 0.5, 0.5))  # others untouched

    def test_write_order_and_commit_like_vendor(self):
        settings.apply(self.dev, [W], actuation=1.5, rapid_trigger=True)
        writes = [r for r in self.fw.received if r[0] == 0x65]
        self.assertEqual([r[1] for r in writes], [7, 7, 7] + [0] * 5)      # mode first, then actuation
        self.assertEqual([r[4] for r in writes], [0] * 7 + [1])            # commit on the very last page
        self.assertEqual(self.fw.dropped, [])

    def test_no_op_writes_nothing(self):
        self.assertEqual(settings.apply(self.dev, [W], actuation=2.0, rapid_trigger=False), [])
        self.assertFalse(any(r[0] == 0x65 for r in self.fw.received))

    def test_limits(self):
        for kw in ({"actuation": 0.05}, {"actuation": 3.6}, {"rt_press": 2.5}, {"deadzone": 1.2}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                settings.apply(self.dev, [W], **kw)
        with self.assertRaises(ValueError):
            settings.apply(self.dev, [], actuation=1.0)

    def test_verify_catches_firmware_ignoring_a_write(self):
        self.fw.min_write_gap = 1.0   # every write after the first is "dropped"
        with self.assertRaises(settings.VerifyError):
            settings.apply(self.dev, [41], actuation=1.5)   # Space: slot 41 lives in write page 1

    def test_needs_explicit_write_permission(self):
        dev = FakeDevice(seeded_firmware()).open()
        with self.assertRaises(Refused):
            settings.apply(dev, [W], actuation=1.5)


if __name__ == "__main__":
    unittest.main()
