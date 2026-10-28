import unittest

from cx68bridge import mapping as M
from cx68bridge import uinput as U

SLOTS = {"W": 14, "A": 9, "S": 15, "D": 21, "Space": 41, "Q": 8, "E": 20}
ABS_X, ABS_Z, ABS_RZ = U.ABS["ABS_X"], U.ABS["ABS_Z"], U.ABS["ABS_RZ"]


def frame(**mm):
    d = [0.0] * 128
    for k, v in mm.items():
        d[SLOTS[k]] = v
    return d


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = M.parse(M.DEFAULT_CONFIG)
        self.m = M.Mapper(self.cfg, SLOTS)

    def test_default_config_is_valid_and_xbox_shaped(self):
        self.assertEqual(self.cfg.style, "xbox360")
        self.assertEqual(len(self.cfg.all_axes()), 8)          # every xpad axis, mapped or not
        self.assertEqual(len(self.cfg.all_buttons()), 11)
        self.assertEqual(self.cfg.keys(), {"W", "A", "S", "D", "Space", "Q", "E"})

    def test_deadzone_saturation_and_trigger_range(self):
        self.assertEqual(self.m(frame(W=0.15))[(U.EV_ABS, ABS_RZ)], 0)
        self.assertEqual(self.m(frame(W=1.70))[(U.EV_ABS, ABS_RZ)], 128)     # (1.7-0.2)/(3.2-0.2) = 0.5
        self.assertEqual(self.m(frame(W=3.40))[(U.EV_ABS, ABS_RZ)], 255)

    def test_steering_sides_and_combine(self):
        self.assertEqual(self.m(frame(A=3.3))[(U.EV_ABS, ABS_X)], -32768)
        self.assertEqual(self.m(frame(D=3.3))[(U.EV_ABS, ABS_X)], 32767)
        self.assertGreater(self.m(frame(A=1.0, D=2.0))[(U.EV_ABS, ABS_X)], 0)      # deeper wins

    def test_last_pressed_wins(self):
        cfg = M.parse(M.DEFAULT_CONFIG.replace('combine = "deeper"', 'combine = "last"'))
        m = M.Mapper(cfg, SLOTS)
        m(frame(A=2.0))
        self.assertGreater(m(frame(A=3.0, D=1.0))[(U.EV_ABS, ABS_X)], 0)       # D came last
        m(frame())
        m(frame(D=2.0))
        self.assertLess(m(frame(A=1.0, D=3.0))[(U.EV_ABS, ABS_X)], 0)          # A came last

    def test_curve_and_calibration(self):
        shape = M.Shape(0.2, 3.2, 2.0)
        self.assertAlmostEqual(M.amount(1.7, shape), 0.25)
        self.assertEqual(M.amount(3.0, M.Shape(0.2, 3.2, 1.0), rest=0.0, bottom=3.0), 1.0)   # bottoms out early
        self.assertEqual(M.amount(0.25, M.Shape(0.2, 3.2, 1.0), rest=0.1), 0.0)              # rest noise ignored

    def test_button_hysteresis(self):
        btn = (U.EV_KEY, U.BTN["BTN_A"])
        self.assertEqual(self.m(frame(Space=1.25))[btn], 1)
        self.assertEqual(self.m(frame(Space=1.15))[btn], 1)    # within the hysteresis band
        self.assertEqual(self.m(frame(Space=1.05))[btn], 0)

    def test_two_keys_on_one_button(self):
        cfg = M.parse(M.DEFAULT_CONFIG + '\n[[button]]\noutput = "rb"\nkey = "W"\n')
        m = M.Mapper(cfg, SLOTS)
        rb = (U.EV_KEY, U.BTN["BTN_TR"])
        self.assertEqual(m(frame(E=2.0))[rb], 1)
        self.assertEqual(m(frame(W=2.0))[rb], 1)       # W is processed after E but must not clear it
        self.assertEqual(m(frame())[rb], 0)

    def test_errors(self):
        for bad, fragment in ((M.DEFAULT_CONFIG.replace('output = "left_x"', 'output = "wheel"'), "not one of"),
                              (M.DEFAULT_CONFIG.replace('positive = "S"', 'negative = "S"'), "no negative side"),
                              (M.DEFAULT_CONFIG.replace('style = "xbox360"', 'style = "ps5"'), "style"),
                              (M.DEFAULT_CONFIG.replace("saturation_mm = 3.20", "saturation_mm = 0.1"), "deadzone_mm")):
            with self.subTest(fragment=fragment), self.assertRaisesRegex(M.ConfigError, fragment):
                M.parse(bad)
        with self.assertRaisesRegex(M.ConfigError, "unknown key"):
            M.Mapper(self.cfg, {"W": 14})

    def test_generic_style(self):
        cfg = M.parse('[pad]\nstyle = "generic"\n[[axis]]\noutput = "wheel"\nnegative = "A"\npositive = "D"\n'
                      '[[button]]\noutput = "b2"\nkey = "Space"\n')
        codes = {a.code for a in cfg.all_axes()}
        self.assertTrue({U.ABS["ABS_X"], U.ABS["ABS_Y"], U.ABS["ABS_WHEEL"]} <= codes)   # x/y keep it a joystick
        self.assertEqual(cfg.device_id["name"], "CX68 Analog Gamepad")


if __name__ == "__main__":
    unittest.main()
