# iCAN CX68 configuration protocol (Phase 2)

Status: read path, analog paths and the safety blocklist are verified on the device; write
commands are documented from source only and have **not** been sent. Captured 2026-10-27,
firmware 0x0400.

## 1. What the keyboard is

| | |
|---|---|
| USB | `3151:502d` "Wired Keyboard", 3 HID interfaces (see `docs/hardware.md`) |
| Vendor identity | **CamelTech CT-M68**, model `ry5088_cx68bug_8k_002_dm`, device id **2427** (`0x97B`) |
| Chip / platform | Rongyuan **RY5088** (the "RY" driver platform shared by many HE brands) |
| Firmware | `0x0400` (from `8F` reply bytes 7–8; matches `bcdDevice 4.00`) |
| Model data (from bundle) | 3.5 mm max travel; deadzone 0–1 mm (default 0.3); RT 0.01–2 mm; 16 layers; 8 kHz; no gamepad UI flag |

## 2. How the vendor software reaches it

```
app.qmk.top ──(device id 2427)──▶ redirects to iotdriver.qmk.top
iotdriver.qmk.top ──gRPC-web──▶ http://127.0.0.1:3814  "iot_driver" helper (Windows .exe)
helper ──HID feature reports──▶ keyboard interface 2
helper ◀─input report 5──────── keyboard interface 1 (notifications)
```

- `app.qmk.top` (the "v4" app) talks WebHID itself, but for this keyboard it only reads the
  device id (`8F`) and redirects.
- `iotdriver.qmk.top` has **no WebHID code**; it requires the native helper. On Linux we
  replace it with `tools/capture/iot_driver_shim.py`, which implements the helper's
  `driver.DriverGrpc` service on hidraw (schema in §9) and enforces the blocklist.
- All protocol knowledge lives in the page's JS. The CX68 is driven by protocol class chain
  `wGa → Kka → kba → Uga → YHe → EG → lG → JC → RC → AC` in
  `https://iotdriver.qmk.top/static/js/main_68eaf5ce.js` (local copy and pretty-printed
  version in `tools/capture/bundles/`). `wGa` only overrides default Fn matrices; `Kka`/`kba`
  only add bootloader/upgrade routines; everything else is the shared RY5088 code.
- **Opcodes are class-specific.** Sibling classes reuse numbers (e.g. `0x0A` is factory
  reset in one class and SET_KEYMATRIX in ours). `tools/capture/class_chain.py` regenerates
  the effective table; never reuse a table from a different model.

## 3. Transport

- **Channel:** interface 2 (`/dev/hidraw5`, vendor page `0xFFFF` usage 2), one **64-byte
  Feature report, report ID 0**. On hidraw: `HIDIOCSFEATURE(65)` / `HIDIOCGFEATURE(65)` with
  buffer byte 0 = `0x00`.
- **Request:** byte 0 = opcode, parameters after it, zero padded to 64. The helper pads to 9
  bytes, applies the checksum, then pads to 64.
- **Checksum** (byte 7 or 8, inverted sum):
  - **Bit7** (default, enum 0): `b[7] = 0xFF − (sum(b[0..6]) & 0xFF)`
  - **Bit8** (enum 1): `b[8] = 0xFF − (sum(b[0..7]) & 0xFF)`, used only by `0x08`
    (side LED) and `0x2B` (OLED effect)
  - **None** (enum 2): only the OLED firmware upgrade uses it (denied). Raw firmware chunks
    are therefore always rejected by "checksum must match the opcode's type".
  - Note: any Bit7 packet with `b[8] = 0` is also a valid Bit8 packet (bytes 0..7 sum to
    0xFF), so Bit8 validity alone proves nothing.
- **Reply:** `GET_FEATURE` on the same report.
- **Readiness quirk (measured):** until the firmware has processed a command, GET returns
  **the request itself**; it then overwrites that echo **front to back** while the host may
  be reading, so torn replies occur (`00 FE 01 01 00 00 00 1A …`). Readiness: 0.13 ms
  median, 0.32 ms max.
  - Data replies (`E5`, `9C`, `9E`, `8A`): poll until byte 7 differs from the request's
    byte 7 **and** two consecutive reads are identical.
  - Opcode-echo replies (`8F`, `84`, `9D`, …): the true reply can be byte-identical to the
    request (`9D` in comfort mode is; `84` for profile 0 is), so wait a fixed delay (2 ms;
    the vendor uses 10 ms) and read until stable.
