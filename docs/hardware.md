# iCAN CX68 — hardware discovery (Phase 1, read-only)

Captured 2026-10-26 on CachyOS, kernel 7.3.0-4-cachyos, KDE/Wayland. Everything here came
from sysfs, `lsusb` and a passive read of interrupt-IN data. No report of any kind was sent to
the keyboard. Re-run with `python3 tools/hw_inventory.py --out docs/descriptors` after a
firmware update and diff `docs/descriptors/`.

## Device

| Field | Value |
|---|---|
| VID:PID | `3151:502d` |
| Product string | `Wired Keyboard` |
| Manufacturer string | six spaces (blank) |
| Serial | none (`iSerial 0`) |
| bcdDevice | `4.00` (possibly the firmware version, not confirmed) |
| Speed | USB 2.0 **High Speed** (480 Mbps) |
| MaxPower | 496 mA |
| Interfaces | 3, all HID, each with one interrupt-IN endpoint, 64 B, `bInterval 1` |

High Speed with `bInterval 1` means one poll per 125 µs microframe, so the keyboard can send
up to **8000 Hz** on every interface.

VID `0x3151` is the vendor ID used by MonsGeek/Akko-family boards, so this is probably an OEM
of that platform. That is **not** confirmed, and the protocol is treated as unknown until
Phase 2 captures it.

## Interface → node map

Event and hidraw numbers change on replug, so code should use the stable
`/dev/input/by-id/usb-_Wired_Keyboard-*` links or match on VID:PID and interface number.

| USB if | EP | hidraw | by-id | Input devices |
|---|---|---|---|---|
| 0 | 0x81 | `/dev/hidraw3` | `usb-_Wired_Keyboard-hidraw` | `event7` "Wired Keyboard" (boot kbd) — `…-event-kbd` |
| 1 | 0x82 | `/dev/hidraw4` | `usb-_Wired_Keyboard-if01-hidraw` | `event8` Consumer Control · `event9` System Control (`…-if01-event-joystick`, see below) · `event10` NKRO keyboard · `event11`/`mouse1` Mouse (`…-if01-event-mouse`) · `event12` vendor report 5 (`…-event-if01`) |
| 2 | 0x83 | `/dev/hidraw5` | `usb-_Wired_Keyboard-if02-hidraw` | none (vendor only) |

## Report descriptors

Full item-by-item decodes are in `docs/descriptors/if{0,1,2}.txt`, and the raw bytes are in `.bin` files next to them.

### Interface 0 — boot keyboard (59 B, no report IDs)
- Input, 8 bytes: modifier bitmap (E0–E7), reserved byte, 6-key array (0x00–0xFF).
- Output, 1 byte: 5 LED bits (Num/Caps/Scroll/Compose/Kana) + 3 pad.

### Interface 1 — composite (171 B)
| Report ID | Collection | Payload (excl. ID) | Notes |
|---|---|---|---|
| 1 | Keyboard (GD 0x06) | 15 B bitmap, usages 0x00–0x77 | NKRO. Modifiers (0xE0–E7) are **not** in this range, so they presumably still go over interface 0. |
| 2 | System Control (GD 0x80) | 1 × u16 usage array, 0x0000–0x7FFF | The range is far too wide, which causes the false-joystick problem below. |
| 3 | Consumer Control (0x0C) | 1 × u16 usage array, 0x000–0x33C | Media keys |
| 5 | **Vendor 0xFFFF / usage 0x01** | **31 B Input**, 0–255 | Vendor input channel. The kernel exposes it as `event12` with only `ABS_MISC`. |
| 6 | Mouse | 5 btn + pad, X/Y i16 rel, wheel i8, AC Pan i8 | Mouse-key emulation |

