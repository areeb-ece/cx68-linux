"""The bridge loop: poll travel (read-only) -> mapping -> uinput pad.

Owns the keyboard's config channel like cx68app does, so the two cannot run at the same
time (the second one reports the device busy).
"""
import fcntl
import json
import os
import select
import statistics
import struct
import threading
import time

from cx68 import live
from cx68 import protocol as P
from cx68.transport import CX68Error, Device, DeviceBusy, DeviceGone, NoReply, NotFound
from cx68app.layout import HID_TO_LINUX, load_keys

from . import mapping as M
from . import uinput as U
from .access import Watcher, write_state

EVIOCSCLOCKID = U._IOW("E", 0xA0, 4)
CLOCK_MONOTONIC = 1
VENDOR, PRODUCT = "3151", "502d"


def read_layout(dev):
    """{legend: slot} and {legend: Linux keycode} for layer 0 of the active profile."""
    profile = dev.transact(*P.get_profile())[1]
    entries = P.keymap_entries([dev.transact(*P.get_keymap_page(profile, 0, pg)) for pg in range(P.KEYMAP_PAGES)])
    keys = [k for k in load_keys(entries) if k.slot is not None]
    slots = {k.name: k.slot for k in keys}
    codes = {k.name: HID_TO_LINUX.get(k.usage) for k in keys}
    for alias, legend in M.KEY_ALIASES.items():
        if legend in slots:
            slots[alias], codes[alias] = slots[legend], codes[legend]
    return slots, codes


def load_calibration(path=M.CALIBRATION_FILE):
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return M.Calibration()
    return M.Calibration({int(k): v for k, v in raw.get("rest", {}).items()},
                         {int(k): v for k, v in raw.get("bottom", {}).items()})


def open_pad(cfg: M.Config):
    ident = cfg.device_id
    return U.VirtualDevice(ident["name"], ident["vendor"], ident["product"], ident["version"], ident["bustype"],
                           cfg.all_axes(), cfg.all_buttons())


class Bridge:
    """Runs until stop is set. The pad exists, and the keyboard is held, only while
    access.allowed is set (see access.py); otherwise both are released."""

    def __init__(self, cfg: M.Config, log=print, always=False):
        self.cfg = cfg
        self.log = log
        self.pad = None
        self.suppressor = None
        self.state = {}
        self.stop = threading.Event()
        self.access = Watcher(log=log, force_always=always)
        self.on_frame = None          # measure hook: (frame, depth, sync time or None)

    def _send(self, values):
        changed = False
        for (typ, code), v in values.items():
            if self.state.get((typ, code)) != v:
                self.pad.emit(typ, code, v)
                self.state[(typ, code)] = v
                changed = True
        return self.pad.sync() if changed else None

    def _live(self):
        return not self.stop.is_set() and self.access.allowed.is_set()

    def run(self):
        try:
            while not self.stop.is_set():
                write_state("waiting", ", ".join(self.access.policy.programs))
                while not self.stop.is_set() and not self.access.allowed.wait(0.5):
                    pass
                if self._live():
                    self._active()
        finally:
            self.access.close()
            write_state("stopped")

    def _active(self):
        self.pad = open_pad(self.cfg)
        self.log(f"virtual pad: {self.pad.name} ({self.cfg.style}) at {U.find_event_node(self.pad.name)}")
        write_state("active", ", ".join(self.access.matched))
        try:
            while self._live():
                try:
                    with Device() as dev:
                        self._session(dev)
                except DeviceBusy as err:
                    self.log(f"keyboard busy ({err}); close cx68app or the vendor page. Retrying…")
                    self.stop.wait(2)
                except NotFound:
                    self.stop.wait(1)
                except (DeviceGone, NoReply, OSError) as err:
                    self.log(f"keyboard disconnected ({err}); waiting…")
                    self.stop.wait(0.5)
                except CX68Error as err:
                    self.log(f"error: {err}")
                    self.stop.wait(2)
                finally:
                    if self.state:
                        self._send({k: 0 for k in self.state})     # never leave the throttle stuck on
        finally:
            if self.suppressor:
                self.suppressor.close()
                self.suppressor = None
            self.pad.close()
            self.pad, self.state = None, {}

    def _session(self, dev):
        slots, codes = read_layout(dev)
        mapper = M.Mapper(self.cfg, slots, load_calibration())
        unit = P.travel_unit_mm(dev.firmware)
        self.log(f"keyboard connected (firmware 0x{dev.firmware:04x}); mapping "
                 + ", ".join(sorted(f"{k}→slot {slots[k]}" for k in self.cfg.keys())))
        if self.cfg.suppress_mapped and self.suppressor is None:
            self.suppressor = Suppressor({codes[k] for k in self.cfg.keys() if codes.get(k)}, self.log)
        self._send(mapper.neutral())
        allowed = self.access.allowed
        while not self.stop.is_set() and allowed.is_set():
            fr = live.read_frame(dev)
            depth = [v * unit for v in fr.travel]
            t = self._send(mapper(depth))
            if self.on_frame:
                self.on_frame(fr, depth, t)


