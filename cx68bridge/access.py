"""Which programs get the gamepad.

Linux grants device access per user, not per program, so a pad that exists can be read by
anything you run. The control that is enforceable is *when* it exists: in "allowlist" mode the
bridge creates the pad (and takes the keyboard) only while one of the listed programs runs, and
destroys it when the last one exits. Policy lives in ~/.config/cx68/access.json (edited by
cx68app's Gamepad tab) and is reloaded when it changes.
"""
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .mapping import CONFIG_DIR

ACCESS_FILE = CONFIG_DIR / "access.json"
STATE_FILE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "cx68-bridge.json"
DEFAULT_PROGRAMS = ["acs.exe", "AssettoCorsa.exe", "Content Manager.exe"]
SCAN_INTERVAL = 1.0


@dataclass
class Policy:
    mode: str = "allowlist"               # "allowlist" or "always"
    programs: list = field(default_factory=lambda: list(DEFAULT_PROGRAMS))


def load(path=ACCESS_FILE) -> Policy:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return Policy()
    mode = raw.get("mode", "allowlist")
    return Policy(mode if mode in ("allowlist", "always") else "allowlist",
                  [str(p) for p in raw.get("programs", DEFAULT_PROGRAMS) if str(p).strip()])


def save(policy: Policy, path=ACCESS_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mode": policy.mode, "programs": policy.programs}, indent=1))
    tmp.replace(path)


def program_name(cmdline: bytes, comm: str = "") -> str:
    """The program's file name: the last path part of argv[0], with / or \\ separators, so
    Windows programs under Wine/Proton show as e.g. 'acs.exe'."""
    argv0 = cmdline.split(b"\0", 1)[0].decode(errors="replace").strip()
    name = re.split(r"[\\/]", argv0)[-1] if argv0 else ""
    return name or comm.strip()


def running_programs():
    """{name: [pid, ...]} of processes owned by this user."""
    out = {}
    uid = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read()
            if not cmd:
                continue                      # kernel threads, zombies
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read()
        except OSError:
            continue
        name = program_name(cmd, comm)
        if name:
            out.setdefault(name, []).append(int(pid))
    return out


def matches(policy: Policy, running: dict) -> list:
    wanted = {p.lower() for p in policy.programs}
    return sorted(name for name in running if name.lower() in wanted)


class Watcher:
    """Background thread: sets `allowed` while the policy permits the pad."""

    def __init__(self, path=ACCESS_FILE, log=print, force_always=False):
        self.path = Path(path)
        self.log = log
        self.force_always = force_always
        self.allowed = threading.Event()
        self.matched = []
        self.policy = Policy("always", []) if force_always else load(self.path)
        self._mtime = None
        self._stop = threading.Event()
        self._check()
        self.thread = threading.Thread(target=self._loop, name="cx68-access", daemon=True)
        self.thread.start()

    def _check(self):
        if not self.force_always:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._mtime = mtime
                self.policy = load(self.path)
                self.log(f"access: {self.policy.mode}"
                         + (f", programs {self.policy.programs}" if self.policy.mode == "allowlist" else ""))
        if self.policy.mode == "always":
            self.matched = ["(any program)"]
            self.allowed.set()
            return
        matched = matches(self.policy, running_programs())
        if matched != self.matched:
            self.log(f"access: allowed program running: {', '.join(matched)}" if matched
                     else "access: no allowed program running; gamepad removed, keyboard released")
            self.matched = matched
        (self.allowed.set if matched else self.allowed.clear)()

    def _loop(self):
        while not self._stop.wait(SCAN_INTERVAL):
            self._check()

    def close(self):
        self._stop.set()


def write_state(state: str, detail=""):
    """For cx68app: what the bridge is doing right now."""
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": state, "detail": detail, "pid": os.getpid()}))
        tmp.replace(STATE_FILE)
    except OSError:
        pass


def read_state():
    try:
        st = json.loads(STATE_FILE.read_text())
        os.kill(st["pid"], 0)
        return st
    except (OSError, ValueError, KeyError):
        return None
