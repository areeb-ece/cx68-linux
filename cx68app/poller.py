"""Keyboard polling process.

Polling runs in its own process so rendering (which holds Python's GIL on the UI side) can
never slow it down. It owns the device, publishes every frame into shared memory
(FrameShare) and reports connection state, snapshots and stats over a pipe. UI jobs (backup,
writes) run in the UI process: the poller is asked to pause, closes the device, and reopens it
afterwards, re-reading the snapshot so the views see any change.

No Qt in here: the child process imports only this module, cx68 and keystate.
"""
import os
import signal
import statistics
import time
from dataclasses import dataclass, field

from cx68 import lighting, live
from cx68 import protocol as P
from cx68.transport import CX68Error, Device, DeviceBusy, DeviceGone, NoReply, NotFound

from .keystate import KeyStates

STATS_INTERVAL = 0.5


@dataclass
class Snapshot:
    """What the views need to know about the keyboard, read once per connection."""
    device_id: int
    firmware: int
    unit_mm: float
    profile: int
    keymap: list            # 128 x 4-byte entries, active profile layer 0
    actuation: list         # mm per slot
    release: list
    rt_press: list
    rt_release: list
    deadzone: list
    mode_bytes: list
    key_mode: dict = field(default_factory=dict)
    backlight: lighting.Backlight | None = None


def read_snapshot(dev: Device) -> Snapshot:
    unit = P.travel_unit_mm(dev.firmware)
    profile = dev.transact(*P.get_profile())[1]
    keymap = P.keymap_entries([dev.transact(*P.get_keymap_page(profile, 0, pg)) for pg in range(P.KEYMAP_PAGES)])

    def mm(param):
        return [v * unit for v in P.u16_slots([dev.transact(*P.get_magnetism_page(param, pg)) for pg in range(P.U16_PAGES)])]

    modes = P.u8_slots([dev.transact(*P.get_magnetism_page(P.Param.KEY_MODE, pg)) for pg in range(P.U8_PAGES)])
    backlight = lighting.parse(dev.transact(*P.get_led()))
    return Snapshot(dev.device_id, dev.firmware, unit, profile, keymap,
                    mm(P.Param.ACTUATION), mm(P.Param.RELEASE), mm(P.Param.RT_PRESS), mm(P.Param.RT_RELEASE),
                    mm(P.Param.DEADZONE), modes, P.parse_key_mode(dev.transact(*P.get_key_mode())), backlight)


def key_states_for(snap: Snapshot) -> KeyStates:
    ks = KeyStates(P.SLOTS)
    ks.configure(snap.actuation, snap.release, snap.rt_press, snap.rt_release, [bool(m & 0x80) for m in snap.mode_bytes])
    return ks


class FrameShare:
    """The latest frame in shared memory. The UI takes it at its own rate; peak is the deepest
    travel since the UI's previous take, so short taps between two UI frames are never lost."""

    def __init__(self, ctx):
        self.lock = ctx.Lock()
        self.seq = ctx.RawValue("Q", 0)
        self.consumed = ctx.RawValue("b", 1)
        self.depth = ctx.RawArray("f", P.SLOTS)
        self.peak = ctx.RawArray("f", P.SLOTS)
        self.states = ctx.RawArray("B", P.SLOTS)

    def publish(self, depth, peak, states):
        with self.lock:
            self.depth[:] = depth
            self.peak[:] = peak
            self.states[:] = states
            self.seq.value += 1

    def take(self):
        """(seq, depth, peak, states); the peak restarts from here."""
        with self.lock:
            out = self.seq.value, self.depth[:], self.peak[:], self.states[:]
            self.consumed.value = 1
        return out


class _Pause(Exception):
    pass


class _Stop(Exception):
    pass


class Poller:
    def __init__(self, conn, share: FrameShare):
        self.conn = conn
        self.share = share
        self.rate_cap = None
        self.parent = os.getppid()

    def send(self, *msg):
        self.conn.send(msg)

    def _commands(self):
        while self.conn.poll():
            msg = self.conn.recv()
            if msg[0] == "rate_cap":
                self.rate_cap = msg[1]
            elif msg[0] == "pause":
                raise _Pause
            elif msg[0] == "stop":
                raise _Stop

    def _sleep(self, seconds):
        """Sleep, but wake for commands."""
        if self.conn.poll(seconds):
            self._commands()

    def run(self):
        try:
            while True:
                try:
                    try:
                        with Device() as dev:
                            self._session(dev)
                    except DeviceBusy as err:
                        self.send("status", f"busy: {err}")
                        self._sleep(2)
                    except NotFound:
                        self.send("status", "keyboard not connected - waiting…")
                        self._sleep(1)
                    except (DeviceGone, NoReply, OSError) as err:
                        self.send("status", f"disconnected ({err}) - reconnecting…")
                        self._sleep(0.5)
                    except CX68Error as err:
                        self.send("status", f"error: {err}")
                        self._sleep(2)
                except _Pause:
                    self.send("released")
                    while (msg := self.conn.recv())[0] != "resume":
                        if msg[0] == "rate_cap":
                            self.rate_cap = msg[1]
                        elif msg[0] == "stop":
                            raise _Stop
        except (_Stop, EOFError, BrokenPipeError):
            return

    def _session(self, dev):
        snap = read_snapshot(dev)
        self.send("connected", snap)
        self.send("status", f"connected: device {snap.device_id}, firmware 0x{snap.firmware:04x}, profile {snap.profile + 1}")
        states = key_states_for(snap)
        unit = snap.unit_mm
        share = self.share
        peak = None
        durations, frames, window_start = [], 0, time.monotonic()
        while True:
            self._commands()
            fr = live.read_frame(dev)
            depth = [v * unit for v in fr.travel]
            st = states.update(depth)
            if peak is None or share.consumed.value:
                peak = depth
                share.consumed.value = 0
            else:
                peak = list(map(max, peak, depth))
            share.publish(depth, peak, st)
            frames += 1
            durations.append(fr.duration)
            if fr.t - window_start >= STATS_INTERVAL:
                durations.sort()
                self.send("stats", {"fps": frames / (fr.t - window_start),
                                    "median_ms": statistics.median(durations) * 1000,
                                    "p99_ms": durations[int(len(durations) * 0.99)] * 1000})
                durations, frames, window_start = [], 0, fr.t
                if os.getppid() != self.parent:     # the app died without stopping us
                    raise _Stop
            if self.rate_cap:
                rest = 1 / self.rate_cap - (time.monotonic() - fr.t) - fr.duration
                if rest > 0:
                    self._sleep(rest)


def main(conn, share):
    signal.signal(signal.SIGINT, signal.SIG_IGN)    # Ctrl+C goes to the app, which stops us cleanly
    try:
        os.nice(-5)                                 # only works with CAP_SYS_NICE; harmless otherwise
    except OSError:
        pass
    Poller(conn, share).run()
