"""Replay harness: our builders must regenerate the vendor's captured requests byte for byte,
and our parsers must decode the captured replies to the values seen on the device.

Fixture: tests/fixtures/vendor_session04.json, the official iotdriver page reading the CX68
through the shim; the same session was cross-checked 696/696 against a usbmon capture.
"""
import json
import unittest

from cx68 import config, policy
from cx68 import protocol as P

from fakes import FIXTURES

DATA = json.loads((FIXTURES / "vendor_session04.json").read_text())
PAIRS = [(bytes.fromhex(p["request"]), bytes.fromhex(p["reply"])) for p in DATA["pairs"]]


def rebuild(req: bytes) -> P.Cmd:
    """Map a captured request back to the builder that should produce it."""
    op = req[0]
    simple = {0x8F: P.get_rev, 0x82: P.get_battery, 0x84: P.get_profile, 0x85: P.get_led_onoff,
              0x86: P.get_debounce, 0x87: P.get_led, 0x88: P.get_side_led, 0x91: P.get_sleep,
              0x97: P.get_auto_os, 0x9D: P.get_key_mode}
    if op in simple:
        return simple[op]()
    if op == 0x89:
        return P.get_kboption(req[1])
    if op == 0x83:
        return P.get_report_rate(req[1])
    if op == 0x8A:
        return P.get_keymap_page(profile=req[1], layer=req[4], page=req[3])
    if op == 0x90:
        os_name = {v: k for k, v in P.FN_OS.items()}[req[1]]
        return P.get_fn_page(os_name, layer=req[2], page=req[4])
    if op == 0xE5:
        return P.get_magnetism_page(P.Param(req[1]), page=req[3])
    raise AssertionError(f"no builder for captured request {req[:8].hex(' ')}")


def reply_to(cmd):
    req = policy.encode(cmd.msg, cmd.checksum)
    return next(r for q, r in PAIRS if q == req)


class ReplayTest(unittest.TestCase):
    def test_every_captured_request_rebuilds_byte_for_byte(self):
        for req, _ in PAIRS:
            with self.subTest(req=req[:8].hex(" ")):
                cmd = rebuild(req)
                self.assertEqual(policy.encode(cmd.msg, cmd.checksum), req)

    def test_full_read_plan_matches_vendor_where_vendor_read_the_same_thing(self):
        vendor = {q for q, _ in PAIRS}
        vendor_kinds = {(q[0], q[1]) if q[0] == 0xE5 else q[0] for q in vendor}
        ours = config.request_packets()
        overlap = [p for p in ours if ((p[0], p[1]) if p[0] == 0xE5 else p[0]) in vendor_kinds and p[0] != 0x89]
        missing = [p[:8].hex(" ") for p in overlap if p not in vendor]
        self.assertEqual(missing, [])
        self.assertGreater(len(overlap), 150)  # keymaps, Fn, E5 params, singles

    def test_full_read_plan_is_read_only(self):
        for pkt in config.request_packets():
            self.assertIsNone(policy.check(pkt, "readonly", policy.BIT7), pkt[:8].hex(" "))

    def test_vendor_writes_in_capture_are_refused_readonly(self):
        for hexreq in DATA["blocked_requests"]:
            self.assertIsNotNone(policy.check(bytes.fromhex(hexreq), "readonly", policy.BIT7))

    def test_parsers_on_captured_replies(self):
        self.assertEqual(P.parse_rev(reply_to(P.get_rev())), (2427, 0x0400))
        self.assertEqual(reply_to(P.get_profile())[1], 0)
        act = P.u16_slots([reply_to(P.get_magnetism_page(P.Param.ACTUATION, pg)) for pg in range(4)])
        rel = P.u16_slots([reply_to(P.get_magnetism_page(P.Param.RELEASE, pg)) for pg in range(4)])
        dz = P.u16_slots([reply_to(P.get_magnetism_page(P.Param.DEADZONE, pg)) for pg in range(4)])
        mode = P.u8_slots([reply_to(P.get_magnetism_page(P.Param.KEY_MODE, pg)) for pg in range(2)])
        self.assertEqual(len(act), P.SLOTS)
        keymap = P.keymap_entries([reply_to(P.get_keymap_page(0, 0, pg)) for pg in range(8)])
        used = [s for s, e in enumerate(keymap) if any(e)]
        self.assertEqual(len(used), 68)
        pick = lambda xs: {xs[s] for s in used}  # noqa: E731
        self.assertEqual((pick(act), pick(rel), pick(dz), pick(mode)), ({200}, {280}, {30}, {0}))
        self.assertEqual(act[127], 0xFFFF)  # unused slots read as 0xFFFF
        self.assertEqual({s: keymap[s][2] for s in (9, 14, 15, 21, 41)},
                         {9: 0x04, 14: 0x1A, 15: 0x16, 21: 0x07, 41: 0x2C})  # A W S D Space

    def test_stream_fixture_decodes(self):
        reports = [bytes.fromhex(h) for h in json.loads((FIXTURES / "stream08.json").read_text())["reports"]]
        parsed = [P.parse_travel_notification(r) for r in reports]
        self.assertTrue(all(parsed))
        self.assertLessEqual(max(v for _, v in parsed), 350)
        self.assertLessEqual({s for s, _ in parsed}, {9, 14, 15, 21, 41, 81})


if __name__ == "__main__":
    unittest.main()
