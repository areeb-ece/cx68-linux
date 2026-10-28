"""Live per-key travel: whole-board polling (read-only) and the 0x1B push stream.

Polling (E5 FE, 4 transactions per frame) needs no keyboard state change and measured
~774 frames/s. The stream (1B 01) sends only changes, ~1 kHz while keys move, but toggles
firmware state, so it must be explicitly allowed and is always switched off on exit.
"""
import time
from dataclasses import dataclass

from . import protocol as P
from .transport import Device, DeviceGone, NoReply, VendorReports


@dataclass
class Frame:
    t: float                 # time.monotonic() when the frame completed
    travel: list[int]        # raw units per slot (see protocol.travel_unit_mm)
    duration: float          # seconds the 4 transactions took


def read_frame(dev: Device) -> Frame:
    t0 = time.monotonic()
    pages = [dev.transact(*P.get_magnetism_page(P.Param.LIVE_TRAVEL, pg)) for pg in range(P.U16_PAGES)]
    t1 = time.monotonic()
    return Frame(t1, P.u16_slots(pages), t1 - t0)


def poll(dev: Device, reconnect=True, on_status=None):
    """Yield Frames as fast as the keyboard answers. Survives unplug/replug if reconnect."""
    status = on_status or (lambda s: None)
    while True:
        try:
            yield read_frame(dev)
        except (DeviceGone, NoReply) as err:
            if not reconnect:
                raise
            status(f"disconnected: {err}")
            dev.reconnect(timeout=float("inf"))
            status("reconnected")


class TravelStream:
    """Context manager for 0x1B travel notifications.

        with TravelStream(dev) as s:
            for t, slot, value in s.events(timeout=0.1): ...

    dev must allow 0x1B (Device(allow=[0x1B])) or be in write mode.
    """

    def __init__(self, dev: Device):
        self.dev = dev
        self.reports = None

    def __enter__(self):
        if self.dev.nodes.vendor is None:
            raise DeviceGone("vendor interface (1) not found")
        self.reports = VendorReports(self.dev.nodes.vendor)
        self.dev.transact(*P.set_travel_report(True))
        return self

    def __exit__(self, *exc):
        try:
            if self.dev.fd is not None:
                self.dev.transact(*P.set_travel_report(False))
        finally:
            self.reports.close()

    def events(self, timeout=0.1):
        """Yield (monotonic time, slot, raw travel) for every notification within timeout."""
        for report in self.reports.read(timeout):
            parsed = P.parse_travel_notification(report)
            if parsed:
                yield (time.monotonic(), *parsed)
