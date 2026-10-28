"""Keymap, Fn, lighting, profile and advanced-mode writers (formats from the vendor source)."""
import unittest

from cx68 import advanced, keymap, lighting, policy
from cx68 import protocol as P
from cx68.transport import Refused

from fakes import FakeDevice, FakeFirmware

W, A, D, CAPS = 14, 9, 21, 3


def ck7(msg):
    return policy.encode(msg, policy.BIT7)


class EncodingTest(unittest.TestCase):
    def test_single_key_packet(self):
        c = P.set_key(0, 0, W, keymap.entry(0x1A))          # W -> W, plain
        pkt = policy.encode(c.msg, c.checksum)
        self.assertEqual(pkt[:12].hex(" "), "0a 00 0e 00 00 01 00 e6 00 00 1a 00")
        self.assertIsNone(policy.check(pkt, "write", c.checksum))

    def test_entries(self):
        self.assertEqual(keymap.entry(0x06, "Ctrl"), bytes([0, 224, 6, 0]))
        self.assertEqual(keymap.describe(bytes([0, 224, 6, 0])), "Ctrl+C")
        self.assertEqual(keymap.describe(keymap.FN_ENTRY), "Fn")
        self.assertEqual(keymap.describe(bytes(4)), "Disabled")
        self.assertFalse(keymap.editable(keymap.FN_ENTRY))
        with self.assertRaises(ValueError):
            keymap.entry(0xF0)

    def test_lighting_matches_board_and_uses_bit8(self):
        bl = lighting.parse(bytes.fromhex("870402040896969600"))      # factory: rainbow wave right
        c = lighting.encode(bl)
        self.assertEqual(c.msg.hex(" "), "07 04 02 04 08 96 96 96")
        pkt = policy.encode(c.msg, c.checksum)
        self.assertEqual(pkt[8], (0xFF - sum(pkt[:8])) & 0xFF)       # checksum after blue
        self.assertIsNone(policy.check(pkt, "write", policy.BIT8))
        self.assertIsNotNone(policy.check(ck7(c.msg), "write", policy.BIT7))  # wrong checksum type refused

    def test_lighting_normalisation(self):
        white = lighting.Backlight(1, 3, 4, 2, False, 0xFFFFFF)
        self.assertEqual(lighting.encode(white).msg.hex(" "), "07 01 04 04 07 fa ff fa")  # no speed/variant, FAFFFA
        off = lighting.Backlight(0, 3, 4, 1, True, 0x123456)
        self.assertEqual(lighting.encode(off).msg.hex(" "), "07 00 04 00 07 00 00 00")
        with self.assertRaises(ValueError):
            lighting.encode(lighting.Backlight(13, 0, 4, 0, False, 0))     # user picture: not offered

    def test_mode_byte_options(self):
        self.assertEqual(P.key_mode_byte(0x87)["option"], "snap")
        self.assertTrue(P.key_mode_byte(0x87)["rapid_trigger"])


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeFirmware()
        self.fw.replies[ck7(bytes([0x84]))] = bytes([0x84, 0]) + bytes(62)
        self.dev = FakeDevice(self.fw, allow_writes=(0x0A, 0x10, 0x07, 0x04, 0x65)).open()

    def test_remap_writes_only_changes_and_verifies(self):
        n = keymap.write_keys(self.dev, 0, 0, {CAPS: keymap.entry(0x29), W: keymap.read_layer(self.dev, 0, 0)[W]})
        self.assertEqual(n, 1)
        self.assertEqual(keymap.read_layer(self.dev, 0, 0)[CAPS], keymap.entry(0x29))
        self.assertEqual([r[0] for r in self.fw.received if r[0] < 0x80], [0x0A])

    def test_remap_refuses_inactive_profile(self):
        with self.assertRaisesRegex(ValueError, "active profile"):
            keymap.write_keys(self.dev, 2, 0, {CAPS: keymap.entry(0x29)})

    def test_fn_layer(self):
        keymap.write_fn_keys(self.dev, {W: keymap.entry(0x52)})
        self.assertEqual(keymap.read_fn(self.dev)[W], keymap.entry(0x52))

    def test_lighting_apply(self):
        got = lighting.apply(self.dev, lighting.Backlight(2, 4, 3, 0, False, 0x5AA9FF))
        self.assertEqual((got.name, got.speed, got.brightness, got.rgb), ("Breathing", 4, 3, 0x5AA9FF))

    def test_verify_catches_dropped_key_write(self):
        self.fw.min_write_gap = 10.0
        self.fw.last_write = float("inf")       # every write is "too soon" and dropped
        with self.assertRaises(keymap.VerifyError):
            keymap.write_keys(self.dev, 0, 0, {CAPS: keymap.entry(0x29)})

    def test_needs_permission(self):
        dev = FakeDevice(self.fw).open()
        with self.assertRaises(Refused):
            keymap.write_keys(dev, 0, 0, {CAPS: keymap.entry(0x29)})


class AdvancedTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeFirmware()
        self.fw.replies[ck7(bytes([0x84]))] = bytes([0x84, 0]) + bytes(62)
        modes = bytearray(128)
        modes[W] = 0x80                                       # W has rapid trigger on
        for pg in range(2):
            self.fw.replies[self.fw._e5(7, pg)] = bytes(modes[pg * 64:pg * 64 + 64])
        self.dev = FakeDevice(self.fw, allow_writes=advanced.WRITES).open()

    def test_snap_pair_and_order(self):
        advanced.apply(self.dev, {A: advanced.KeyMode("snap", partner=D), D: advanced.KeyMode("snap", partner=A)})
        got, modes, _ = advanced.read(self.dev, 0)
        self.assertEqual((got[A].partner, got[D].partner), (D, A))
        self.assertEqual((modes[A], modes[D], modes[W]), (7, 7, 0x80))       # RT bit of W untouched
        writes = [r for r in self.fw.received if r[0] == 0x65]
        self.assertEqual([(r[1], r[2]) for r in writes], [(7, 1)] * 3 + [(9, 0)] * 2)   # mode array (3 pages), then partners
        self.assertEqual([r[4] for r in writes], [0, 0, 0, 0, 1])           # commit on the very last packet

    def test_mod_tap_and_back(self):
        tap, hold = keymap.entry(0x04), keymap.entry(0xE1)
        advanced.apply(self.dev, {W: advanced.KeyMode("mt", tap=tap, hold=hold, hold_units=25)})
        got, modes, layers = advanced.read(self.dev, 0)
        self.assertEqual((got[W].tap, got[W].hold, got[W].hold_units), (tap, hold, 25))
        self.assertEqual(modes[W], 0x83)                                     # mt with RT kept
        advanced.apply(self.dev, {W: advanced.KeyMode("normal")})
        got, modes, layers = advanced.read(self.dev, 0)
        self.assertNotIn(W, got)
        self.assertEqual(layers[1][W], keymap.DISABLED)

    def test_validation(self):
        for km in (advanced.KeyMode("snap"), advanced.KeyMode("mt", tap=b"\0\0\4\0"),
                   advanced.KeyMode("mt", tap=bytes(4), hold=bytes(4), hold_units=0), advanced.KeyMode("dks")):
            with self.subTest(km=km), self.assertRaises(ValueError):
                advanced.plan({W: km}, [0] * 128, [[bytes(4)] * 128] * 4, 0)


if __name__ == "__main__":
    unittest.main()


class RestoreTest(unittest.TestCase):
    def test_backup_change_restore_roundtrip(self):
        from cx68 import config
        fw = FakeFirmware()
        fw.replies[ck7(bytes([0x84]))] = bytes([0x84, 0]) + bytes(62)
        fw.replies[ck7(bytes([0x87]))] = bytes.fromhex("870402040896969600") + bytes(55)
        dev = FakeDevice(fw, allow_writes=config.RESTORE_WRITES).open()
        backup = config.read_full_config(dev)
        keymap.write_keys(dev, 0, 0, {CAPS: keymap.entry(0x29)})
        keymap.write_fn_keys(dev, {W: keymap.entry(0x52)})
        lighting.apply(dev, lighting.Backlight(1, 0, 2, 0, False, 0xFF0000))
        advanced.apply(dev, {A: advanced.KeyMode("snap", partner=D), D: advanced.KeyMode("snap", partner=A)})
        changed = config.read_full_config(dev)
        self.assertTrue(config.diff(backup, changed))
        cmds = config.plan_restore(backup, changed)
        self.assertEqual(sorted({c.msg[0] for c in cmds}), [0x07, 0x0A, 0x10, 0x65])
        steps = config.describe_restore(cmds)
        self.assertIn("0A key slot 3 layer 0 → Caps Lock", steps)
        self.assertIn("07 lighting → Wave · rainbow · right · speed 2/4 · brightness 4/4", steps)
        for c in cmds:
            dev.transact(*c)
            dev.wait_ready()
        restored = config.read_full_config(dev)
        # the snap keys' per-key partner values stay (as in the page, they only matter in snap mode)
        self.assertEqual([d for d in config.diff(backup, restored) if not d.startswith("e5 09 00")], [])