### Interface 2 — vendor config (20 B)
```
Usage Page 0xFFFF, Usage 0x02, Collection(Application)
  Logical -128..127, Report Size 8, Report Count 64
  Feature (Data,Var,Abs)
```
- A single **64-byte Feature report, no report ID** (report ID 0). Over hidraw that means
  `HIDIOCSFEATURE` / `HIDIOCGFEATURE` with a 65-byte buffer whose first byte is `0x00`. In WebHID
  it is `sendFeatureReport(0, data)` / `receiveFeatureReport(0)`.
- It has **no Input or Output reports**, even though the interface has an IN endpoint (0x83). Per the
  descriptor, all traffic on this interface goes over control endpoint 0 as SET_REPORT/GET_REPORT(Feature).

## Which interface carries configuration

- **Primary candidate: interface 2 (`hidraw5`)**, vendor page 0xFFFF usage 2, Feature reports.
  This is the usual command/response pattern for these vendor drivers: SET_FEATURE a 64-byte
  command, then GET_FEATURE to read the reply.
- **Secondary: interface 1 report ID 5** (vendor page 0xFFFF usage 1, 31-byte Input). It is a good
  candidate for asynchronous data such as live key-travel or analog streams and event notifications.
  A 5-second passive read of `hidraw4` with the keyboard idle got **zero** reports of any ID, so it
  does not stream unprompted. It may need a command to enable it, or a key press. Phase 2 will tell.
- Implication for analog streaming: if live travel only comes back through GET_FEATURE polling, each
  sample is a control transfer, which is much slower than the 8 kHz interrupt path. Report 5 would be
  the fast path if the firmware uses it.

WebHID visibility: Chromium hides keyboard/mouse/system-control collections from pages, but vendor
collections are allowed. The web app can therefore see interface 1 (only the report-5 collection) and
interface 2. Expect two `HIDDevice` objects with VID:PID `3151:502d`, and filter by
`collections[].usagePage === 0xFFFF`.

## Native gamepad / joystick?

**None.** No collection uses Joystick (GD 0x04), Game Pad (GD 0x05) or Simulation Controls (0x02),
and there are no `/dev/input/js*` nodes. One open question: some HE firmwares only add a gamepad
interface after a mode switch, by re-enumerating with a different descriptor. Phase 2 will check the
web app's JS for any "gamepad / joystick / XInput" mode before we assume it doesn't exist.

### ⚠ Spurious "joystick": event9 (System Control)
udev tags `event9` with `ID_INPUT_JOYSTICK=1` (by-id link `usb-_Wired_Keyboard-if01-event-joystick`).
The System Control collection declares Generic Desktop usages 0x0000–0x7FFF, which includes the
X/Y/hat usages. The kernel therefore gives it `ABS_HAT0X/HAT0Y/ABS_MISC` plus some BTN_* bits, and
udev's heuristic treats it as a joystick. SDL, Steam and Proton may list a "Wired Keyboard System Control"
controller that does nothing. This matters for Phase 5, because it may take controller slot 1 in
Assetto Corsa. Planned fix: a udev rule that clears `ID_INPUT_JOYSTICK` on that node, or SDL's
ignore-device list. We won't change that until Phase 5, and only with your OK.

## Environment notes for later phases

| Item | Status |
|---|---|
| hidraw access | ✅ udev `uaccess` on all hidraw (ACL `user:areeb:rw-` on hidraw3–5); nothing else currently holds them open |
| `/dev/uinput` | ✅ `uinput` module loaded; ACL already grants `areeb` rw (from an existing uaccess rule, likely `40-kdeconnect-uinput.rules` / Steam's). We'll still ship our own rule so it doesn't depend on those packages. |
| Steam input rules | `60-steam-input.rules` present |
| Toolchains | Python 3.14.8, Node 26.12.1, Rust/cargo, pip; `hidapi` 0.15 (C library) |
| Missing tools | `evtest`, `wireshark-cli` (tshark), `hid-tools` (hid-decode/hid-recorder), Playwright + Chromium, `sdl2-jstest`; `usbmon` module not loaded |
| Game | Assetto Corsa installed under `~/.local/share/Steam/steamapps/common/assettocorsa` |
