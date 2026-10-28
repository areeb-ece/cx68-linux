"""Advanced tab: snap tap, toggle and mod-tap per key on the active profile (DKS read-only)."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from cx68 import advanced, keymap

from . import theme
from .keyboard_view import KeyboardView
from .layout import load_keys
from .widgets import KeyPicker, backup_note, card, guarded, heading, muted

MARK = {"snap": QColor(theme.ACCENT), "mt": QColor("#f0b04f"), "tgl_hold": QColor("#7ddc9a"),
        "tgl_dots": QColor("#7ddc9a"), "dks": QColor(theme.MUTED)}
SHIFT = keymap.entry(0xE1)


class AdvancedTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.snap = None
        self.modes = None          # {slot: KeyMode} for keys that aren't normal
        self.names = {}
        self.view = KeyboardView(selectable=True, single=True)
        self.view.bar_when_selected = False
        self.view.selection_changed.connect(lambda _s: self._show_key())

        side, sl = card("KEY")
        self.key_name = QLabel("—", objectName="cardValue")
        self.now = muted("Click a key on the board.")
        self.mode = QComboBox()
        for opt in advanced.EDITABLE:
            self.mode.addItem(advanced.LABELS[opt], opt)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.pages = QStackedWidget()
        self.pages.addWidget(muted("A plain key, apart from its travel settings (Keys tab)."))
        # snap tap
        snap = QWidget()
        sf = QFormLayout(snap)
        sf.setContentsMargins(0, 0, 0, 0)
        self.partner = QComboBox()
        sf.addRow("Paired with", self.partner)
        sf.addRow(muted("While both keys are held, only the one pressed last counts (for example A and D "
                        "while strafing). Both keys of the pair are set."))
        self.pages.addWidget(snap)
        # toggle
        self.pages.addWidget(muted("Toggle: the key latches. Two firmware variants exist (the page calls them "
                                   "tgl_hold and tgl_dots); try which behaviour you prefer. The key sends its "
                                   "base-layer action."))
        # mod-tap
        mt = QWidget()
        mf = QFormLayout(mt)
        mf.setContentsMargins(0, 0, 0, 0)
        self.tap = KeyPicker(allow_disabled=False)
        self.hold = KeyPicker(allow_disabled=False)
        self.hold_ms = QSpinBox()
        self.hold_ms.setRange(advanced.HOLD_RANGE[0] * 10, advanced.HOLD_RANGE[1] * 10)
        self.hold_ms.setSingleStep(10)
        self.hold_ms.setSuffix(" ms")
        mf.addRow("Tap", self.tap)
        mf.addRow("Hold", self.hold)
        mf.addRow("Hold after", self.hold_ms)
        self.pages.addWidget(mt)
        self.page_of = {"normal": 0, "snap": 1, "tgl_hold": 2, "tgl_dots": 2, "mt": 3}

        self.apply_btn = QPushButton("Apply", objectName="primary")
        self.apply_btn.clicked.connect(self._apply)
        self.result = muted("")
        sl.addWidget(self.key_name)
        sl.addWidget(self.now)
        sl.addWidget(QLabel("MODE", objectName="cardTitle"))
        sl.addWidget(self.mode)
        sl.addWidget(self.pages)
        sl.addWidget(self.apply_btn)
        sl.addWidget(self.result)
        sl.addStretch(1)
        legend = " ".join(f'<span style="color:{MARK[o].name()}">■</span>&nbsp;{advanced.LABELS[o]}&nbsp;&nbsp;'
                          for o in ("snap", "mt", "tgl_hold", "dks"))
        lg = QLabel(legend.replace("Toggle (hold variant)", "Toggle"), objectName="muted")
        lg.setTextFormat(Qt.RichText)
        lg.setWordWrap(True)
        sl.addWidget(lg)
        sl.addWidget(muted("Rapid trigger and travel are set in the Keys tab and are kept. DKS keys are shown "
                           "but set up in the vendor app; they can be switched back to Normal here."))
        side.setFixedWidth(380)

        left = QVBoxLayout()
        left.addLayout(heading("Advanced", "Snap tap, toggle and mod-tap keys on the active profile."))
        left.addWidget(self.view, 1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 14)
        lay.setSpacing(16)
        lay.addLayout(left, 1)
        lay.addWidget(side)
        worker.connected.connect(self.on_snapshot)
        worker.frame.connect(self.view.set_frame)
        worker.job_done.connect(self._done)
        self._enable(False)

    # data ------------------------------------------------------------------------
    def on_snapshot(self, sn):
        self.snap = sn
        keys = load_keys(sn.keymap)
        self.view.set_keys(keys)
        self.view.set_snapshot(sn)
        self.names = {k.slot: k.name for k in keys if k.slot is not None}
        self.partner.clear()
        for s, n in sorted(self.names.items(), key=lambda kv: kv[1]):
            self.partner.addItem(n, s)
        self.modes = None
        if self.isVisible():
            self._read()

    def showEvent(self, ev):
        if self.snap is not None and self.modes is None:
            self._read()
        super().showEvent(ev)

    def _read(self):
        profile = self.snap.profile
        self.worker.submit("adv-read", lambda dev: advanced.read(dev, profile)[0])

    def _refresh(self):
        modes = self.modes or {}
        labels = {}
        for s, km in modes.items():
            name = self.names.get(s, str(s))
            if km.option == "snap":
                labels[s] = f"{name}⇄{self.names.get(km.partner, '?')}"
            elif km.option == "mt":
                labels[s] = f"{keymap.describe(km.tap)}/{keymap.describe(km.hold)}"
        self.view.set_annotations(labels, {s: MARK.get(km.option, QColor(theme.MUTED)) for s, km in modes.items()})
        self._show_key()

    def _slot(self):
        return next(iter(self.view.selected), None)

    def _enable(self, on):
        for w in (self.mode, self.pages, self.apply_btn):
            w.setEnabled(on)

    def _show_key(self):
        s = self._slot()
        if s is None or self.modes is None:
            self.key_name.setText("—")
            self.now.setText("Click a key on the board." if self.modes is not None else "reading…")
            self._enable(False)
            return
        km = self.modes.get(s, advanced.KeyMode())
        self.key_name.setText(self.names.get(s, f"slot {s}"))
        desc = advanced.LABELS.get(km.option, km.option)
        if km.option == "snap":
            desc += f" with {self.names.get(km.partner, '?')}"
        elif km.option == "mt":
            desc += f": tap {keymap.describe(km.tap)}, hold {keymap.describe(km.hold)} after {km.hold_units * 10} ms"
        self.now.setText(f"Now: {desc}")
        self._enable(True)
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(self.mode.findData(km.option), 0))
        self.mode.blockSignals(False)
        if km.option == "snap":
            self.partner.setCurrentIndex(max(self.partner.findData(km.partner), 0))
        base = self.snap.keymap[s]
        self.tap.set_entry(km.tap if km.option == "mt" else base)
        self.hold.set_entry(km.hold if km.option == "mt" else SHIFT)
        self.hold_ms.setValue((km.hold_units or advanced.DEFAULT_HOLD) * 10)
        self._mode_changed()

    def _mode_changed(self, *_):
        self.pages.setCurrentIndex(self.page_of[self.mode.currentData()])

    # writing -----------------------------------------------------------------------
    def _changes(self, s):
        modes = self.modes or {}
        opt = self.mode.currentData()
        changes = {}

        def unpair(slot):
            old = modes.get(slot)
            if old and old.option == "snap" and old.partner is not None:
                other = modes.get(old.partner)
                if other and other.option == "snap" and other.partner == slot and old.partner not in changes:
                    changes[old.partner] = advanced.KeyMode("normal")

        if opt == "snap":
            p = self.partner.currentData()
            if p == s:
                raise ValueError("pick a different key as the partner")
            changes[s] = advanced.KeyMode("snap", partner=p)
            changes[p] = advanced.KeyMode("snap", partner=s)
            unpair(s)
            unpair(p)
        elif opt == "mt":
            changes[s] = advanced.KeyMode("mt", tap=self.tap.entry(), hold=self.hold.entry(),
                                          hold_units=self.hold_ms.value() // 10)
            unpair(s)
        else:
            changes[s] = advanced.KeyMode(opt)
            unpair(s)
        return changes

    def _apply(self):
        s = self._slot()
        if s is None:
            return
        try:
            changes = self._changes(s)
        except ValueError as err:
            self.result.setText(str(err))
            return
        self.apply_btn.setEnabled(False)
        self.result.setText("writing…")
        self.worker.submit("adv-apply", guarded(lambda dev: (advanced.apply(dev, changes), sorted(changes))),
                           allow_writes=advanced.WRITES)

    def _done(self, token, result, error):
        if token == "adv-read":
            if error:
                self.now.setText(f"couldn't read the advanced modes: {error}")
            else:
                self.modes = result
                self._refresh()
            return
        if token != "adv-apply":
            return
        self.apply_btn.setEnabled(True)
        if error:
            self.result.setText(f"failed: {error}")
            QMessageBox.warning(self, "Write failed", f"{error}\n\nUse Backup → Restore to go back.")
            return
        made, (_n, slots) = result
        self.result.setText("written and verified: " + ", ".join(self.names.get(s, str(s)) for s in slots)
                            + backup_note(made))
        # a fresh snapshot (and with it a re-read) follows the write
