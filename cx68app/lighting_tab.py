"""Lighting tab: backlight effect, colour, speed and brightness, previewed before writing."""
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QFormLayout, QHBoxLayout, QMessageBox, QPushButton,
                               QSlider, QVBoxLayout, QWidget)

from cx68 import lighting

from .keyboard_view import KeyboardView
from .layout import load_keys
from .widgets import backup_note, card, guarded, heading, muted

SWATCHES = [0xFF0000, 0xFF8000, 0xFFFF00, 0x00FF00, 0x00FFFF, 0x5AA9FF, 0x0000FF, 0xFF00FF, 0xFFFFFF]


def slider(maximum):
    s = QSlider(Qt.Horizontal)
    s.setRange(0, maximum)
    s.setPageStep(1)
    s.setTickPosition(QSlider.TicksBelow)
    return s


class LightingTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.snap = None
        self.current = None        # what the keyboard has
        self.view = KeyboardView()

        side, sl = card("EFFECT")
        self.mode = QComboBox()
        for m in lighting.MODE_SPEC:
            self.mode.addItem(lighting.MODES[m], m)
        self.variant = QComboBox()
        self.rainbow = QCheckBox("Rainbow")
        self.colour = QPushButton()
        self.colour.clicked.connect(self._pick_colour)
        self._rgb = 0xFFFFFF
        swatches = QHBoxLayout()
        swatches.setSpacing(6)
        for rgb in SWATCHES:
            b = QPushButton()
            b.setFixedSize(22, 22)
            b.setStyleSheet(f"background:#{rgb:06x}; border-radius:11px; border:1px solid rgba(255,255,255,40);")
            b.clicked.connect(lambda _c=False, v=rgb: self._set_rgb(v))
            swatches.addWidget(b)
        swatches.addStretch(1)
        self.speed = slider(lighting.MAXSPEED)
        self.brightness = slider(lighting.MAXBRIGHT)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("Mode", self.mode)
        form.addRow("Direction", self.variant)
        form.addRow("Colour", self.colour)
        form.addRow("", swatches)
        form.addRow("", self.rainbow)
        form.addRow("Speed", self.speed)
        form.addRow("Brightness", self.brightness)
        self.apply_btn = QPushButton("Apply", objectName="primary")
        self.apply_btn.clicked.connect(self._apply)
        revert = QPushButton("Revert preview")
        revert.clicked.connect(lambda: self._load(self.current))
        row = QHBoxLayout()
        row.addWidget(self.apply_btn)
        row.addWidget(revert)
        self.result = muted("")
        sl.addLayout(form)
        sl.addLayout(row)
        sl.addWidget(self.result)
        sl.addStretch(1)
        sl.addWidget(muted("The board shows a preview of the effect; the keyboard only changes when you press "
                           "Apply. Brightness 0 or mode Off turns the backlight off. Custom pictures and music "
                           "modes need the vendor software and aren't offered."))
        side.setFixedWidth(360)

        for w in (self.mode, self.variant):
            w.currentIndexChanged.connect(self._changed)
        self.rainbow.toggled.connect(self._changed)
        for w in (self.speed, self.brightness):
            w.valueChanged.connect(self._changed)
        self.mode.currentIndexChanged.connect(self._mode_changed)

        left = QVBoxLayout()
        left.addLayout(heading("Lighting", "Backlight effect of the keyboard."))
        left.addWidget(self.view, 1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 14)
        lay.setSpacing(16)
        lay.addLayout(left, 1)
        lay.addWidget(side)
        worker.connected.connect(self.on_snapshot)
        worker.frame.connect(self.view.set_frame)
        worker.job_done.connect(self._done)
        self.setEnabled(False)

    def on_snapshot(self, sn):
        self.snap = sn
        self.view.set_keys(load_keys(sn.keymap))
        self.current = sn.backlight
        self.setEnabled(sn.backlight is not None)
        self._load(sn.backlight)

    # editing -----------------------------------------------------------------------
    def _load(self, bl):
        if bl is None:
            return
        widgets = (self.mode, self.variant, self.rainbow, self.speed, self.brightness)
        for w in widgets:
            w.blockSignals(True)
        idx = self.mode.findData(bl.mode)
        self.mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._fill_variants(bl.mode)
        v = self.variant.findData(bl.option)
        self.variant.setCurrentIndex(max(v, 0))
        self.rainbow.setChecked(bl.rainbow)
        self.speed.setValue(bl.speed)
        self.brightness.setValue(min(bl.brightness, lighting.MAXBRIGHT))
        self._rgb = bl.rgb
        for w in widgets:
            w.blockSignals(False)
        if idx < 0:
            self.result.setText(f"The keyboard is set to {bl.name}, which can't be edited here.")
        self._changed()

    def _fill_variants(self, mode):
        self.variant.clear()
        for name, val in (lighting.MODE_SPEC.get(mode, (0, 0, None))[2] or {}).items():
            self.variant.addItem(name, val)

    def _mode_changed(self, *_):
        self.variant.blockSignals(True)
        self._fill_variants(self.mode.currentData())
        self.variant.blockSignals(False)
        self._changed()

    def _pick_colour(self):
        c = QColorDialog.getColor(QColor(f"#{self._rgb:06x}"), self, "Backlight colour")
        if c.isValid():
            self._set_rgb(int(c.name()[1:], 16))

    def _set_rgb(self, rgb):
        self._rgb = rgb
        self.rainbow.setChecked(False)
        self._changed()

    def setting(self) -> lighting.Backlight:
        return lighting.normalised(lighting.Backlight(
            self.mode.currentData(), self.speed.value(), self.brightness.value(),
            self.variant.currentData() or 0, self.rainbow.isChecked(), self._rgb))

    def _changed(self, *_):
        speed_used, colour_used, variants = lighting.MODE_SPEC[self.mode.currentData()]
        self.variant.setEnabled(bool(variants))
        self.speed.setEnabled(speed_used)
        self.rainbow.setEnabled(colour_used)
        self.colour.setEnabled(colour_used and not self.rainbow.isChecked())
        self.brightness.setEnabled(self.mode.currentData() != 0)
        c = QColor(f"#{self._rgb:06x}")
        self.colour.setText(f"#{self._rgb:06X}")
        self.colour.setStyleSheet(f"background: {c.name()}; color: {'#0e1014' if c.lightnessF() > 0.55 else '#ffffff'};"
                                  f"border-color: {c.name()};" if self.colour.isEnabled() else "")
        bl = self.setting()
        if self.snap is not None:
            self.view.set_snapshot(replace(self.snap, backlight=bl))     # preview
        cur = self.current
        same = cur is not None and cur.mode in lighting.MODE_SPEC and lighting.encode(bl).msg == lighting.encode(cur).msg
        self.apply_btn.setEnabled(not same)

    def _apply(self):
        bl = self.setting()
        self.apply_btn.setEnabled(False)
        self.result.setText("writing…")
        self.worker.submit("lighting-apply", guarded(lambda dev: lighting.apply(dev, bl)),
                           allow_writes=lighting.WRITES)

    def _done(self, token, result, error):
        if token != "lighting-apply":
            return
        if error:
            self.result.setText(f"failed: {error}")
            self.apply_btn.setEnabled(True)
            QMessageBox.warning(self, "Write failed", f"{error}\n\nUse Backup → Restore to go back.")
            return
        made, bl = result
        self.result.setText(f"written and verified: {bl.describe()}" + backup_note(made))