- **One transaction at a time.** Two processes interleaving SET/GET pairs get each other's
  replies. The library must own the device exclusively (see Phase 3).

## 4. Command table

Source of truth: `tools/capture/commands.json` (used by both the WebHID logger and the shim).
All requests are Bit7 unless noted. "Verified" = seen on the wire with the reply decoded.

| Opcode | Name | Kind | Verified | Notes |
|---|---|---|---|---|
| `03` | SET_REPORT_RATE | write |  | [0x03, profile, rateCode]; RC maps only 1000/500/250/125 Hz; the page never calls it for the CX68 (8 kHz board) - open question |
| `04` | SET_PROFILE | write |  | [0x04, profile] |
| `05` | SET_LEDONOFF | write |  |  |
| `06` | SET_DEBOUNCE | write |  | [0x06, debounce] |
| `07` | SET_LEDPARAM | write |  | [0x07, mode 1..14, speed, brightness, ..., r, g, b] BIT7 on boards without OLED |
| `08` | SET_SIDE_LEDPARAM | write |  | [0x08, mode, speed, brightness, option, r, g, b] BIT8, sent with 500 ms delay **Bit8** |
| `09` | SET_KBOPTION | write |  | [0x09, profile, flags bitfield, b3, b4] |
| `0A` | SET_KEYMATRIX | write |  | bulk: [0x0A, profile, 0xFF, page, len, last, ...] + 56 data bytes (4 bytes per key); single: [0x0A, profile, keyPos, ..., b[5]=1, b[6]=layer offset, b[8..11]=4-byte code]. In some other RY classes 0x0A is reset; this entry is only valid for this class chain |
| `0B` | SET_MACRO | write |  |  |
| `0C` | SET_USERPIC | write |  |  |
| `0D` | SET_AUDIO | write |  |  |
| `0E` | SET_WINDOWS_LOCK | write |  |  |
| `10` | SET_FN | write |  |  |
| `11` | SET_SLEEPTIME | write |  |  |
| `12` | SET_USERGIF | write |  |  |
| `13` | SET_KEYMATRIX_SIMPLE | write |  |  |
| `14` | SET_USERPIC_SIMPLE | write |  |  |
| `15` | SET_FN_SIMPLE | write |  |  |
| `16` | SET_MACRO_SIMPLE | write |  |  |
| `17` | SET_AUTO_OS | write |  |  |
| `18` | SET_USERGIFSTART | write |  |  |
| `1A` | SET_MAGNETISM_MODE | write |  |  |
| `1B` | SET_TRAVEL_REPORT | volatile | ✅ | [0x1B, 1\|0] BIT7; enables/disables travel notifications (interface 1, report 5, first byte 0x1B) |
| `1D` | SET_KEY_MAGNETISM_MODE | write |  | [0x1D, mode (0 comfort,1 sensitive,2 gaming,3 custom), keyboardDisable, gamepadEnable]; the page sends 1D 03 00 00 on opening key settings |
| `27` | SET_OLED_LANGUAGE | write |  |  |
| `28` | SET_OLED_CLOCK | write |  |  |
| `2A` | SET_OLED_WEATHER | write |  |  |
| `2B` | SET_OLED_EFFECT | write |  |  **Bit8** |
| `65` | SET_MAGNETISM_BY_ARR | write |  | [0x65, param, bulk(1)/single(0), page, last, ...]; BIT7; data 56 bytes per packet; values as for 0xE5 |
| `82` | GET_BATTERY | read | ✅ |  |
| `83` | GET_REPORT_RATE | read |  | [0x83, profile] |
| `84` | GET_PROFILE | read | ✅ | [0x84]; reply b[1] = current profile |
| `85` | GET_LEDONOFF | read | ✅ |  |
| `86` | GET_DEBOUNCE | read |  |  |
| `87` | GET_LEDPARAM | read | ✅ |  |
| `88` | GET_SIDE_LEDPARAM | read |  |  |
| `89` | GET_KBOPTION | read | ✅ | [0x89, profile]; reply flags at b[2], options b[3], b[4] |
| `8A` | GET_KEYMATRIX | read | ✅ | [0x8A, profile, 0xFF, page 0..7, layer]; reply is raw 4-byte entries, no opcode echo |
| `8B` | GET_MACRO | read |  |  |
| `8C` | GET_USERPIC | read |  |  |
| `8F` | GET_REV | read | ✅ | reply: u32 LE device id at [1..4] (2427), firmware version (b[8]<<8)\|b[7] (0x0400) |
| `90` | GET_FN | read | ✅ |  |
| `91` | GET_SLEEPTIME | read |  |  |
| `93` | GET_KEYMATRIX_SIMPLE | read |  |  |
| `94` | GET_USERPIC_SIMPLE | read |  |  |
| `95` | GET_FN_SIMPLE | read |  |  |
| `96` | GET_MACRO_SIMPLE | read |  |  |
| `97` | GET_AUTO_OS | read |  |  |
| `9C` | GET_MAGNETISM_CAL | read |  | static/rest raw values, b[1]=page |
| `9D` | GET_KEY_MAGNETISM_MODE | read | ✅ | [0x9D]; reply [0x9D, mode, keyboardDisable, gamepadEnable] - reads gamepad state without writing |
| `9E` | GET_MAGNETISM_CALMAX | read |  | dynamic/max raw values, b[1]=page |
| `AB` | GET_OLED_EFFECT | read |  |  |
| `AD` | GET_OLED_VERSION | read |  |  |
| `AE` | GET_MLED_VERSION | read |  |  |
| `E5` | GET_MAGNETISM_BY_ARR | read | ✅ | [0xE5, param, bulk=1, page]; reply = raw values, no opcode echo. u16 LE per matrix slot, 32 per page, 4 pages = 128 slots, units 0.01 mm (firmware 0x0400). params: 0 actuation, 1 release ('lift'), 2 RT press, 3 RT release, 4 DKS start, 5 MT hold time, 6 bottom deadzone, 7 key mode byte (u8, 2 pages), 8 DKS flags, 9 snap/SOCD partner, 10 all-keys DKS flags, 252 switch type, 254 live press travel, 255 live dynamic value. Factory defaults: actuation 200, release 280, deadzone 30, RT 50/50 |

