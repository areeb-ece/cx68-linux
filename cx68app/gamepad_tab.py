"""Gamepad tab: run the analog bridge and choose which programs get the virtual pad."""
import subprocess

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QPushButton, QRadioButton, QVBoxLayout, QWidget)

from cx68bridge import access
from cx68bridge import mapping as M

SERVICE = "cx68-bridge.service"


def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args, SERVICE], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def card(title):
    frame = QFrame(objectName="card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    lay.addWidget(QLabel(title, objectName="cardTitle"))
    return frame, lay


def muted(text):
    label = QLabel(text, objectName="muted")
    label.setWordWrap(True)
    return label


class GamepadTab(QWidget):
    def __init__(self):
        super().__init__(objectName="page")
        self.policy = access.load()

        # bridge ---------------------------------------------------------------
        bridge, bl = card("BRIDGE")
        self.state = QLabel("—", objectName="cardValue")
        self.detail = muted("")
        self.start = QPushButton("Start", objectName="primary")
        self.stop = QPushButton("Stop")
        self.start.clicked.connect(lambda: self._service("start"))
        self.stop.clicked.connect(lambda: self._service("stop"))
        self.login = QCheckBox("Start at login")
        self.login.toggled.connect(lambda on: self._service("enable" if on else "disable"))
        edit = QPushButton("Edit mapping…")
        edit.clicked.connect(self._edit_mapping)
        row = QHBoxLayout()
        for w in (self.start, self.stop, edit):
            row.addWidget(w)
        row.addStretch(1)
        self.mapping = muted("")
        bl.addWidget(self.state)
        bl.addWidget(self.detail)
        bl.addLayout(row)
        bl.addWidget(self.login)
        bl.addSpacing(6)
        bl.addWidget(QLabel("MAPPING", objectName="cardTitle"))
        bl.addWidget(self.mapping)
        bl.addStretch(1)

        # access -----------------------------------------------------------------
        acc, al = card("WHO GETS THE GAMEPAD")
        self.only = QRadioButton("Only while one of these programs is running")
        self.any = QRadioButton("Any program, whenever the bridge runs")
        group = QButtonGroup(self)
        group.addButton(self.only)
        group.addButton(self.any)
        (self.only if self.policy.mode == "allowlist" else self.any).setChecked(True)
        self.only.toggled.connect(self._mode_changed)
        self.programs = QListWidget()
        self.programs.addItems(self.policy.programs)
        self.programs.setMinimumHeight(140)
        add_running = QPushButton("Add running program…")
        add_running.clicked.connect(self._add_running)
        add_name = QPushButton("Add by name…")
        add_name.clicked.connect(self._add_name)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        for w in (add_running, add_name, remove):
            buttons.addWidget(w)
        buttons.addStretch(1)
        self.running = muted("")
        al.addWidget(self.only)
        al.addWidget(self.programs, 1)
        al.addLayout(buttons)
        al.addWidget(self.running)
        al.addWidget(self.any)
        al.addWidget(muted(
            "Linux gives devices to your user, not to single programs, so anything you run could read a pad "
            "that exists. In the first mode the pad exists only while a listed program is running, and the "
            "keyboard is free for this app the rest of the time. Windows games under Proton are listed by "
            "their .exe name (Assetto Corsa: acs.exe). Changes apply within a second."))
        self._list_widgets = (self.programs, add_running, add_name, remove)

        head = QVBoxLayout()
        head.setSpacing(0)
        head.addWidget(QLabel("Gamepad", objectName="heading"))
        head.addWidget(muted("Analog keys as a virtual controller for games. See docs/gamepad.md for game setup."))
        cols = QHBoxLayout()
        cols.setSpacing(16)
        cols.addWidget(bridge, 1)
        cols.addWidget(acc, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 14)
        lay.setSpacing(14)
        lay.addLayout(head)
        lay.addLayout(cols, 1)

        self.timer = QTimer(self, interval=1000, timeout=self.refresh)
        self._mode_changed()
        self._show_mapping()

    # visibility ------------------------------------------------------------------
    def showEvent(self, ev):
        self.refresh()
        self._show_mapping()
        self.timer.start()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self.timer.stop()
        super().hideEvent(ev)

    # status ----------------------------------------------------------------------
    def refresh(self):
        active = systemctl("is-active") == "active"
        st = access.read_state() if active else None
        if not active:
            self.state.setText("Stopped")
            self.detail.setText("The bridge is not running. The keyboard is free for this app.")
        elif st and st["state"] == "active":
            self.state.setText("Active")
            self.detail.setText(f"Pad present for {st['detail']}. The keyboard is in use by the bridge, "
                                f"so the other tabs show it as busy until the program exits.")
        else:
            self.state.setText("Waiting")
            self.detail.setText("Running. The pad appears when an allowed program starts.")
        self.start.setEnabled(not active)
        self.stop.setEnabled(active)
        self.login.blockSignals(True)
        self.login.setChecked(systemctl("is-enabled") == "enabled")
        self.login.blockSignals(False)
        now = access.matches(self.policy, access.running_programs())
        self.running.setText(f"Running now: {', '.join(now)}" if now else "None of these is running now.")

    def _service(self, verb):
        systemctl(verb)
        self.refresh()

    def _show_mapping(self):
        try:
            cfg = M.load()
        except (M.ConfigError, ValueError) as err:
            self.mapping.setText(f"config error: {err}")
            return
        lines = [f"{a.negative + ' / ' if a.negative else ''}{a.positive or ''} → {a.output.replace('_', ' ')}"
                 for a in cfg.axes]
        lines += [f"{b.key} → {b.output}" for b in cfg.buttons]
        style = "Xbox 360 controller" if cfg.style == "xbox360" else "generic joystick"
        self.mapping.setText(f"Appears as: {style}\n" + "\n".join(lines))

    def _edit_mapping(self):
        M.load()          # creates the default file with comments if missing
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(M.CONFIG_FILE)))

    # policy ------------------------------------------------------------------------
    def _save(self):
        self.policy = access.Policy("allowlist" if self.only.isChecked() else "always",
                                    [self.programs.item(i).text() for i in range(self.programs.count())])
        access.save(self.policy)
        self.refresh()

    def _mode_changed(self, *_):
        for w in self._list_widgets:
            w.setEnabled(self.only.isChecked())
        if (self.policy.mode == "allowlist") != self.only.isChecked():
            self._save()

    def _add(self, name):
        name = name.strip()
        existing = {self.programs.item(i).text().lower() for i in range(self.programs.count())}
        if name and name.lower() not in existing:
            self.programs.addItem(name)
            self._save()

    def _add_running(self):
        names = sorted(access.running_programs(), key=str.lower)
        name, ok = QInputDialog.getItem(self, "Add running program", "Programs running now:", names, 0, False)
        if ok:
            self._add(name)

    def _add_name(self):
        name, ok = QInputDialog.getText(self, "Add program", "Program file name (for example acs.exe):")
        if ok:
            self._add(name)

    def _remove(self):
        for item in self.programs.selectedItems():
            self.programs.takeItem(self.programs.row(item))
        self._save()
