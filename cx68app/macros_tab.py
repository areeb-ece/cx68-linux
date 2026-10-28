"""Macros tab: record or build macros and put them on keys of the active profile's base layer."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from cx68 import keymap, macros
from cx68 import protocol as P

from . import theme
from .keyboard_view import KeyboardView
from .layout import load_keys
from .widgets import KeyPicker, backup_note, card, guarded, heading, muted

ADD_MODES = ("Tap", "Press", "Release")


class MacrosTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.snap = None
        self.stored = None         # {index: Macro} held by the keyboard
        self.refs = {}             # {index: [(layer name, slot)]} keys that run each macro
        self.names = {}
        self.index = None          # macro in the editor
        self.events = []
        self.dirty = False
        self.recorder = None

        self.view = KeyboardView(selectable=True, single=True)
        self.view.bar_when_selected = False
        self.view.selection_changed.connect(lambda _s: self._show_assign())
        self.slots = QListWidget()
        self.slots.setFixedHeight(150)
        self.slots.currentItemChanged.connect(self._open_slot)
        new = QPushButton("New macro")
        new.clicked.connect(self._new)
        self.status = muted("")

        # editor card
        ed, el = card("MACRO")
        self.title = QLabel("—", objectName="cardValue")
        self.list = QListWidget()
        self.list.setMinimumHeight(160)
        self.record_btn = QPushButton("● Record")
        self.record_btn.clicked.connect(self._toggle_record)
        tools = QHBoxLayout()
        for label, fn in (("Remove", self._remove), ("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1)),
                          ("Clear", self._clear)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            tools.addWidget(b)
        self.picker = KeyPicker(allow_disabled=False)
        self.add_mode = QComboBox()
        self.add_mode.addItems(ADD_MODES)
        add_key = QPushButton("Add key")
        add_key.clicked.connect(self._add_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.add_mode)
        key_row.addWidget(add_key)
        self.delay_ms = QSpinBox()
        self.delay_ms.setRange(1, 65535)
        self.delay_ms.setValue(50)
        self.delay_ms.setSuffix(" ms")
        add_delay = QPushButton("Add delay")
        add_delay.clicked.connect(lambda: self._insert([macros.delay(self.delay_ms.value())]))
        delay_row = QHBoxLayout()
        delay_row.addWidget(self.delay_ms)
        delay_row.addWidget(add_delay)
        self.repeat = QSpinBox()
        self.repeat.setRange(1, 65535)
        self.repeat.setPrefix("Play ")
        self.repeat.setSuffix(" ×")
        self.repeat.setToolTip("How often the macro plays per press when the key's mode is 'Play N times'")
        self.repeat.valueChanged.connect(lambda _v: self._touch())
        self.size = muted("")
        self.save_btn = QPushButton("Save to keyboard", objectName="primary")
        self.save_btn.clicked.connect(self._save)
        save_row = QHBoxLayout()
        save_row.addWidget(self.repeat)
        save_row.addWidget(self.save_btn, 1)
        for w in (self.title, self.list):
            el.addWidget(w)
        el.addWidget(self.record_btn)
        el.addLayout(tools)
        el.addWidget(self.picker)
        el.addLayout(key_row)
        el.addLayout(delay_row)
        el.addWidget(self.size)
        el.addLayout(save_row)

        # assignment card
        asg, al = card("PUT ON A KEY")
        self.key_name = muted("Click a key on the board.")
        self.key_mode = QComboBox()
        for mode, label in macros.MODE_LABELS.items():
            self.key_mode.addItem(label, mode)
        self.assign_btn = QPushButton("Assign", objectName="primary")
        self.assign_btn.clicked.connect(self._assign)
        self.unassign_btn = QPushButton("Restore key")
        self.unassign_btn.setToolTip("Put the key's factory action back")
        self.unassign_btn.clicked.connect(self._unassign)
        row = QHBoxLayout()
        row.addWidget(self.assign_btn)
        row.addWidget(self.unassign_btn)
        self.result = muted("")
        al.addWidget(self.key_name)
        al.addWidget(self.key_mode)
        al.addLayout(row)
        al.addWidget(self.result)

        side = QVBoxLayout()
        side.setSpacing(12)
        side.addWidget(ed, 1)
        side.addWidget(asg)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(400)
        side.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.addLayout(heading("Macros", "Up to 50 macros, shared by all profiles. Keys run them from the base "
                                        "layer of the active profile."), 1)
        top.addWidget(new)
        left = QVBoxLayout()
        left.addLayout(top)
        left.addWidget(self.view, 1)
        left.addWidget(self.slots)
        left.addWidget(self.status)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 14)
        lay.setSpacing(16)
        lay.addLayout(left, 1)
        lay.addWidget(side_w)
        worker.connected.connect(self.on_snapshot)
        worker.frame.connect(self.view.set_frame)
        worker.job_done.connect(self._done)
        self._editor_enabled(False)
        self._show_assign()

    # reading -----------------------------------------------------------------------
    def on_snapshot(self, sn):
        self.snap = sn
        keys = load_keys(sn.keymap)
        self.view.set_keys(keys)
        self.view.set_snapshot(sn)
        self.names = {k.slot: k.name for k in keys if k.slot is not None}
        self.stored = None
        if self.isVisible():
            self._read()

    def showEvent(self, ev):
        if self.snap is not None and self.stored is None:
            self._read()
        super().showEvent(ev)

    def _read(self):
        self.status.setText("reading macros and keymaps…")

        def job(dev):
            layers = {f"profile {p + 1} layer {layer}": keymap.read_layer(dev, p, layer)
                      for p in range(P.PROFILES) for layer in range(P.LAYERS)}
            layers |= {f"Fn ({os_name})": keymap.read_fn(dev, os_name) for os_name in P.FN_OS}
            return macros.read_all(dev), layers
        self.worker.submit("macro-read", job)

    def _place(self, layer, slot):
        name = self.names.get(slot, f"slot {slot}")
        here = f"profile {self.snap.profile + 1} layer 0"
        return name if layer == here else f"{name} ({layer})"

    def _refresh(self):
        current = self.index
        self.slots.blockSignals(True)
        self.slots.clear()
        for i in sorted(set(self.stored) | set(self.refs)):
            m = self.stored.get(i, macros.Macro(0))
            where = ", ".join(self._place(*r) for r in self.refs.get(i, [])) or "not on any key"
            self.slots.addItem(f"Macro {i + 1}   {m.summary()}   ·   {where}")
            self.slots.item(self.slots.count() - 1).setData(Qt.UserRole, i)
        self.slots.blockSignals(False)
        here = f"profile {self.snap.profile + 1} layer 0"
        labels = {s: f"M{i + 1}" for i, places in self.refs.items() for layer, s in places if layer == here}
        self.view.set_annotations(labels, {s: theme.qc(theme.ACCENT, 230) for s in labels})
        n = len(self.stored)
        self.status.setText(f"{n} macro{'s' if n != 1 else ''} stored. Macros no key uses are free to overwrite.")
        if current is not None:
            self._select_row(current)
        self._show_assign()

    def _select_row(self, index):
        for r in range(self.slots.count()):
            if self.slots.item(r).data(Qt.UserRole) == index:
                self.slots.blockSignals(True)
                self.slots.setCurrentRow(r)
                self.slots.blockSignals(False)

    # editor ------------------------------------------------------------------------
    def _editor_enabled(self, on):
        for w in (self.list, self.record_btn, self.picker, self.add_mode, self.delay_ms, self.repeat, self.save_btn):
            w.setEnabled(on)

    def _discard_ok(self):
        return not self.dirty or QMessageBox.question(
            self, "Macros", f"Discard the unsaved changes to macro {self.index + 1}?") == QMessageBox.Yes

    def _open_slot(self, item, _prev=None):
        if item is None:
            return
        index = item.data(Qt.UserRole)
        if index == self.index:
            return
        if not self._discard_ok():
            self._select_row(self.index)
            return
        m = self.stored.get(index, macros.Macro())
        self._load(index, m)

    def _new(self):
        if self.stored is None or not self._discard_ok():
            return
        try:
            index = macros.free_slot(self.refs)
        except ValueError as err:
            QMessageBox.information(self, "Macros", str(err))
            return
        self._load(index, macros.Macro())
        self.slots.clearSelection()

    def _load(self, index, m):
        self.index = index
        self.events = list(m.events)
        self.repeat.blockSignals(True)
        self.repeat.setValue(max(m.repeat, 1))
        self.repeat.blockSignals(False)
        self.dirty = False
        self._editor_enabled(True)
        self._render()
        self._show_assign()

    def _touch(self):
        self.dirty = True
        self._render()

    def _render(self, select=None):
        row = self.list.currentRow() if select is None else select
        self.list.clear()
        for e in self.events:
            self.list.addItem(e.describe())
        if self.events:
            self.list.setCurrentRow(min(max(row, 0), len(self.events) - 1))
        if self.index is None:
            self.title.setText("—")
            self.size.setText("Pick a macro below the board, or make a new one.")
            return
        m = macros.Macro(self.repeat.value(), self.events)
        used = macros.size(m)
        state = "unsaved changes" if self.dirty else "saved" if self.index in self.stored else "new"
        self.title.setText(f"Macro {self.index + 1}  ·  {state}")
        self.size.setText(f"{used} of {macros.CAPACITY} bytes" + ("  ·  too long" if used >= macros.CAPACITY else ""))
        self.save_btn.setEnabled(bool(self.events) and used < macros.CAPACITY and self.recorder is None)

    def _insert(self, events):
        if self.index is None:
            return
        at = self.list.currentRow() + 1 if self.list.currentRow() >= 0 else len(self.events)
        self.events[at:at] = events
        self.dirty = True
        self._render(select=at + len(events) - 1)

    def _add_key(self):
        e = self.picker.entry()
        mode = self.add_mode.currentText()
        if e[0] == 1:                                   # mouse button
            kind, mods, code = "mouse", [], e[2]
        elif e[0] == 0 and e[2]:
            kind, mods, code = "key", [e[1]] if e[1] else [], e[2]
        else:
            QMessageBox.information(self, "Macros", "Media and system keys can't go in a macro; put them on a key "
                                                    "in the Remap tab instead.")
            return
        down = [macros.key(u) for u in mods] + [macros.Event(kind, code, True)]
        up = [macros.Event(kind, code, False)] + [macros.key(u, False) for u in reversed(mods)]
        if mode == "Tap":
            self._insert(down + [macros.delay(20)] + up + [macros.delay(20)])
        else:
            self._insert(down if mode == "Press" else up)

    def _remove(self):
        r = self.list.currentRow()
        if r >= 0:
            del self.events[r]
            self.dirty = True
            self._render(select=r)

    def _move(self, step):
        r = self.list.currentRow()
        if 0 <= r and 0 <= r + step < len(self.events):
            self.events[r], self.events[r + step] = self.events[r + step], self.events[r]
            self.dirty = True
            self._render(select=r + step)

    def _clear(self):
        if self.events and QMessageBox.question(self, "Macros", "Remove every event?") == QMessageBox.Yes:
            self.events = []
            self._touch()

    # recording ---------------------------------------------------------------------
    def _toggle_record(self):
        app = QApplication.instance()
        if self.recorder is None:
            self.recorder = macros.Recorder()
            app.installEventFilter(self)
            self.record_btn.setText("■ Stop recording (click)")
            self.size.setText("Recording: type on the keyboard. Keys go into the macro, not to other apps. "
                              "Click Stop when done.")
            self.save_btn.setEnabled(False)
        else:
            app.removeEventFilter(self)
            recorded = self.recorder.finish()
            self.recorder = None
            self.record_btn.setText("● Record")
            if recorded:
                self._insert(recorded)
            else:
                self._render()

    def eventFilter(self, obj, ev):
        if self.recorder is not None:
            t = ev.type()
            if t == QEvent.ShortcutOverride:
                ev.accept()
                return True
            if t in (QEvent.KeyPress, QEvent.KeyRelease):
                if not ev.isAutoRepeat():
                    usage = keymap.LINUX_TO_HID.get(ev.nativeScanCode() - 8)
                    self.recorder.feed(usage, t == QEvent.KeyPress, float(ev.timestamp()))
                    self.size.setText(f"Recording: {len(self.recorder.events)} events so far…")
                return True
        return super().eventFilter(obj, ev)

    def hideEvent(self, ev):
        if self.recorder is not None:
            self._toggle_record()
        super().hideEvent(ev)

    # writing -----------------------------------------------------------------------
    def _macro(self):
        return macros.Macro(self.repeat.value(), list(self.events))

    def _save(self):
        index, m = self.index, self._macro()
        self.save_btn.setEnabled(False)
        self.result.setText("writing…")
        self.worker.submit(("macro-save", index), guarded(lambda dev: macros.write(dev, index, m)),
                           allow_writes=macros.WRITES)

    def _slot(self):
        return next(iter(self.view.selected), None)

    def _show_assign(self):
        s = self._slot()
        ok = s is not None and self.snap is not None and self.stored is not None
        if s is None:
            self.key_name.setText("Click a key on the board.")
        elif keymap.FACTORY_LAYER0[s] == keymap.FN_ENTRY:
            self.key_name.setText("This is the Fn key; it stays Fn.")
            ok = False
        else:
            now = keymap.describe(self.snap.keymap[s]) if self.snap else "?"
            self.key_name.setText(f"{self.names.get(s, s)}: now {now}")
        self.assign_btn.setEnabled(ok and self.index is not None and bool(self.events))
        self.assign_btn.setText("Save and assign" if self.dirty or (self.index is not None and self.index not in
                                                                    (self.stored or {})) else "Assign")
        self.unassign_btn.setEnabled(ok and macros.parse_entry(self.snap.keymap[s]) is not None)

    def _assign(self):
        s, index, profile = self._slot(), self.index, self.snap.profile
        mode = self.key_mode.currentData()
        m = self._macro() if self.dirty or index not in self.stored else None
        if m is not None and macros.size(m) >= macros.CAPACITY:
            self.result.setText("the macro is too long to save")
            return

        def job(dev):
            if m is not None:
                macros.write(dev, index, m)              # the page also writes the macro before the key
            return keymap.write_keys(dev, profile, 0, {s: macros.entry(index, mode)})
        self.assign_btn.setEnabled(False)
        self.result.setText("writing…")
        self.worker.submit(("macro-assign", index), guarded(job), allow_writes=(*macros.WRITES, *keymap.WRITES))

    def _unassign(self):
        s, profile = self._slot(), self.snap.profile
        self.worker.submit(("macro-assign", None),
                           guarded(lambda dev: keymap.write_keys(dev, profile, 0, {s: keymap.FACTORY_LAYER0[s]})),
                           allow_writes=keymap.WRITES)

    def _done(self, token, result, error):
        if token == "macro-read":
            if error:
                self.status.setText(f"couldn't read the macros: {error}")
                return
            self.stored, layers = result
            self.refs = macros.references(layers)
            if self.index is not None and not self.dirty:
                self._load(self.index, self.stored.get(self.index, macros.Macro()))
            self._refresh()
            return
        if not (isinstance(token, tuple) and token[0] in ("macro-save", "macro-assign")):
            return
        if error:
            self.result.setText(f"failed: {error}")
            self._render()
            QMessageBox.warning(self, "Write failed", f"{error}\n\nUse Backup → Restore to go back.")
            return
        made, _ = result
        if token[1] is not None and token[1] == self.index:
            self.dirty = False
        self.result.setText(("saved and verified" if token[0] == "macro-save" else "written and verified")
                            + backup_note(made))
        if token[0] == "macro-save":
            self._read()            # key writes bring a fresh snapshot, which re-reads