### Denied (never sent; enforced in code)

| Opcode | Why |
|---|---|
| `00` | SET_REV (purpose not documented). Also what setKeyMagnetismFast actually sends on this class chain: FEA_CMD_SET_MAGNETISM_FAST is undefined here, so byte 0 stays 0x00 - a vendor bug |
| `01` | factory reset (FEA_CMD_SET_RESERT) |
| `02` | SET_BATTERY_LP (wireless low-power; unused on this wired board) / factory reset in older RY classes |
| `1C` | write min-travel calibration (only inside an explicit, user-confirmed calibration flow) |
| `1E` | write max-travel calibration (only inside an explicit, user-confirmed calibration flow) |
| `30` | OLED enter bootloader |
| `31` | OLED boot start |
| `32` | TFT flash data write |
| `40` | MLED enter bootloader |
| `41` | MLED boot start |
| `7F` | enter USB bootloader (7F 55 AA 55 AA) |
| `AC` | flash chip erase (FEA_CMD_SET_FLASHCHIPERASSE) |
| `B0` | OLED bootloader status |
| `B1` | OLED boot checksum |
| `B2` | TFT flash data read (flash tooling) |
| `BA` | firmware upgrade start/verify (BA C0 / BA C2) |
| `C0` | MLED bootloader status |
| `C1` | MLED boot checksum |
| `F7` | RF bootloader status (upgrade flow only) |
| `F8` | enter RF bootloader (F8 55 AA 55 AA) |

Also denied by rule: every opcode not in the table, any checksum not matching the opcode's
type, WebHID output reports, and the helper's `sendRawFeature` / `readRawFeature` /
`cleanDev` / `upgradeOTAGATT`.

**Vendor bug worth knowing:** `setKeyMagnetismFast` uses `FEA_CMD_SET_MAGNETISM_FAST`,
which is undefined in this class chain, so on a CX68 it would send opcode `0x00` (denied).

## 5. Per-key Hall-effect settings (`E5` read / `65` write)