class Suppressor:
    """Grab the keyboard's own input nodes and pass everything through except the mapped keys,
    so pressing W drives the throttle without also typing w. Released when the bridge exits."""

    def __init__(self, suppressed: set, log=print):
        self.suppressed = suppressed
        self.fds = []
        nodes = keyboard_nodes()
        if not nodes:
            raise RuntimeError("no CX68 keyboard input nodes found")
        keybits = set()
        try:
            for node, bits in nodes:
                self.fds.append(os.open(node, os.O_RDONLY | os.O_NONBLOCK))
                keybits |= bits
        except PermissionError as err:
            for fd in self.fds:
                os.close(fd)
            raise RuntimeError(f"cannot read {err.filename}: install the optional rule in udev/70-cx68.rules "
                               f"to use [suppress] mapped_keys") from None
        self.out = U.VirtualDevice("CX68 passthrough keyboard", 0x3151, 0xC68B, 1, U.BUS_VIRTUAL, [],
                                   sorted(keybits))
        _wait_all_released(self.fds)
        for fd in self.fds:
            fcntl.ioctl(fd, U.EVIOCGRAB, 1)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._forward, name="cx68-suppress", daemon=True)
        self.thread.start()
        log(f"suppressing keycodes {sorted(suppressed)} from the keyboard; everything else passes through")

    def _forward(self):
        while not self._stop.is_set():
            ready, _, _ = select.select(self.fds, [], [], 0.2)
            for fd in ready:
                sent = False
                for _t, typ, code, value in U.read_events(fd):
                    if typ == U.EV_KEY and code not in self.suppressed:
                        self.out.emit(typ, code, value)
                        sent = True
                if sent:
                    self.out.sync()

    def close(self):
        self._stop.set()
        self.thread.join(1)
        for fd in self.fds:
            try:
                fcntl.ioctl(fd, U.EVIOCGRAB, 0)
            finally:
                os.close(fd)
        self.out.close()


def keyboard_nodes():
    """[(/dev/input/eventN, {keycodes})] for the CX68's key-producing input nodes."""
    out = []
    for entry in sorted(os.listdir("/sys/class/input")):
        base = f"/sys/class/input/{entry}/device"
        try:
            with open(f"{base}/id/vendor") as v, open(f"{base}/id/product") as p:
                if (v.read().strip(), p.read().strip()) != (VENDOR, PRODUCT) or not entry.startswith("event"):
                    continue
            with open(f"{base}/capabilities/key") as f:
                words = f.read().split()
        except OSError:
            continue
        mask = int("".join(w.rjust(16, "0") for w in words), 16) if words else 0
        bits = {i for i in range(mask.bit_length()) if mask >> i & 1}
        if bits & set(range(1, 128)):       # letters, digits, modifiers: a real keyboard node
            out.append((f"/dev/input/{entry}", bits))
    return out


def _wait_all_released(fds, timeout=3.0):
    """Grabbing while a key is held would leave it stuck down in the session; wait it out."""
    EVIOCGKEY = U._IOC(2, "E", 0x18, 96)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(any(fcntl.ioctl(fd, EVIOCGKEY, bytes(96))) for fd in fds):
            return
        time.sleep(0.02)


# ------------------------------------------------------------------------------- calibration
def calibrate(seconds_rest=2.0, log=print, ask=input):
    """Measure each mapped key's resting reading and its full press."""
    cfg = M.load()
    with Device() as dev:
        slots, _ = read_layout(dev)
        unit = P.travel_unit_mm(dev.firmware)
        keys = sorted(cfg.keys())
        log(f"Hands off the keyboard for {seconds_rest:.0f} s…")
        rest = {slots[k]: 0.0 for k in keys}
        end = time.monotonic() + seconds_rest
        while time.monotonic() < end:
            fr = live.read_frame(dev)
            for s in rest:
                rest[s] = max(rest[s], fr.travel[s] * unit)
        bottom = {}
        for k in keys:
            ask(f"Press {k} all the way down and hold it, then press Enter (with the other hand)… ")
            s = slots[k]
            frames = [live.read_frame(dev).travel[s] * unit for _ in range(200)]
            bottom[s] = statistics.median(frames)
            log(f"  {k}: rest {rest[s]:.2f} mm, full press {bottom[s]:.2f} mm")
    M.CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    M.CALIBRATION_FILE.write_text(json.dumps({"rest": rest, "bottom": bottom}, indent=1))
    log(f"saved {M.CALIBRATION_FILE}")


