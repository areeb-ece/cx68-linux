import unittest

from cx68 import lighting
from cx68app.keystate import IDLE, PRESSED, RESETTING, TRAVEL, KeyStates


class LightingTest(unittest.TestCase):
    def test_board_backup_value(self):
        # 0x87 reply from backups/cx68-20261027-104912.json: led "04 02 04 08 96 96 96"
        bl = lighting.parse(bytes([0x87, 0x04, 0x02, 0x04, 0x08, 0x96, 0x96, 0x96]))
        self.assertEqual((bl.name, bl.speed, bl.brightness, bl.rainbow), ("Wave", 2, 4, True))
        self.assertEqual(lighting.WAVE_DIRS[bl.option], "right")
        self.assertTrue(bl.lit)

    def test_preset_colour_and_off(self):
        bl = lighting.parse(bytes([0x87, 0x01, 0x00, 0x04, 0x03, 0, 0, 0]))
        self.assertEqual((bl.rgb, bl.rainbow), (0x00FF00, False))
        self.assertFalse(lighting.parse(bytes([0x87, 0, 0, 4, 7, 0, 0, 0])).lit)
        self.assertFalse(lighting.parse(bytes([0x87, 1, 0, 4, 7, 0, 0, 0]), on=False).lit)


def run(ks, seq, slot=0):
    out = []
    for d in seq:
        depth = [0.0] * 4
        depth[slot] = d
        out.append(ks.update(depth)[slot])
    return out


class KeyStateTest(unittest.TestCase):
    def make(self, act=2.0, rel=2.8, rapid=False):
        ks = KeyStates(4)
        ks.configure([act] * 4, [rel] * 4, [0.5] * 4, [0.5] * 4, [rapid] * 4)
        return ks

    def test_full_press_releases_at_release_depth(self):
        got = run(self.make(), [0.0, 1.0, 2.1, 3.5, 2.9, 2.7, 1.0, 0.0])
        self.assertEqual(got, [IDLE, TRAVEL, PRESSED, PRESSED, PRESSED, RESETTING, RESETTING, IDLE])

    def test_shallow_press_releases_above_actuation(self):
        got = run(self.make(), [1.0, 2.3, 2.1, 1.9, 0.0])
        self.assertEqual(got, [TRAVEL, PRESSED, PRESSED, RESETTING, IDLE])

    def test_going_down_again_while_resetting_is_travel(self):
        got = run(self.make(), [3.5, 1.5, 1.7])
        self.assertEqual(got, [PRESSED, RESETTING, TRAVEL])

    def test_rapid_trigger(self):
        got = run(self.make(rapid=True), [1.0, 2.1, 3.0, 2.6, 2.4, 2.8, 3.0, 1.0, 0.0])
        self.assertEqual(got, [TRAVEL, PRESSED, PRESSED, PRESSED, RESETTING, TRAVEL, PRESSED, RESETTING, IDLE])


if __name__ == "__main__":
    unittest.main()
