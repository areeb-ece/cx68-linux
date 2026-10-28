"""Small shared pieces for the configuration pages."""
import time
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from cx68 import config, keymap

ROOT = Path(__file__).resolve().parents[1]
_session_backup = None


def card(title):
    frame = QFrame(objectName="card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    lay.addWidget(QLabel(title, objectName="cardTitle"))
    return frame, lay


def muted(text=""):
    label = QLabel(text, objectName="muted")
    label.setWordWrap(True)
    return label


def heading(title, subtitle):
    box = QVBoxLayout()
    box.setSpacing(0)
    box.addWidget(QLabel(title, objectName="heading"))
    box.addWidget(muted(subtitle))
    return box


def guarded(fn):
    """Wrap a write job: before the session's first write, save a full backup.
    The job then returns (backup path or None, fn's result)."""
    def job(dev):
        global _session_backup
        made = None
        if _session_backup is None:
            path = ROOT / "backups" / time.strftime("cx68-auto-%Y%m%d-%H%M%S.json")
            path.parent.mkdir(exist_ok=True)
            config.save(config.read_full_config(dev), path)
            _session_backup = made = path
        return made, fn(dev)
    return job


def backup_note(made):
    return f"\nbackup first: {made.name}" if made else ""


class KeyPicker(QWidget):
    """Pick an ordinary key action: optional modifier + key, or Disabled."""
    changed = Signal()

    def __init__(self, allow_disabled=True):
        super().__init__()
        self.allow_disabled = allow_disabled
        self.mod = QComboBox()
        self.mod.addItem("—", None)
        for name in keymap.MODIFIERS:
            self.mod.addItem(f"{name} +", name)
        self.group = QComboBox()
        if allow_disabled:
            self.group.addItem("Disabled", None)
        for g in keymap.GROUPS:
            self.group.addItem(g, g)
        self.key = QComboBox()
        self.group.currentIndexChanged.connect(self._fill)
        for w in (self.mod, self.key):
            w.currentIndexChanged.connect(lambda _i: self.changed.emit())
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.mod, 0, 0)
        lay.addWidget(self.group, 0, 1)
        lay.addWidget(self.key, 1, 0, 1, 2)
        self._fill()

    def _fill(self, *_):
        g = self.group.currentData()
        self.key.blockSignals(True)
        self.key.clear()
        for usage, name in (keymap.GROUPS[g].items() if g else ()):
            self.key.addItem(name, usage)
        self.key.blockSignals(False)
        self.key.setEnabled(g is not None)
        self.mod.setEnabled(g is not None)
        self.changed.emit()

    def entry(self) -> bytes:
        usage = self.key.currentData()
        if self.group.currentData() is None or usage is None:
            return keymap.DISABLED
        return keymap.entry(usage, self.mod.currentData())

    def set_entry(self, e: bytes):
        """Show an entry; returns False if it isn't an ordinary key (left unchanged)."""
        if e == keymap.DISABLED and self.allow_disabled:
            self.group.setCurrentIndex(0)
            return True
        if not keymap.editable(e) or e[3]:
            return False
        group = next((g for g, keys in keymap.GROUPS.items() if e[2] in keys), None)
        if group is None:
            return False
        self.group.setCurrentIndex(self.group.findData(group))
        self.key.setCurrentIndex(self.key.findData(e[2]))
        mod = next((m for m, u in keymap.MODIFIERS.items() if u == e[1]), None)
        self.mod.setCurrentIndex(self.mod.findData(mod) if mod else 0)
        return True
