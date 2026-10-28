"""Remap tab: what each key sends, on the base layer or the Fn layer of the active profile."""
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from cx68 import keymap

from . import theme
from .keyboard_view import KeyboardView
from .layout import load_keys
from .widgets import KeyPicker, backup_note, card, guarded, heading, muted

LAYERS = [("base", "Base layer"), ("win", "Fn layer (Windows)"), ("mac", "Fn layer (Mac)")]


class RemapTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.snap = None
        self.data = {}             # "base" / "win" / "mac" -> 128 entries
        self.view = KeyboardView(selectable=True, single=True)
        self.view.bar_when_selected = False
        self.view.selection_changed.connect(lambda _s: self._show_key())
        self.layer = QComboBox()
        for key, label in LAYERS:
            self.layer.addItem(label, key)
        self.layer.currentIndexChanged.connect(lambda _i: self._refresh())

        side, sl = card("KEY")
        self.key_name = QLabel("—", objectName="cardValue")
        self.now = muted("Click a key on the board.")
        self.picker = KeyPicker()
        self.apply_btn = QPushButton("Apply", objectName="primary")
        self.apply_btn.clicked.connect(self._apply)
        self.reset_btn = QPushButton("Reset key to factory")
        self.reset_btn.clicked.connect(self._reset_key)
        self.reset_all = QPushButton("Reset whole layer…")
        self.reset_all.clicked.connect(self._reset_layer)
        self.result = muted("")
        row = QHBoxLayout()
        row.addWidget(self.apply_btn)
        row.addWidget(self.reset_btn)
        sl.addWidget(self.key_name)
        sl.addWidget(self.now)
        sl.addWidget(QLabel("NEW ACTION", objectName="cardTitle"))
        sl.addWidget(self.picker)
        sl.addLayout(row)
        sl.addWidget(self.result)
        sl.addStretch(1)
        self.summary = muted("")
        sl.addWidget(self.summary)
        sl.addWidget(self.reset_all)
        sl.addWidget(muted("Remapped keys have a blue rim and show their new action. Writes go to the active "
                           "profile and are read back to verify. Keys (with at most one modifier), media, system "
                           "and mouse buttons can be set here; macros are put on keys in the Macros tab."))
        side.setFixedWidth(360)

        top = QHBoxLayout()
        top.addLayout(heading("Remap", "Change what keys send. The Fn layer applies while Fn is held."), 1)
        top.addWidget(self.layer)
        left = QVBoxLayout()
        left.addLayout(top)
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
        self.view.set_keys(load_keys(sn.keymap))
        self.view.set_snapshot(sn)
        self.data["base"] = list(sn.keymap)
        if self.isVisible():
            self._load_fn()
        self._refresh()

    def showEvent(self, ev):
        if self.snap is not None and "win" not in self.data:
            self._load_fn()
        super().showEvent(ev)

    def _load_fn(self):
        self.worker.submit("remap-read", lambda dev: {"win": keymap.read_fn(dev, "win"), "mac": keymap.read_fn(dev, "mac")})

    def _layer(self):
        return self.layer.currentData()

    def _factory(self, layer=None):
        layer = layer or self._layer()
        return keymap.FACTORY_LAYER0 if layer == "base" else keymap.FACTORY_FN[layer]

    def _refresh(self):
        entries = self.data.get(self._layer())
        if entries is None:
            self.view.set_annotations({}, {})
            self.summary.setText("reading the Fn layer…" if self.snap else "")
            return
        factory = self._factory()
        accent = theme.qc(theme.ACCENT, 230)
        changed = {s for s in range(len(entries)) if entries[s] != factory[s]}
        if self._layer() == "base":
            labels = {s: keymap.describe(entries[s]) for s in changed}
        else:
            labels = {k.slot: keymap.describe(entries[k.slot]) if any(entries[k.slot]) else "—"
                      for k in self.view.keys if k.slot is not None}
        self.view.set_annotations(labels, {s: accent for s in changed})
        self.summary.setText(f"{len(changed)} key{'s differ' if len(changed) != 1 else ' differs'} from factory on this layer.")
        self._show_key()

    def _slot(self):
        return next(iter(self.view.selected), None)

    def _enable(self, on):
        for w in (self.picker, self.apply_btn, self.reset_btn):
            w.setEnabled(on)

    def _show_key(self):
        s = self._slot()
        entries = self.data.get(self._layer())
        if s is None or entries is None:
            self.key_name.setText("—")
            self.now.setText("Click a key on the board.")
            self._enable(False)
            return
        name = next((k.name for k in self.view.keys if k.slot == s), f"slot {s}")
        self.key_name.setText(name)
        cur, fac = entries[s], self._factory()[s]
        self.now.setText(f"Now: {keymap.describe(cur)}    Factory: {keymap.describe(fac)}")
        if self._layer() == "base" and keymap.FACTORY_LAYER0[s] == keymap.FN_ENTRY:
            self.now.setText("This is the Fn key. It stays Fn so the Fn layer remains reachable.")
            self._enable(False)
            return
        shown = self.picker.set_entry(cur)
        self._enable(True)
        if not shown:
            self.now.setText(self.now.text() + "\n(not an ordinary key; choosing an action replaces it)")

    # writing -----------------------------------------------------------------------
    def _write(self, changes, label):
        layer = self._layer()
        if layer == "base":
            profile = self.snap.profile
            job = guarded(lambda dev: keymap.write_keys(dev, profile, 0, changes))
            writes = keymap.WRITES
        else:
            job = guarded(lambda dev: keymap.write_fn_keys(dev, changes, layer))
            writes = keymap.FN_WRITES
        self.apply_btn.setEnabled(False)
        self.result.setText("writing…")
        self.worker.submit(("remap-apply", label, layer), job, allow_writes=writes)

    def _apply(self):
        s = self._slot()
        if s is not None:
            self._write({s: self.picker.entry()}, keymap.describe(self.picker.entry()))

    def _reset_key(self):
        s = self._slot()
        if s is not None:
            self._write({s: self._factory()[s]}, "factory")

    def _reset_layer(self):
        entries = self.data.get(self._layer())
        if entries is None:
            return
        factory = self._factory()
        changes = {s: factory[s] for s in range(len(entries)) if entries[s] != factory[s]}
        if not changes:
            self.result.setText("this layer already matches the factory keymap")
            return
        if QMessageBox.question(self, "Reset layer", f"Write the factory action back to {len(changes)} key(s) on "
                                f"the {self.layer.currentText().lower()}?") == QMessageBox.Yes:
            self._write(changes, "factory")

    def _done(self, token, result, error):
        if token == "remap-read":
            if not error:
                self.data.update(result)
                self._refresh()
            return
        if not (isinstance(token, tuple) and token[0] == "remap-apply"):
            return
        self.apply_btn.setEnabled(True)
        if error:
            self.result.setText(f"failed: {error}")
            QMessageBox.warning(self, "Write failed", f"{error}\n\nUse Backup → Restore to go back.")
            return
        made, n = result
        self.result.setText((f"written and verified ({n} key{'s' if n != 1 else ''})" if n else "already set")
                            + backup_note(made))
        if token[2] != "base":
            self._load_fn()    # base-layer changes arrive with the next snapshot