# ------------------------------------------------------------------------------- latency
def measure(seconds=20.0, log=print):
    """Run the bridge and report where the time goes, per report sent to the pad:
    sample age (frame read), frame end -> event in the kernel, and (if the keyboard's own
    input node is readable) bridge vs the keyboard's native key events."""
    cfg = M.load()
    bridge = Bridge(cfg, log=log, always=True)
    stats = {"frame_ms": [], "emit_ms": [], "proc_ms": [], "periods": [], "native": []}
    native, crossed, armed = {}, {}, set()   # keycode -> time of the native event / of crossing actuation
    pad_fd = None
    kb = []
    for node, _bits in keyboard_nodes():
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
            kb.append(fd)
        except OSError:
            pass
    codes = {}
    last_t = [None]

    def on_frame(fr, depth, t_sync):
        nonlocal pad_fd
        stats["proc_ms"].append((time.monotonic() - fr.t) * 1000)
        if pad_fd is None:
            node = U.find_event_node(bridge.pad.name, timeout=0)
            if node:
                try:
                    pad_fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
                except PermissionError:
                    return                    # udev has not granted access to the new node yet
                fcntl.ioctl(pad_fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
        if last_t[0] is not None:
            stats["periods"].append((fr.t - last_t[0]) * 1000)
        last_t[0] = fr.t
        stats["frame_ms"].append(fr.duration * 1000)
        if t_sync is not None and pad_fd is not None:
            for ts, typ, _code, _v in U.read_events(pad_fd):
                if typ == U.EV_SYN:
                    stats["emit_ms"].append((ts - fr.t) * 1000)
        # native comparison: first frame past the key's actuation vs the keyboard's own key event
        for fd in kb:
            for ts, typ, code, value in U.read_events(fd):
                if typ == U.EV_KEY and value == 1:
                    native[code] = ts
        for code, act, s in codes.values():
            if depth[s] >= act and code not in armed:
                armed.add(code)
                crossed[code] = fr.t
            elif depth[s] < act * 0.5:
                armed.discard(code)
            if code in crossed and code in native:
                if abs(crossed[code] - native[code]) < 0.1:
                    stats["native"].append((crossed[code] - native[code]) * 1000)
                del crossed[code], native[code]

    if kb:
        with Device() as dev:
            slots, linux = read_layout(dev)
            acts = P.u16_slots([dev.transact(*P.get_magnetism_page(P.Param.ACTUATION, pg)) for pg in range(P.U16_PAGES)])
            unit = P.travel_unit_mm(dev.firmware)
        codes = {k: (linux[k], acts[slots[k]] * unit, slots[k]) for k in cfg.keys() if linux.get(k)}
    bridge.on_frame = on_frame
    timer = threading.Timer(seconds, bridge.stop.set)
    timer.start()
    log(f"measuring for {seconds:.0f} s: press the mapped keys ({', '.join(sorted(cfg.keys()))}) now and then…")
    bridge.run()

    def pct(v, q):
        return sorted(v)[min(len(v) - 1, int(len(v) * q))] if v else float("nan")

    per = stats["periods"]
    log(f"frames: {len(per) + 1}, {1000 / statistics.mean(per):.0f}/s; frame period p50 {pct(per, .5):.2f} ms, "
        f"p99 {pct(per, .99):.2f} ms")
    log(f"sample age (one frame read): p50 {pct(stats['frame_ms'], .5):.2f} ms")
    pr = stats["proc_ms"]
    log(f"frame end -> mapped and written (every frame): p50 {pct(pr, .5) * 1000:.0f} µs, p99 {pct(pr, .99) * 1000:.0f} µs")
    e = stats["emit_ms"]
    log(f"frame end -> event in kernel: {len(e)} reports, p50 {pct(e, .5) * 1000:.0f} µs, p99 {pct(e, .99) * 1000:.0f} µs")
    n = stats["native"]
    if n:
        log(f"bridge vs native key events ({len(n)} presses): p50 {pct(n, .5):+.2f} ms, p99 {pct(n, .99):+.2f} ms "
            f"(positive = the bridge saw the press later than the keyboard's own key event)")
    elif not kb:
        log("native comparison skipped: the keyboard's input nodes are not readable (optional udev rule)")
    for fd in kb + ([pad_fd] if pad_fd else []):
        os.close(fd)