Request: `[E5, param, 1 (bulk), page]`. Reply: raw values, **no opcode echo**. u16
little-endian per **matrix slot**, 32 slots per page, 4 pages = 128 slots (68 used). The key
mode (param 7) is u8, 2 pages. Units: **0.01 mm** on firmware 0x0400 (the app uses 0.1 mm
below 0x0300 and 0.005 mm from 0x0500).

| param | meaning | CX68 value now (all keys) |
|---|---|---|
| 0 | actuation point | 200 = **2.00 mm** |
| 1 | release ("lift") point | 280 = **2.80 mm** (see open questions) |
| 2 / 3 | rapid-trigger press / release sensitivity | not read (RT off); vendor default 50 = 0.50 mm |
| 4 | DKS start travel | — |
| 5 | mod-tap hold time | — |
| 6 | bottom deadzone | 30 = **0.30 mm** |
| 7 | key mode byte (u8) | `00` = normal, RT off |
| 8 | DKS flags | — |
| 9 | snap-tap / SOCD partner slot | — |
| 10 | all-keys DKS flags | — |
| 252 | switch type | — |
| **254** | **live press travel** (§7) | 0 at rest |
| 255 | live "dynamic" value | — |

These values equal the vendor's write-path defaults (`travel 200, liftTravel 280,
deadZone 30, RT 50/50`), so the board is at factory settings.

Write (**verified**, `captures/09-write-session-shim.jsonl`, replayed byte for byte by
`tests/test_replay_writes.py`): `[65, param, 1, page, last, 00, 00, ck]` + 56 data bytes,
Bit7, no reply. The data is the whole array exactly as `E5` reads it: u16 params take 5 pages
(256 B), u8 params take 3 (128 B). `last = 1` only on the final page of the final param in a
batch; that commits it. The vendor's Apply sends e.g. `param 7` (mode) then `param 0`, or
`7, 3, 2` for rapid trigger.

Mode byte: **bit 7 = rapid trigger (verified)**. Per the v4 bundle, not yet verified here:
bit 6 = full-bottom, low nibble 0 normal, 2 DKS, 3 mod-tap, 4 toggle-hold, 5 toggle-dots,
7–10 snap-tap variants. RT press/release are in 0.01 mm (the page's RT default is 20/30 =
0.20/0.30 mm).

**Write quirks (measured):**
- **Pacing:** the vendor waits 10 ms before each write. Sent back to back, the firmware
  silently dropped 1 of 10 packets. The library waits 10 ms around every write.
- **Busy after commit:** after a `last = 1` page the firmware returns unprocessed echoes for
  over 200 ms, intermittently. Reads must wait (`Device.wait_ready()` polls `8F` until it
  sees a real device id) and are re-sent on timeout. Writes are never re-sent.
- **Vendor side effect:** enabling RT on one key made the page rewrite RT press/release for
  **every** key (50 → 0 on keys without RT), and reverting didn't restore them.
  `python3 -m cx68 restore` fixed it; `diff` then matched the backup exactly.
- `1D 03 00 00` (the page's automatic "custom" mode) left `9D` reading comfort afterwards.
  It doesn't appear to persist, or it's overridden; harmless either way.

## 6. Other configuration blocks

| Block | Read | Write | Format |
|---|---|---|---|
| Keymap | `8A [profile, FF, page 0..7, layer]` | `0A` bulk `[profile, FF, page, len, last]` + 56 B, or single `[profile, slot, …, b5=1, b6=layer offset, b8..11=code]` | 4 bytes per slot; byte 2 = HID usage (`29` Esc, `1A` W …). 8 pages × 4 layers per profile; Fn layers via `90`/`10` |
| Profile | `84` → `b[1]` | `04 [profile]` | |
| Lighting | `87` | `07 [mode 1..14, speed, brightness, …, r, g, b]` | |
| Side LED | `88` | `08 …` **Bit8**, 500 ms delay | |
| Keyboard options | `89 [profile]` → flags `b[2]`, `b[3]`, `b[4]` | `09 [profile, flags, b3, b4]` | flags is a bitfield |
| LED on/off | `85` | `05` | |
| Debounce | `86` → `b[1]` | `06 [value]` | |
| Sleep | `91` | `11` | u16 fields |
| Trigger mode / gamepad | `9D` → `[9D, mode, keyboardDisable, gamepadEnable]` | `1D [mode, keyboardDisable, gamepadEnable]` | mode 0 comfort, 1 sensitive, 2 gaming, 3 custom |

Slot → key: matrix is column-major with 6 rows per column (slot 8 Q, 9 A, 14 W, 15 S,
20 E, 21 D, 41 Space …). The full layer-0 map, decoded from the device's own `8A` reply, is
in `captures/slot-map-layer0.json`.

### Lighting read (`87`), decoded from the vendor's `getLightSetting`

Reply `[87, mode, speed, brightness, option, r, g, b]`, implemented in `cx68/lighting.py`:

- `mode`: 0 off, 1 always on, 2 breathing, 3 neon, 4 wave, 5 ripple, 6 raindrop, 7 snake,
  8 reactive, 9 converge, 10 sine wave, 11 kaleidoscope, 12 line wave, 13 user picture, 14 laser,
  15 circle wave, 16 dazzle, 17 rain down, 18 meteor, 19 reactive-off, 20/22 music, 21 screen colour,
  23 train, 24 fireworks, 25 user colour.
- `speed` is stored inverted: the displayed speed is `4 - b[2]` (MAXSPEED 4).
- `brightness` is assumed to be 0..4 (not confirmed).
- `option`: the low nibble is the colour: 8 is rainbow (DAZZLE), 7 is the RGB bytes, 0..6 are
  presets (red, orange, yellow, green, cyan, blue, magenta). The high nibble is the direction
  (wave: 0 right, 1 left, 2 down, 3 up) or the variant.
- Board at factory: `04 02 04 08 96 96 96`, i.e. rainbow wave, rightward, speed 2, brightness 4.


### Configuration writes (source-confirmed, used by cx68app)

All of these come from this class chain's own methods (class `Uga`), and each one is verified
after writing by reading the value back. They have not yet been compared against a captured
vendor session (session 10), so a mismatch shows up as a verify error, not silent damage.
The tests are in `tests/test_writers.py`.

| Write | Packet | Read-back |
|---|---|---|
| Single key (`setKeyConfigSimple`) | `0A profile slot 00 00 01 layer ck e0 e1 e2 e3` (Bit7). On Hall-effect boards the page's `currentProfile` is 4 × profile, so byte 1 is the profile and byte 6 the layer | `8A` page |
| Fn key (`setFnKeyConfigSimple`) | `10 os fnlayer slot 00 00 00 ck e0..e3` (Bit7; os 0 win / 1 mac) | `90` page |
| Lighting (`setLightSetting`) | `07 mode 4-speed brightness option r g b ck` with **Bit8** (b[7] is blue). White is sent as `FA FF FA` | `87` b[1..7] |
| Profile (`setCurrentProfile`) | `04 profile` (Bit7) | `84` |
| Per-key value (`setMultiMagnetismModes`) | `65 param 00 slot last 00 00 ck data`: MT hold time 1 byte (×10 ms, 1–100), snap partner 1 byte (slot), DKS flags 4 bytes, DKS start u16le | `E5 param 00 slot`, value at reply byte 0 |
| Macro (`_setMacro`, `$Y.setMacro`) | `0B macro page 38 last 00 00 ck` + 56 bytes (Bit7), one page per non-empty 56-byte slice of the 256-byte buffer; `last` commits | `8B macro page` → 64 data bytes, until a page holds `00 00 00 00` (max 4) |

Key entries are `[0, modifier, key, key2]`. The modifier is a modifier's own HID usage
(Ctrl E0, Shift E1, Alt E2, Win E3), so an entry can hold only one modifier. All zeros means
disabled. Fn is `0A 01 00 00`. From the page's tables (`CB`, `CY`):

- media / consumer keys `03 00 lo hi` (consumer usage, little-endian: volume up `03 00 E9 00`,
  calculator `03 00 92 01`)
- system power / sleep / wake `02 81 00 00`, `02 82 00 00`, `02 83 00 00`
- mouse buttons `01 00 F0..F4 00` (left, right, middle, back, forward)
- macros `09 mode index 00`: mode 0 plays the macro its repeat count of times, 1 toggles it,
  2 repeats it while the key is held. 50 macro slots (indexes 0–49) are shared by all profiles.

Macro buffer (256 bytes, `$Y.setMacro` / `buffToMacroEvents`): u16le repeat count, then
events. A key event is `[usage, flags]` (a mouse button uses its `F0..F4` code): flags bit 7 is
down, and the low 7 bits are the delay after the event in ms when it is 1–127. Otherwise a
u16le delay follows (0 if none). A mouse move is `[F9, delay, dx, dy]`. Four zero bytes end
the macro. `cx68/macros.py` mirrors the vendor encoder byte for byte, and the page refuses
macros whose encoding reaches 248 bytes.

The mode byte (param 7) is `option | 0x80 if rapid trigger`. The options are normal 0, DKS 2,
mod-tap 3, toggle (hold variant) 4, toggle (dots variant) 5, gamepad key 6 and snap 7. An
advanced key's actions are stored in layers 0–3 of the profile at the key's own slot:

- mod-tap: tap in layer 0, hold in layer 1; layers 2–3 are cleared
- toggle: layer 0; layers 1–3 are cleared
- snap: no layers
- DKS: up to four actions in layers 0–3. The per-key "DKS flags" value is one byte per
  action, with a 2-bit code for each of four stages of the stroke: bits 0–1 pressing past the
  start depth, 2–3 bottoming out, 4–5 lifting off the bottom, 6–7 rising back past the start
  depth. Codes (from the DKS editor's row component): 0 nothing, 1 tap, 2 press here and release
  at the next stage, 3 press here and stay held through the next stage (which is then 2 or 3).
  The last stage can only tap. The "DKS start" value is the start depth in 0.1 mm units
  (0.5–2.5 mm, default 0.7 mm).

The page writes in this order: macros, then layer entries, then the mode array, then the
per-key values (DKS start, DKS flags, MT time, snap partner). The commit flag goes on the very
last packet.

Not used:

- `05`/`85` LED on/off: the page reads it only for the "keyboard3123" family, so its meaning on
  this board is unknown. Mode 0 is "off".
- `06` debounce: the page shows it only for models that define it, and this board reads 0.
- The report rate (8 kHz): it isn't in this class chain.

## 7. Analog data (the important part)

Two independent paths, both verified with key presses (`captures/06-probe-keys.jsonl`,
`captures/08-stream-keys.jsonl`):

### 7a. Whole-board polling: `E5 FE 01 <page>` (read-only)

- Four transactions per frame give the live travel of **every** slot: u16 LE, 0.01 mm,
  0 = released, **350 = 3.50 mm = bottomed out** (observed).
- Measured with correct readiness handling: **774 frames/s** (median frame 1.22 ms, p99
  1.96 ms). Polling flat out without readiness checks reaches ~1100 frames/s but returns
  torn/echo frames.
- `E5 FF` returns the "dynamic" value in the same layout (not yet characterised).
- Needs no state change on the keyboard. **This is the HUD's data source.**

### 7b. Push stream: `1B 01` / `1B 00` (volatile on/off)

- After `1B 01`, interface 1 sends **input report 5** whenever a key moves:
  `05 1B <lo> <hi> <slot> 00 …` = u16 LE travel (0.01 mm) for that slot. Releases are
  reported with value 0.
- The vendor UI only uses byte 2 and ignores the slot byte; the slot byte is reliable
  (matched the keys pressed and the polling path exactly).
- ~**970 notifications/s** while keys move, median gap 0.5 ms; nothing while idle.
- Lower host load than polling; requires toggling `1B` and must be turned off on exit.
  `probe_stream.py` always sends `1B 00` in a `finally`.

### 7c. Calibration reads: `9C` / `9E` (read-only)

Raw sensor "static" and "max" values, 4 pages; the calibration UI computes
`|max − static| / 400` per key. Calibration **writes** (`1C`, `1E`) are denied.

## 8. Gamepad mode

- Current state, read with `9D` (no write): mode **comfort**, keyboard enabled,
  **gamepad disabled**.
- The firmware command carries a `gamepadEnable` bit, and the app has full gamepad
  mapping / curve / disable screens, but the CX68's model entry lacks the `gmaepad` flag,
  so the vendor never shows them for this board. Whether this firmware implements the bit
  is **unknown**; testing it is a write (`1D [mode, 0, 1]`) and is deferred to Phase 5,
  after a backup, with explicit approval.
- The iotdriver page writes `1D 03 00 00` (switch to "custom") on opening key settings even
  when nothing is changed; with that write blocked it shows "read device error".

## 9. `driver.DriverGrpc` (vendor helper interface)

gRPC-web on `http://127.0.0.1:3814`, `application/grpc-web-text` (base64) by default.

