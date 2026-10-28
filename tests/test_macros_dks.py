"""Macros, media/system/mouse key actions and DKS (formats from the vendor source)."""
import unittest

from cx68 import advanced, config, keymap, macros, policy
from cx68 import protocol as P

from fakes import FakeDevice, FakeFirmware

W, A, D, CAPS = 14, 9, 21, 3
KA, KB = 0x04, 0x05


def ck7(msg):
    return policy.encode(msg, policy.BIT7)


def fake(writes):
    fw = FakeFirmware()
    fw.replies[ck7(bytes([0x84]))] = bytes([0x84, 0]) + bytes(62)
    return fw, FakeDevice(fw, allow_writes=writes).open()


class MacroFormatTest(unittest.TestCase):
    EVENTS = [macros.key(KA), macros.delay(30), macros.key(KA, False), macros.delay(200),
              macros.key(KB), macros.key(KB, False)]

    def test_encoding_follows_the_page(self):
        # A down with 30 ms inline; A up then a u16 200 ms; B down, u16 0; B up (no delay after it)
        buf = macros.encode(macros.Macro(1, self.EVENTS))
        self.assertEqual(buf[:18].hex(" "), "01 00 04 9e 04 00 c8 00 05 80 00 00 05 00 00 00 00 00")
        self.assertEqual(len(buf), P.MACRO_BYTES)

    def test_round_trip(self):
        m = macros.decode(macros.encode(macros.Macro(3, self.EVENTS)))
        self.assertEqual((m.repeat, m.events), (3, self.EVENTS))
        self.assertEqual(m.summary(), "A B")

    def test_long_delays_and_consecutive_delays(self):
        ev = [macros.delay(5), macros.key(KA), macros.delay(1000), macros.delay(40000), macros.key(KA, False)]
        m = macros.decode(macros.encode(macros.Macro(1, ev)))
        self.assertEqual(m.events, ev)

    def test_mouse_and_unknown_codes_survive(self):
        ev = [macros.Event("mouse", 240), macros.delay(10), macros.Event("mouse", 240, False),
              macros.Event("raw", 0xFA), macros.Event("raw", 0xFA, False)]
        self.assertEqual(macros.decode(macros.encode(macros.Macro(1, ev))).events, ev)

    def test_too_long_is_refused(self):
        ev = [e for _ in range(70) for e in (macros.key(KA), macros.delay(300), macros.key(KA, False), macros.delay(300))]
        self.assertFalse(macros.fits(macros.Macro(1, ev)))
        with self.assertRaisesRegex(ValueError, "too long"):
            macros.encode(macros.Macro(1, ev))

    def test_empty_buffer(self):
        self.assertTrue(macros.decode(bytes(256)).empty)
        self.assertEqual(P.set_macro(0, bytes(256)), [])      # the page never writes an empty macro

    def test_write_packets(self):
        buf = macros.encode(macros.Macro(1, [macros.key(KA), macros.key(KA, False)] * 20))
        cmds = P.set_macro(3, buf)
        self.assertEqual(len(cmds), 3)                          # 2 + 20*8 bytes = 3 pages of 56
        pkt = policy.encode(cmds[0].msg, cmds[0].checksum)
        self.assertEqual(pkt[:7].hex(" "), "0b 03 00 38 00 00 00")
        self.assertEqual([c.msg[4] for c in cmds], [0, 0, 1])   # commit on the last page
        self.assertEqual(pkt[8:64], buf[:56])
        self.assertIsNone(policy.check(pkt, "write", policy.BIT7))
        self.assertEqual(P.get_macro_page(3, 1).msg.hex(" "), "8b 03 01")

    def test_key_entries(self):
        self.assertEqual(macros.entry(4, "toggle"), bytes([9, 1, 4, 0]))
        self.assertEqual(macros.parse_entry(bytes([9, 2, 7, 0])), (7, "hold"))
        self.assertEqual(keymap.describe(bytes([9, 2, 7, 0])), "Macro 8 (held)")
        refs = macros.references({"base": [bytes(4)] * 3 + [macros.entry(0)], "win": [macros.entry(2)]})
        self.assertEqual(refs, {0: [("base", 3)], 2: [("win", 0)]})
        self.assertEqual(macros.free_slot(refs), 1)

    def test_recorder(self):
        r = macros.Recorder()
        r.feed(KA, True, 0.0)
        r.feed(KA, True, 50.0)                 # auto-repeat: ignored
        r.feed(KA, False, 80.4)
        r.feed(KB, True, 300.0)
        self.assertEqual(r.finish(), [macros.key(KA), macros.delay(80), macros.key(KA, False), macros.delay(220),
                                      macros.key(KB), macros.key(KB, False)])
        self.assertEqual(keymap.LINUX_TO_HID[30], KA)      # KEY_A
        self.assertEqual(keymap.LINUX_TO_HID[103], 0x52)   # KEY_UP


