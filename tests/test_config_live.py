import json
import tempfile
import unittest
from pathlib import Path

from cx68 import config, live
from cx68 import protocol as P
from cx68.transport import Refused

from fakes import FIXTURES, FakeDevice


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDevice().open()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_read_decodes_factory_settings(self):
        cfg = config.read_full_config(self.dev)
        d = cfg["decoded"]
        self.assertEqual(cfg["device"]["id"], 2427)
        self.assertEqual(d["travel_unit_mm"], 0.01)
        self.assertEqual(len(d["per_key_active_profile"]), 68)
        w = d["per_key_active_profile"]["14"]
        self.assertEqual((w["actuation"], w["release"], w["deadzone"]), (2.0, 2.8, 0.3))
        self.assertFalse(w["mode"]["rapid_trigger"])
        # Nothing but reads reached the device.
        self.assertTrue(all(r[0] >= 0x80 for r in self.dev.fw.received))

    def test_diff(self):
        a = config.read_full_config(self.dev)
        b = json.loads(json.dumps(a))
        self.assertEqual(config.diff(a, b), [])
        key = next(k for k in b["raw"] if k.startswith("e5 00"))
        b["raw"][key] = "96" + b["raw"][key][2:]
        self.assertEqual(len(config.diff(a, b)), 1)

    def test_restore_brings_device_back_to_backup(self):
        from cx68 import protocol as P
        # The real board's RT presets are 50 (0.50 mm) on every slot (backup 2026-10-27).
        for prm in (P.Param.RT_PRESS, P.Param.RT_RELEASE):
            for pg in range(4):
                self.dev.fw.replies[self.dev.fw._e5(prm, pg)] = bytes([0x32, 0]) * 32
        backup = config.read_full_config(self.dev)
        dev = FakeDevice(self.dev.fw, allow_writes=config.RESTORE_WRITES).open()
        # Reproduce the vendor page's side effect: RT values of all keys zeroed.
        for c in P.set_magnetism_array(P.Param.RT_PRESS, bytes(256), last=False) + \
                 P.set_magnetism_array(P.Param.RT_RELEASE, bytes(256), last=True):
            dev.transact(*c)
        changed = config.read_full_config(dev)
        self.assertEqual(len(config.diff(backup, changed)), 8)
        plan = config.plan_restore(backup, changed)
        self.assertEqual([c.msg[1] for c in plan], [2] * 5 + [3] * 5)
        self.assertEqual([c.msg[4] for c in plan], [0] * 9 + [1])  # commit on the very last page
        for c in plan:
            dev.transact(*c)
        self.assertEqual(self.dev.fw.dropped, [])            # paced writes are never dropped
        self.assertEqual(config.diff(backup, config.read_full_config(dev)), [])  # waits out the busy commit
        self.assertEqual(config.plan_restore(backup, backup), [])

    def test_restore_refuses_other_writes_without_approval(self):
        from cx68 import protocol as P
        from cx68.transport import Refused
        with self.assertRaises(Refused):
            self.dev.transact(*P.set_magnetism_array(P.Param.ACTUATION, bytes(256), last=True)[0])

    def test_backup_roundtrip_and_tamper_detection(self):
        cfg = config.read_full_config(self.dev)
        path = Path(self.tmp.name) / "b.json"
        config.save(cfg, path)
        self.assertEqual(config.load(path)["raw"], cfg["raw"])
        data = json.loads(path.read_text())
        key = next(k for k in data["raw"] if k.startswith("e5 00"))
        data["raw"][key] = "00" * 64
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            config.load(path)


class FakeReports:
    def __init__(self, path):
        self.reports = [bytes.fromhex(h) for h in json.loads((FIXTURES / "stream08.json").read_text())["reports"][:50]]
        self.closed = False

    def read(self, timeout):
        out, self.reports = self.reports, []
        return out

    def close(self):
        self.closed = True


class LiveTest(unittest.TestCase):
    def test_poll_frames_and_reconnect(self):
        dev = FakeDevice().open()
        statuses = []
        frames = live.poll(dev, on_status=statuses.append)
        self.assertEqual(len(next(frames).travel), P.SLOTS)
        dev.fw.unplugged = True
        self.assertEqual(len(next(frames).travel), P.SLOTS)  # reconnected transparently
        self.assertEqual(statuses[-1], "reconnected")

    def test_stream_always_switched_off(self):
        orig = live.VendorReports
        live.VendorReports = FakeReports
        try:
            dev = FakeDevice(allow=[0x1B]).open()
            with self.assertRaises(RuntimeError):
                with live.TravelStream(dev) as s:
                    events = list(s.events())
                    raise RuntimeError("consumer crashed")
            sent = [r[:2] for r in dev.fw.received if r[0] == 0x1B]
            self.assertEqual(sent, [b"\x1b\x01", b"\x1b\x00"])
            self.assertEqual(len(events), 50)
        finally:
            live.VendorReports = orig

    def test_stream_refused_without_allow(self):
        orig = live.VendorReports
        live.VendorReports = FakeReports
        try:
            with self.assertRaises(Refused):
                with live.TravelStream(FakeDevice().open()):
                    pass
        finally:
            live.VendorReports = orig


if __name__ == "__main__":
    unittest.main()
