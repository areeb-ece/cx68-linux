"""Keys tab: per-key actuation, release, rapid trigger and deadzone for the active profile."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from cx68 import settings

from .keyboard_view import KeyboardView
from .widgets import backup_note, guarded

FIELDS = [  # name, label, step
    ("actuation", "Actuation point", 0.01),
    ("release", "Release point", 0.01),
    ("rt_press", "RT press sensitivity", 0.01),
    ("rt_release", "RT release sensitivity", 0.01),
    ("deadzone", "Bottom deadzone", 0.01),
]


class Field(QDoubleSpinBox):
    """mm spin box that remembers whether the user changed it, and can show 'mixed'."""

    def __init__(self, name, step):
        super().__init__()
        lo, hi = settings.LIMITS_MM[name]
        self.setRange(lo, hi)
        self.setSingleStep(step)
        self.setDecimals(2)
        self.setSuffix(" mm")
        self.setKeyboardTracking(False)
        self.edited = False
        self.valueChanged.connect(lambda _v: setattr(self, "edited", True))

    def load(self, values):
        self.blockSignals(True)
        self.setValue(values[0] if values else self.minimum())
        self.setToolTip("selected keys differ; changing this sets them all" if len(set(values)) > 1 else "")
        self.setProperty("mixed", len(set(values)) > 1)
        self.setPrefix("mixed · " if len(set(values)) > 1 else "")
        self.blockSignals(False)
        self.edited = False


class KeysTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.snap = None
        self.view = KeyboardView(selectable=True)
        self.view.selection_changed.connect(self._on_selection)

        sel_row = QHBoxLayout()
        for label, fn in (("All", self._all), ("None", lambda: self.view.set_selected(set())), ("Invert", self._invert)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            sel_row.addWidget(b)
        self.count = QLabel("click keys to select them", objectName="muted")
        sel_row.addWidget(self.count, 1)

        left = QVBoxLayout()
        head = QVBoxLayout()
        head.setSpacing(0)
        head.addWidget(QLabel("Keys", objectName="heading"))
        head.addWidget(QLabel("Per-key travel settings for the active profile. Live travel stays visible while you edit.",
                              objectName="muted"))
        left.addLayout(head)
        left.addWidget(self.view, 1)
        left.addLayout(sel_row)

        panel = QFrame(objectName="card")
        panel.setFixedWidth(330)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setVerticalSpacing(10)
        self.fields = {}
        for name, label, step in FIELDS:
            f = Field(name, step)
            self.fields[name] = f
            if name == "rt_press":
                self.rt = QCheckBox("Rapid trigger")
                self.rt.setTristate(False)
                self.rt.stateChanged.connect(self._rt_changed)
                self.rt_edited = False
                form.addRow(self.rt)
            form.addRow(label, f)
        self.apply_btn = QPushButton("Apply", objectName="primary")
        self.apply_btn.clicked.connect(self.apply)
        discard = QPushButton("Discard changes")
        discard.clicked.connect(lambda: self._on_selection(self.view.selected))
        self.result = QLabel("", objectName="muted")
        self.result.setWordWrap(True)
        note = QLabel("Only the fields you change are written, to the selected keys only. The keyboard is read back "
                      "afterwards to verify. A full backup is saved before the first change of the session.",
                      objectName="muted")
        note.setWordWrap(True)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(18, 16, 18, 16)
        pv.addWidget(QLabel("TRIGGER", objectName="cardTitle"))
        pv.addLayout(form)
        pv.addSpacing(6)
        pv.addWidget(self.apply_btn)
        pv.addWidget(discard)
        pv.addWidget(self.result)
        pv.addStretch(1)
        pv.addWidget(note)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 14)
        lay.setSpacing(16)
        lay.addLayout(left, 1)
        lay.addWidget(panel)
        worker.connected.connect(self.on_snapshot)
        worker.frame.connect(self.view.set_frame)
        worker.job_done.connect(self._done)
        self._enable(False)

    # data ------------------------------------------------------------------------
    def on_snapshot(self, sn):
        from .layout import load_keys
        self.snap = sn
        self.view.set_keys(load_keys(sn.keymap))
        self.view.set_snapshot(sn)
        self._on_selection(self.view.selected)

    def _slots(self):
        return sorted(k.slot for k in self.view.keys if k.slot is not None)

    def _all(self):
        self.view.set_selected(self._slots())

    def _invert(self):
        self.view.set_selected(set(self._slots()) - self.view.selected)

    def _enable(self, on):
        for f in self.fields.values():
            f.setEnabled(on)
        self.rt.setEnabled(on)
        self.apply_btn.setEnabled(on)

    def _on_selection(self, slots):
        slots = sorted(slots)
        self.count.setText(f"{len(slots)} key{'s' if len(slots) != 1 else ''} selected" if slots
                           else "click keys to select them")
        self._enable(bool(slots) and self.snap is not None)
        if not slots or self.snap is None:
            return
        sn = self.snap
        for name, f in self.fields.items():
            f.load([round(getattr(sn, name)[s], 2) for s in slots])
        rts = {bool(sn.mode_bytes[s] & 0x80) for s in slots}
        self.rt.blockSignals(True)
        self.rt.setText("Rapid trigger" + (" (mixed)" if len(rts) > 1 else ""))
        self.rt.setChecked(rts == {True})
        self.rt.blockSignals(False)
        self.rt_edited = False
        self._rt_fields()
        self.apply_btn.setText(f"Apply to {len(slots)} key{'s' if len(slots) != 1 else ''}")

    def _rt_changed(self, _state):
        self.rt_edited = True
        self.rt.setText("Rapid trigger")
        self._rt_fields()

    def _rt_fields(self):
        for name in ("rt_press", "rt_release"):
            self.fields[name].setEnabled(self.rt.isEnabled() and self.rt.isChecked())

    # writing ---------------------------------------------------------------------
    def apply(self):
        slots = sorted(self.view.selected)
        changes = {name: f.value() for name, f in self.fields.items() if f.edited}
        if self.rt_edited:
            changes["rapid_trigger"] = self.rt.isChecked()
        if not changes:
            self.result.setText("nothing changed")
            return
        self.apply_btn.setEnabled(False)
        self.result.setText("writing…")
        self.worker.submit("keys-apply", guarded(lambda dev: settings.apply(dev, slots, **changes)),
                           allow_writes=settings.WRITES)

    def _done(self, token, result, error):
        if token != "keys-apply":
            return
        self.apply_btn.setEnabled(True)
        if error:
            self.result.setText(f"failed: {error}")
            QMessageBox.warning(self, "Write failed", f"{error}\n\nYour last backup is in the backups folder; "
                                                      f"use Backup → Restore to go back.")
            return
        backup, written = result
        self.result.setText((("written and verified: " + ", ".join(written)) if written else "already set, nothing written")
                            + backup_note(backup))