class MacroDeviceTest(unittest.TestCase):
    def setUp(self):
        self.fw, self.dev = fake(macros.WRITES)

    def test_write_read_verify(self):
        m = macros.Macro(2, [macros.key(0xE0), macros.key(0x06), macros.delay(15), macros.key(0x06, False),
                             macros.key(0xE0, False)])
        n = macros.write(self.dev, 5, m)
        self.assertEqual(n, 1)
        got = macros.read(self.dev, 5)
        self.assertEqual((got.repeat, got.events), (2, m.events))
        self.assertEqual(macros.read_all(self.dev), {5: got})

    def test_shorter_macro_over_a_longer_one(self):
        long = macros.Macro(1, [macros.key(KA), macros.delay(500), macros.key(KA, False), macros.delay(500)] * 12)
        macros.write(self.dev, 0, long)
        short = macros.Macro(1, [macros.key(KB), macros.key(KB, False)])
        macros.write(self.dev, 0, short)                   # stale bytes stay after the end marker
        self.assertEqual(macros.read(self.dev, 0).events, short.events)

    def test_empty_refused_and_needs_permission(self):
        with self.assertRaises(ValueError):
            macros.write(self.dev, 0, macros.Macro())
        from cx68.transport import Refused
        dev = FakeDevice(self.fw).open()
        with self.assertRaises(Refused):
            macros.write(dev, 0, macros.Macro(1, [macros.key(KA)]))


class SpecialKeysTest(unittest.TestCase):
    def test_media_system_mouse(self):
        vol = bytes([3, 0, 0xE9, 0])
        self.assertEqual(keymap.describe(vol), "Volume up")
        self.assertEqual(keymap.describe(bytes([3, 0, 0x92, 1])), "Calculator")      # usage 0x192
        self.assertEqual(keymap.describe(bytes([2, 0x82, 0, 0])), "System sleep")
        self.assertEqual(keymap.describe(bytes([1, 0, 240, 0])), "Mouse left")
        self.assertTrue(keymap.editable(vol))
        self.assertFalse(keymap.editable(macros.entry(0)))
        self.assertFalse(keymap.editable(keymap.FN_ENTRY))

    def test_remap_to_media_key(self):
        fw, dev = fake(keymap.WRITES)
        keymap.write_keys(dev, 0, 0, {CAPS: bytes([3, 0, 0xCD, 0])})
        self.assertEqual(keymap.describe(keymap.read_layer(dev, 0, 0)[CAPS]), "Play/Pause")


