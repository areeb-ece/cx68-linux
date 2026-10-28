"""Main window: HUD tab (live travel), device tab, backup tab."""
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from cx68 import config, profiles

from . import theme
from .keyboard_view import KeyboardView
from .advanced_tab import AdvancedTab
from .gamepad_tab import GamepadTab
from .lighting_tab import LightingTab
from .remap_tab import RemapTab
from .widgets import backup_note, card, guarded, muted
from .keys_tab import KeysTab
from .layout import LINUX_TO_HID, load_keys

ROOT = Path(__file__).resolve().parents[1]
RATE_CAPS = [("Max", None), ("500 Hz", 500), ("240 Hz", 240), ("120 Hz", 120)]


class Card(QFrame):
    def __init__(self, title, unit=""):
        super().__init__(objectName="card")
        self.value = QLabel("—", objectName="cardValue")
        self.unit = QLabel(unit, objectName="cardUnit")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        lay.addWidget(QLabel(title.upper(), objectName="cardTitle"))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, Qt.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)

    def set(self, value, unit=None):
        self.value.setText(value)
        if unit is not None:
            self.unit.setText(unit)


def legend_html():
    from .keyboard_view import STATE_COLOR
    from .keystate import PRESSED, RESETTING, TRAVEL
    a, m = theme.ACCENT, theme.MUTED
    item = '<span style="color:{c}">{sym}</span>&nbsp;<span style="color:%s">{t}</span>&nbsp;&nbsp;&nbsp;&nbsp;' % m
    return "".join(item.format(c=c, sym=s, t=t) for c, s, t in [
        (STATE_COLOR[TRAVEL].name(), "■", "travelling"), (STATE_COLOR[PRESSED].name(), "■", "pressed"),
        (STATE_COLOR[RESETTING].name(), "■", "resetting"),
        ("#ffffff", "╌╌", "actuation"), (STATE_COLOR[RESETTING].name(), "╌╌", "release"),
        ("#ffffff", "▢", "OS sees pressed (key test)"), (a, "●", "rapid trigger")])


class HudTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.view = KeyboardView()
        self.subtitle = QLabel("waiting for keyboard…", objectName="muted")

        cap = QComboBox()
        for label, hz in RATE_CAPS:
            cap.addItem(f"Poll {label}", hz)
        cap.currentIndexChanged.connect(lambda i: setattr(worker, "rate_cap", cap.itemData(i)))
        cap.setCurrentIndex(0)   # Max: polling runs in its own process, so drawing never slows it down

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(QLabel("Live travel", objectName="heading"))
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        head.addWidget(cap)

        self.fps, self.frame_ms, self.p99 = Card("Frames / s", "full board"), Card("Frame time", "ms"), Card("p99", "ms")
        self.deepest, self.mode = Card("Deepest key"), Card("Trigger mode")
        self.display = Card("Display", "fps")
        self.display.setToolTip("Frames drawn per second. Only counts while something on the board changes; "
                                "an idle board isn't redrawn.")
        cards = QHBoxLayout()
        cards.setSpacing(12)
        for c in (self.fps, self.frame_ms, self.p99, self.display, self.deepest, self.mode):
            cards.addWidget(c, 1)

        self.test = QCheckBox("Key test")
        self.test.toggled.connect(self._toggle_test)
        self.test_result = QLabel("", objectName="muted")
        legend = QLabel(legend_html())
        foot = QHBoxLayout()
        foot.addWidget(legend, 1)
        foot.addWidget(self.test_result)
        foot.addWidget(self.test)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 14)
        lay.setSpacing(14)
        lay.addLayout(head)
        lay.addLayout(cards)
        lay.addWidget(self.view, 1)
        lay.addLayout(foot)
        self.setFocusPolicy(Qt.StrongFocus)

    def on_snapshot(self, sn):
        self.subtitle.setText(f"iCAN CX68  ·  firmware 0x{sn.firmware:04x}  ·  profile {sn.profile + 1}  ·  "
                              f"0.01 mm resolution  ·  backlight: {sn.backlight.describe() if sn.backlight else '—'}")
        self.subtitle.setToolTip("The backlight preview approximates the effect; colours and timing on the "
                                 "keyboard itself may differ. Key colours are modelled from your settings; "
                                 "the key test outline shows what the OS actually received.")
        km = sn.key_mode
        self.mode.set(km.get("mode", "—").capitalize(), "gamepad on" if km.get("gamepad_enable") else "gamepad off")

    def on_stats(self, s):
        self.fps.set(f"{s['fps']:.0f}")
        self.frame_ms.set(f"{s['median_ms']:.2f}")
        self.p99.set(f"{s['p99_ms']:.2f}")
        self.display.set(f"{self.view.render_fps()}")

    def on_frame(self, depth, _peak, _states=None):
        best = max(((depth[k.slot], k.name) for k in self.view.keys if k.slot is not None), default=(0, ""))
        self.deepest.set(f"{best[0]:.2f}" if best[0] > 0.02 else "—", f"mm  {best[1]}" if best[0] > 0.02 else "")

    # key test: the OS's own press/release events vs measured depth ---------------
    def _toggle_test(self, on):
        self.view.key_test.clear()
        self.view.os_pressed.clear()
        self.test_result.setText("focus this window, press keys slowly" if on else "")
        if on:
            self.setFocus()

    def _key_for(self, ev):
        usage = LINUX_TO_HID.get(ev.nativeScanCode() - 8)
        return next((k for k in self.view.keys if usage is not None and k.usage == usage and k.slot is not None), None)

    def keyPressEvent(self, ev):
        if not self.test.isChecked() or ev.isAutoRepeat():
            return super().keyPressEvent(ev)
        if k := self._key_for(ev):
            self.view.os_pressed.add(k.slot)
            self.view.key_test.setdefault(k.slot, {})["press"] = self.view.depth[k.slot]
            self._show(k)

    def keyReleaseEvent(self, ev):
        if not self.test.isChecked() or ev.isAutoRepeat():
            return super().keyReleaseEvent(ev)
        if k := self._key_for(ev):
            self.view.os_pressed.discard(k.slot)
            self.view.key_test.setdefault(k.slot, {})["release"] = self.view.depth[k.slot]
            self._show(k)

    def _show(self, k):
        t, sn = self.view.key_test[k.slot], self.view.snap
        self.test_result.setText(f"{k.name}: OS press at {t.get('press', float('nan')):.2f} mm, release at "
                                 f"{t.get('release', float('nan')):.2f} mm  (set: {sn.actuation[k.slot]:.2f} / "
                                 f"{sn.release[k.slot]:.2f})")


class BackupTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.out = QPlainTextEdit(readOnly=True)
        backup = QPushButton("Back up now", objectName="primary")
        diff = QPushButton("Compare with a backup…")
        restore = QPushButton("Restore…")
        backup.clicked.connect(self.backup)
        diff.clicked.connect(self.diff)
        restore.clicked.connect(self.restore)
        row = QHBoxLayout()
        row.addWidget(backup)
        row.addWidget(diff)
        row.addWidget(restore)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 12, 22, 14)
        lay.setSpacing(12)
        lay.addWidget(QLabel("Backups", objectName="heading"))
        lay.addWidget(QLabel("Back up and compare only read from the keyboard. Restore writes back per-key travel "
                             "settings and the trigger mode, after showing you exactly what it will change.",
                             objectName="muted"))
        lay.addLayout(row)
        lay.addWidget(self.out, 1)
        worker.job_done.connect(self._done)

    def backup(self):
        path = ROOT / "backups" / time.strftime("cx68-%Y%m%d-%H%M%S.json")
        path.parent.mkdir(exist_ok=True)
        self.out.appendPlainText("reading full configuration…")
        self.worker.submit(("backup", path), lambda dev: config.save(config.read_full_config(dev), path))

    def diff(self):
        path, _ = QFileDialog.getOpenFileName(self, "Backup to compare", str(ROOT / "backups"), "Backups (*.json)")
        if path:
            old = config.load(path)
            self.out.appendPlainText(f"comparing with {path}…")
            self.worker.submit(("diff", path), lambda dev: config.diff(old, config.read_full_config(dev)))

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "Backup to restore", str(ROOT / "backups"), "Backups (*.json)")
        if not path:
            return
        backup = config.load(path)
        self.out.appendPlainText(f"planning restore from {path}…")
        self.worker.submit(("restore-plan", path),
                           lambda dev: (backup, config.plan_restore(backup, config.read_full_config(dev))))

    def _run_restore(self, path, backup, cmds):
        if not cmds:
            self.out.appendPlainText("nothing to restore: per-key settings and mode already match")
            return
        steps = config.describe_restore(cmds)
        if QMessageBox.question(self, "Restore", f"Write {len(cmds)} packets to the keyboard?\n\n" + "\n".join(steps)) \
                != QMessageBox.Yes:
            self.out.appendPlainText("restore cancelled, nothing written")
            return

        def job(dev):
            import time as _t
            for c in cmds:
                dev.transact(*c)
            _t.sleep(0.5)
            dev.wait_ready()
            return config.diff(backup, config.read_full_config(dev))

        self.worker.submit(("restore", path), job, allow_writes=config.RESTORE_WRITES)

    def _done(self, token, result, error):
        if not isinstance(token, tuple) or token[0] not in ("backup", "diff", "restore-plan", "restore"):
            return
        if error:
            self.out.appendPlainText(f"failed: {error}")
        elif token[0] == "restore-plan":
            self._run_restore(token[1], *result)
        elif token[0] == "restore":
            self.out.appendPlainText("restored and verified: keyboard matches the backup" if not result else
                                     "restored; still different (not covered by restore):\n" + "\n".join(result))
        elif token[0] == "backup":
            self.out.appendPlainText(f"saved {token[1]} (sha256 {result[:16]}…)")
        else:
            self.out.appendPlainText("\n".join(result) if result else f"keyboard matches {token[1]} exactly")