| Method | Request → Response | Shim |
|---|---|---|
| `watchDevList` | Empty → stream `DeviceList{repeated DJDev{Device dev=1}, type}` | implemented (hot-plug) |
| `watchVender` | Empty → stream `VenderMsg{bytes msg}` (report incl. ID byte) | implemented (report 5 only) |
| `sendMsg` | `SendMsg{path, msg, checksumtype, dangledevtype}` → `ResSend{err}` | implemented + policy |
| `readMsg` | `ReadMsg{path}` → `ResRead{err, msg}` | implemented |
| `getVersion`, `changeWirelessLoopStatus`, `setLightType` | | stubs (needed to start) |
| `insertDb`/`getItemFromDb`/`getAll*FromDb`/`deleteItemFromDb` | page settings cache | JSON files in `tools/capture/shim-db/` |
| `sendRawFeature`/`readRawFeature` | used only for 2.4 GHz dongle id (`F1`) | refused |
| `upgradeOTAGATT`, `cleanDev`, weather, microphone | | refused / unimplemented |

`Device{devtype=1 enum, is24=2, path=3, id=4, battery=5, isonline=6, vid=7, pid=8}`;
`DeviceType{YZWKEYBOARD=0, YZWBOOT=1, YZWVENDER=2}`. The shim never reports a boot device.
Only the `qmk.top` origins may call the shim (any other origin gets 403).