class DksTest(unittest.TestCase):
    TAP_PRESS_AND_RELEASE = advanced.dks_flag((1, 0, 0, 1))
    HOLD_PRESS_TO_LIFT = advanced.dks_flag((3, 2, 0, 0))     # held from the press through bottom-out to the lift

    def setUp(self):
        self.fw, self.dev = fake(advanced.WRITES)
        modes = bytearray(128)
        modes[W] = 0x80
        for pg in range(2):
            self.fw.replies[self.fw._e5(7, pg)] = bytes(modes[pg * 64:pg * 64 + 64])

    def mode(self, start=12):
        return advanced.KeyMode("dks", actions=(keymap.entry(0x1A), keymap.entry(0xE1), None, None),
                                flags=(self.TAP_PRESS_AND_RELEASE, self.HOLD_PRESS_TO_LIFT, 0, 0), start=start)

    def test_flag_bits(self):
        self.assertEqual(self.TAP_PRESS_AND_RELEASE, 0b01000001)
        self.assertEqual(self.HOLD_PRESS_TO_LIFT, 0b00001011)
        self.assertEqual(advanced.dks_codes(0b01000001), (1, 0, 0, 1))

    def test_apply_order_and_read_back(self):
        advanced.apply(self.dev, {W: self.mode()})
        got, modes, layers = advanced.read(self.dev, 0)
        self.assertEqual(modes[W], 0x82)                                    # dks, RT kept
        self.assertEqual((got[W].flags, got[W].start), ((0x41, 0x0B, 0, 0), 12))
        self.assertEqual(got[W].actions[:2], (keymap.entry(0x1A), keymap.entry(0xE1)))
        self.assertEqual(layers[2][W], keymap.DISABLED)                     # unused actions cleared
        per_key = [(r[1], r[3], r[4]) for r in self.fw.received if r[0] == 0x65 and r[2] == 0]
        self.assertEqual(per_key, [(P.Param.DKS_START, W, 0), (P.Param.DKS_FLAGS, W, 1)])   # start, flags (commit)
        start_pkt = next(r for r in self.fw.received if r[0] == 0x65 and r[1] == P.Param.DKS_START)
        self.assertEqual(start_pkt[8:10], bytes([12, 0]))

    def test_back_to_normal_clears_layers(self):
        advanced.apply(self.dev, {W: self.mode()})
        advanced.apply(self.dev, {W: advanced.KeyMode("normal")})
        got, modes, layers = advanced.read(self.dev, 0)
        self.assertNotIn(W, got)
        self.assertEqual([layers[i][W] for i in (1, 2, 3)], [keymap.DISABLED] * 3)

    def test_validation(self):
        good = self.mode()
        bad = [
            advanced.KeyMode("dks", actions=good.actions, flags=(advanced.dks_flag((0, 0, 0, 2)), 0, 0, 0), start=7),
            advanced.KeyMode("dks", actions=good.actions, flags=(advanced.dks_flag((3, 0, 0, 0)), 0, 0, 0), start=7),
            advanced.KeyMode("dks", actions=good.actions, flags=(0, 0, 0, 0), start=7),
            advanced.KeyMode("dks", actions=(None,) * 4, flags=(1, 0, 0, 0), start=7),
            self.mode(start=30),
        ]
        for km in bad:
            with self.subTest(km=km), self.assertRaises(ValueError):
                advanced.check_dks(km)
        advanced.check_dks(good)


class RestoreTest(unittest.TestCase):
    def test_macros_and_dks_round_trip(self):
        fw, dev = fake(config.RESTORE_WRITES)
        fw.replies[ck7(bytes([0x87]))] = bytes.fromhex("870402040896969600") + bytes(55)
        first = macros.Macro(1, [macros.key(KA), macros.delay(20), macros.key(KA, False)])
        macros.write(dev, 0, first)
        keymap.write_keys(dev, 0, 0, {CAPS: macros.entry(0)})
        backup = config.read_full_config(dev)
        self.assertEqual(backup["decoded"]["macros"], {"1": "A"})

        macros.write(dev, 0, macros.Macro(4, [macros.key(KB), macros.key(KB, False)]))
        keymap.write_keys(dev, 0, 0, {CAPS: keymap.entry(0x29)})
        advanced.apply(dev, {W: DksTest.mode(DksTest())})
        changed = config.read_full_config(dev)
        self.assertIn("macro 1: old A  new B", config.diff(backup, changed))

        cmds = config.plan_restore(backup, changed)
        ops = [c.msg[0] for c in cmds]
        self.assertEqual(ops[0], 0x0B)                       # macros before the keys that use them
        self.assertIn("0B macro 1 page 0 (commit)", config.describe_restore(cmds))
        config.execute(dev, cmds)
        restored = config.read_full_config(dev)
        # the DKS key's per-key values stay (as in the page, they only matter in DKS mode)
        left = [d for d in config.diff(backup, restored) if not d.startswith(("e5 04 00", "e5 08 00"))]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