class DeviceTab(QWidget):
    def __init__(self, worker):
        super().__init__(objectName="page")
        self.worker = worker
        self.label = QLabel("waiting for keyboard…")
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        prof, pl = card("PROFILE")
        self.profile = QComboBox()
        for p in range(4):
            self.profile.addItem(f"Profile {p + 1}", p)
        self.switch = QPushButton("Switch", objectName="primary")
        self.switch.clicked.connect(self._switch)
        row = QHBoxLayout()
        row.addWidget(self.profile)
        row.addWidget(self.switch)
        row.addStretch(1)
        self.profile_result = muted("")
        pl.addLayout(row)
        pl.addWidget(self.profile_result)
        pl.addWidget(muted("Each profile has its own keymap, lighting is shared. Per-key travel, remaps and "
                           "advanced keys are edited on the active profile."))
        prof.setMaximumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 12, 22, 14)
        lay.setSpacing(14)
        lay.addWidget(QLabel("Device", objectName="heading"))
        lay.addWidget(self.label)
        lay.addWidget(prof)
        lay.addStretch(1)
        worker.job_done.connect(self._done)

    def _switch(self):
        p = self.profile.currentData()
        self.switch.setEnabled(False)
        self.profile_result.setText("switching…")
        self.worker.submit("profile", guarded(lambda dev: profiles.switch(dev, p)), allow_writes=profiles.WRITES)

    def _done(self, token, result, error):
        if token != "profile":
            return
        self.switch.setEnabled(True)
        if error:
            self.profile_result.setText(f"failed: {error}")
            return
        made, p = result
        self.profile_result.setText(f"profile {p + 1} is active" + backup_note(made))

    def on_snapshot(self, sn):
        self.profile.setCurrentIndex(sn.profile)
        same = len({(a, r, d) for a, r, d in zip(sn.actuation, sn.release, sn.deadzone)}) <= 2
        self.label.setText(
            f"iCAN CX68  =  CamelTech CT-M68  (device id {sn.device_id})\n"
            f"Firmware 0x{sn.firmware:04x}  ·  travel resolution {sn.unit_mm} mm\n"
            f"Active profile {sn.profile + 1}\n"
            f"Trigger mode {sn.key_mode.get('mode')}  ·  keyboard disabled {sn.key_mode.get('keyboard_disable')}  ·  "
            f"gamepad {sn.key_mode.get('gamepad_enable')}\n"
            f"Per-key settings {'uniform' if same else 'vary per key'}  (hover keys on the HUD for details)")


class MainWindow(QMainWindow):
    def __init__(self, worker):
        super().__init__()
        self.setWindowTitle("CX68")
        self.worker = worker
        self.hud = HudTab(worker)
        self.device = DeviceTab(worker)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self.hud, "Live")
        tabs.addTab(KeysTab(worker), "Keys")
        tabs.addTab(RemapTab(worker), "Remap")
        tabs.addTab(AdvancedTab(worker), "Advanced")
        tabs.addTab(LightingTab(worker), "Lighting")
        tabs.addTab(GamepadTab(), "Gamepad")
        tabs.addTab(self.device, "Device")
        tabs.addTab(BackupTab(worker), "Backup")
        self.setCentralWidget(tabs)
        self.resize(1180, 640)
        worker.connected.connect(self.on_snapshot)
        worker.frame.connect(self.hud.view.set_frame)
        worker.frame.connect(self.hud.on_frame)
        worker.stats.connect(self.hud.on_stats)
        worker.status.connect(self.statusBar().showMessage)

    def on_snapshot(self, sn):
        self.hud.view.set_keys(load_keys(sn.keymap))
        self.hud.view.set_snapshot(sn)
        self.hud.on_snapshot(sn)
        self.device.on_snapshot(sn)