## 10. Verification

| Check | Result |
|---|---|
| Shim log vs usbmon capture (`crosscheck.py`, session 04) | **696/696 events identical** |
| Blocked packets on the wire | none (wire carried only `82 84 85 87 89 8A 8F 90 E5`) |
| WebHID vs hidraw for `8F` | identical request and reply |
| Policy tests | 23 Python + 21 JS, all pass (`test_shim.py`, `test-policy.mjs`) |
| Writes sent to the keyboard | `1B 01/00` (approved, volatile); approved session 09 (`1D`, `65` by the vendor page); `65` RT restore by the library. The keyboard matches the 2026-10-27 backup exactly afterwards |
| Write replay | all 30 vendor write packets rebuilt byte for byte by `set_magnetism_array` / `set_key_mode` |

## 11. Open questions (resolve before/while writing)

1. ~~Release point semantics~~: resolved with the HUD's key test (OS events vs measured
   depth). W pressed at 1.98 mm (actuation 2.00) and released at 2.84 mm (release 2.80).
   The release value is an **absolute depth from the top**: a pressed key releases once
   it rises above that depth. The factory 2.80 mm means a key releases after lifting only
   ~0.7 mm from the bottom. The scatter (fast presses) is polling/compositor delay, not
   firmware behaviour.
2. **Report rate.** The page never calls `83`/`03` for this board, and the class's encoder
   only knows 125–1000 Hz. The 8 kHz setting may live in the keyboard-options block.
3. ~~RT units / RT bit~~: resolved, 0.01 mm and bit 7 (write session 09). The other
   mode-byte options are now taken from this class's source (section 6). ~~DKS flag bits~~:
   decoded from the page's DKS editor (section 6).
5. **Session 10 capture** would confirm the source-derived writes above byte for byte.
4. **Gamepad bit** support on this firmware (Phase 5).

## 12. Re-capturing after a firmware update

1. `python3 tools/hw_inventory.py --out docs/descriptors` and diff the descriptors.
2. Refetch the bundle, pretty-print it (`npx prettier`, needs a 12 GB heap), and rerun
   `python3 tools/capture/class_chain.py <bundle> <class>` (find the class for the model
   name next to `case "ry5088_cx68bug_8k_002_dm":`) and diff against `commands.json`.
3. Run the shim read-only with `tools/capture/usbmon.sh` alongside, then
   `crosscheck.py <pcapng> <shim log>`.
