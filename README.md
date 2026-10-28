# cx68 — Linux tools for the iCAN CX68 Hall-effect keyboard

A native replacement for the vendor's web configurator (`app.qmk.top` → `iotdriver.qmk.top`)
for the **iCAN CX68** (= CamelTech CT-M68, Rongyuan RY5088, USB `3151:502d`). It includes a
live analog HUD and an analog gamepad bridge for racing games.

| Phase | Status |
|---|---|
| 1. Hardware discovery | done: [`docs/hardware.md`](docs/hardware.md) |
| 2. Protocol capture | done: [`docs/protocol.md`](docs/protocol.md) |
| 3. Protocol library (`cx68/`) | reads, live travel, verified writes for travel, keymap (incl. media / system / mouse keys), Fn, lighting, profile, advanced modes (snap tap, toggle, mod-tap, DKS), macros; backup/diff/restore of all of them |
| 4. Desktop app + HUD (`cx68app/`, PySide6) | live view, per-key travel, remap (base + Fn), snap tap / toggle / mod-tap / DKS, macros (record or build), lighting, profiles, backup/restore, gamepad control |
| 5. Gamepad bridge (`cx68bridge/`) | uinput pad (Xbox 360 or generic), curves/deadzones/calibration, program allowlist: [`docs/gamepad.md`](docs/gamepad.md) |

## Safety

Every packet goes through `cx68/policy.py`, which enforces the table in `cx68/commands.json`:

- **Never sent:** factory reset, bootloader/DFU entry, firmware upgrade, flash erase,
  calibration writes, unknown opcodes, and packets with the wrong checksum type.
- **Read-only by default.** Writes need `Device(mode="write")`. The live-travel stream
  toggle needs `Device(allow=[0x1B])`.
- **One process at a time:** the device is `flock`ed, and a second `cx68` process is refused.

## Install (CachyOS / Arch)

The library uses only the Python 3.12+ standard library.

```fish
git clone https://github.com/areeb-ece/cx68-linux ~/keyboard; cd ~/keyboard
python3 -m cx68 info                  # needs read/write on /dev/hidraw* (see below)
```

The app needs PySide6: `sudo pacman -S --needed pyside6`.

The udev rules in `udev/70-cx68.rules` cover:

- hidraw access
- the phantom-joystick fix
- uinput for the gamepad bridge
- an optional, commented-out keyboard rule (read its trade-off before enabling it)

```fish
sudo install -m 644 udev/70-cx68.rules /etc/udev/rules.d/
sudo udevadm control --reload        # then unplug and replug the keyboard
systemctl --user link ~/keyboard/systemd/cx68-bridge.service   # gamepad bridge service
```

## Usage

```fish
python3 -m cx68 info                  # id, firmware, active profile, mode, gamepad bit, other openers
python3 -m cx68 backup                # full read-only backup -> backups/cx68-<time>.json
python3 -m cx68 show backups/cx68-….json
python3 -m cx68 diff backups/cx68-….json      # what changed since the backup (reads only)
python3 -m cx68 restore backups/cx68-….json   # writes back per-key HE settings + mode, then verifies
python3 -m cx68 watch                 # live per-key depth by polling (read-only, ~860 frames/s)
python3 -m cx68 stream --seconds 20   # live depth via the firmware's notification stream
```

App:

```fish
python3 -m cx68app                    # live view, per-key settings, backup/restore
env CX68_RASTER=1 python3 -m cx68app  # CPU rendering instead of OpenGL, if GL misbehaves
```

The app polls the keyboard in a separate process (`cx68-poller`), so drawing never slows
polling down. The view renders with OpenGL and follows the refresh rate of the monitor its
window is on (240 fps on a 240 Hz screen). Key colours are modelled from your settings: the
keyboard reports travel, not whether it counts a key as pressed. The backlight preview
approximates the configured effect.

Library:

```python
from cx68 import Device, config, live
with Device() as dev:                          # read-only, exclusive
    cfg = config.read_full_config(dev)
    config.save(cfg, "backup.json")
    for frame in live.poll(dev):               # reconnects after unplug/replug
        print(frame.travel[14] * 0.01, "mm")   # slot 14 = W
```

Gamepad bridge: [`docs/gamepad.md`](docs/gamepad.md).

```fish
python3 -m cx68bridge check           # permissions, config, phantom joystick, allowlist
python3 -m cx68bridge run             # or the systemd user service / the app's Gamepad tab
python3 -m cx68bridge calibrate       # rest and full-press travel of the mapped keys
python3 -m cx68bridge measure         # latency breakdown
```

## Tests

```fish
python3 -m unittest discover -s tests                     # library: replay harness, transport, backup, live
python3 -m unittest discover -s tools/capture -p 'test_*.py'
node tools/capture/test-policy.mjs
```

`tests/test_replay.py` regenerates every request the vendor page sent (captured, and
cross-checked against usbmon) from our builders, byte for byte, and decodes the captured
replies. The keymap, lighting, profile, advanced-mode, DKS and macro writers are built from the
vendor page's source and tested against a fake keyboard (`tests/fakes.py`) that models those
commands; their first run on the real board is still to come.

## Re-capturing after a firmware update

See `docs/protocol.md` §12. The tooling is in `tools/`:

| Tool | What it does |
|---|---|
| `hw_inventory.py` | sysfs-only interface map and report descriptors |
| `capture/iot_driver_shim.py` | Linux stand-in for the vendor's helper; logs and polices everything |
| `capture/capture.mjs` | Chromium with a WebHID logger (`app.qmk.top`) |
| `capture/usbmon.sh` + `crosscheck.py` | USB ground truth and byte-for-byte comparison |
| `capture/class_chain.py`, `dump_method.py` | opcode tables and method bodies from the vendor bundle |
| `capture/probe_travel.py`, `probe_stream.py` | standalone analog probes |
