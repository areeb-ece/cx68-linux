"""UI side of the keyboard connection.

The device is owned by the polling process (poller.py). This module starts it, relays its
messages as Qt signals from a listener thread, and pumps the latest shared-memory frame to the
views at up to TARGET_FPS from a precise timer on the UI thread. Jobs submitted by the UI
(backup, restore, writes) run in the listener thread while the poller is paused.
"""
import multiprocessing as mp
import queue

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal

from cx68 import policy
from cx68.transport import Device

from . import poller
from .poller import Snapshot, key_states_for, read_snapshot

TARGET_FPS = 240


class DeviceWorker(QObject):
    connected = Signal(object)          # Snapshot
    status = Signal(str)                # human-readable connection state
    stats = Signal(object)              # dict: fps, median_ms, p99_ms (polling)
    job_done = Signal(object, object, object)   # (token, result, error)

    def __init__(self):
        super().__init__()
        ctx = mp.get_context("spawn")
        self.share = poller.FrameShare(ctx)
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=poller.main, args=(child, self.share), name="cx68-poller", daemon=True)
        self.outbox = queue.Queue()     # ("cmd", msg) or ("job", token, fn, kwargs); only the listener uses the pipe
        self._rate_cap = None
        self._stop = False

    # called from the UI thread ------------------------------------------------
    @property
    def rate_cap(self):
        return self._rate_cap

    @rate_cap.setter
    def rate_cap(self, hz):
        """Polling rate limit in frames/s, None = as fast as the keyboard answers."""
        self._rate_cap = hz
        self.outbox.put(("cmd", ("rate_cap", hz)))

    def submit(self, token, fn, **device_kwargs):
        """Run fn(dev) with the poller paused. device_kwargs (e.g. allow_writes) grant those
        permissions for just this job."""
        self.outbox.put(("job", token, fn, device_kwargs))

    @property
    def frame(self):
        """Signal (depth_mm list, peak_mm list, keystate list), emitted on the UI thread."""
        return self.pump.frame

    def stop(self):
        self._stop = True

    # listener thread ------------------------------------------------------------
    def run(self):
        self.proc.start()
        try:
            while not self._stop:
                if self.conn.poll(0.02):
                    self._handle(self.conn.recv())
                self._drain()
        except (EOFError, BrokenPipeError):
            self.status.emit("polling process stopped")
        finally:
            try:
                self.conn.send(("stop",))
            except (BrokenPipeError, OSError):
                pass
            self.proc.join(1.0)
            if self.proc.is_alive():
                self.proc.terminate()

    def _handle(self, msg):
        kind = msg[0]
        if kind == "connected":
            self.connected.emit(msg[1])
        elif kind == "status":
            self.status.emit(msg[1])
        elif kind == "stats":
            self.stats.emit(msg[1])

    def _drain(self):
        while True:
            try:
                item = self.outbox.get_nowait()
            except queue.Empty:
                return
            if item[0] == "cmd":
                self.conn.send(item[1])
            else:
                self._run_job(*item[1:])

    def _run_job(self, token, fn, kwargs):
        self.conn.send(("pause",))
        while (msg := self.conn.recv())[0] != "released":
            self._handle(msg)
        try:
            with Device() as dev:
                if kwargs:
                    dev.allow = (dev.allow | policy.validate_allow(kwargs.get("allow", ()))
                                 | policy.validate_allow_writes(kwargs.get("allow_writes", ())))
                result = fn(dev)
            self.job_done.emit(token, result, None)
        except Exception as err:  # noqa: BLE001 - reported to the UI
            self.job_done.emit(token, None, err)
        finally:
            self.conn.send(("resume",))     # the poller reopens and re-reads the snapshot


class FramePump(QObject):
    """Lives on the UI thread: hands each new shared-memory frame to the views. The signal lives
    here (not on the worker, which belongs to the listener thread) so any slot runs on the UI thread."""
    frame = Signal(object, object, object)

    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self._seq = 0
        self.timer = QTimer(self, interval=max(1, 1000 // TARGET_FPS), timeout=self.tick)
        self.timer.setTimerType(Qt.PreciseTimer)

    def tick(self):
        seq, depth, peak, states = self.worker.share.take()
        if seq != self._seq:
            self._seq = seq
            self.frame.emit(depth, peak, states)


def start_worker():
    thread = QThread()
    worker = DeviceWorker()
    worker.pump = FramePump(worker)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    worker.pump.timer.start()
    return thread, worker


__all__ = ["DeviceWorker", "Snapshot", "key_states_for", "start_worker", "read_snapshot", "TARGET_FPS"]
