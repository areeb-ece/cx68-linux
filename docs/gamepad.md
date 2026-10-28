# Analog gamepad bridge (Phase 5)

`cx68bridge` reads every key's travel (the same read-only polling as the live view, about
850 frames/s) and drives a virtual gamepad through `/dev/uinput`. By default W/S are
throttle/brake on the triggers and A/D steer on the left stick, all fully analog.

## Native gamepad mode vs uinput

The keyboard has a gamepad flag: the `9D` reply is `[9D, mode, keyboardDisable, gamepadEnable]`
and `1D` sets it. The vendor page hides the switch for this model, and turning it on is an
untested write, so the bridge doesn't depend on it. It uses uinput, which works regardless of
firmware support. Testing the native mode is still open: it needs one approved `1D` write, and
it would then appear as its own USB gamepad interface.

## Setup (CachyOS)

1. udev rules: phantom joystick fix, uinput, and the optional keyboard-access rule:
   ```fish
   cd ~/keyboard
   sudo install -m 644 udev/70-cx68.rules /etc/udev/rules.d/
   sudo udevadm control --reload
   ```
   Then unplug and replug the keyboard.
2. Check:
   ```fish
   python3 -m cx68bridge check
   ```
   It should show `/dev/uinput writable: True` and `phantom joystick: none`.
3. Service (already linked). Start and stop it from cx68app's **Gamepad** tab, or:
   ```fish
   systemctl --user link ~/keyboard/systemd/cx68-bridge.service   # first time only
   systemctl --user enable --now cx68-bridge   # run at every login (fine in allowlist mode)
   journalctl --user -u cx68-bridge -f         # what it is doing
   ```

## Who gets the gamepad

Linux grants input devices to your user, not to single programs. Anything you run (a browser,
Discord, another game) can read a pad that exists. So the bridge controls **when** the pad exists:

- **Only while one of these programs is running** (default). The bridge waits without creating
  a pad and without touching the keyboard. When a listed program starts, it creates the pad and
  starts polling. When the last listed program exits, it removes the pad and releases the
  keyboard. The defaults are `acs.exe` (the Assetto Corsa sim), `AssettoCorsa.exe` and
  `Content Manager.exe`.
- **Any program, whenever the bridge runs.**

Manage the list in cx68app → **Gamepad**: add a running program, add one by name, or remove
one. It's stored in `~/.config/cx68/access.json`, and the bridge applies changes within a
second. Programs match by file name, case-insensitively. Under Proton that's the Windows `.exe`
name. Programs that start while an allowed one is running can also see the pad; nothing below
a sandbox can prevent that.

While the pad is active, the bridge holds the keyboard, so cx68app's live view shows "busy"
until the game exits. `python3 -m cx68bridge run --always` ignores the list.

## Config: `~/.config/cx68/bridge.toml`

The file is created on first run, with comments. You can change:

- `style`:
  - `"xbox360"` is the default. SDL, Proton and Steam see a standard Xbox 360 controller (verified with SDL2).
  - `"generic"` is a plain joystick named "CX68 Analog Gamepad". Its axes are separate (`wheel`, `gas`, `brake`, `x`, `y`, …) and its buttons are `b1`…`b16`.
- `deadzone_mm`: travel ignored at the top of the stroke.
- `saturation_mm`: the travel that already counts as 100 %.
- `curve`: the response curve. 1 is linear; above 1 gives finer control near the top.
- `button_mm`: the press depth for button mappings. These can be set globally or per axis.
- `combine`: what happens when both keys of a stick axis are held. `deeper` means the deeper key wins, `last` means the last-pressed key wins (snap-tap style), and `sum` makes them cancel.
- Key names are the legends shown in `cx68app`, such as `W`, `Space` and `Shift`.

After editing: `systemctl --user restart cx68-bridge`.

**Calibration** (optional): `python3 -m cx68bridge calibrate` (with the bridge stopped). It
records each mapped key's resting reading and full-press depth. A key that bottoms out short
of `saturation_mm` still reaches 100 %, and sensor noise at rest never registers.

## Assetto Corsa (Proton) and Content Manager

1. **Steam Input.** In Steam → Assetto Corsa → Properties → Controller, choose **Disable
   Steam Input**. Otherwise Steam wraps the pad in its own virtual controller, which adds a hop and can
   show up as a second pad.
2. **Start the bridge.** With the service enabled in allowlist mode, there's nothing to do: the pad
   appears when `acs.exe` starts. Without the service, use the launch options
   `/home/areeb/keyboard/tools/with-bridge.sh %command%`
3. **Content Manager → Settings → Assetto Corsa → Controls.**
   - **Input method: Gamepad** (xbox360 style). Steering is the left stick, throttle the right
     trigger and brake the left trigger, which matches the default mapping. AC's gamepad
     options (steering speed, gamma, filter, speed sensitivity) smooth digital sticks. With
     analog keys you'll likely want little filtering; tune the travel feel with `curve` and
     `deadzone_mm` in the bridge instead.
   - **Input method: Wheel** suits the `generic` style. Bind steering, throttle and brake to
     separate axes (`wheel`, `gas`, `brake`) for full control without AC's gamepad
     assists. Not yet verified in game.
   - Bind the buttons (shift up/down, handbrake) in the same screen. The defaults are Q → LB,
     E → RB and Space → A.

## Double input

A mapped key reaches the game twice: as the gamepad axis, and as an ordinary key press.

- In **Gamepad** or **Wheel** mode, AC doesn't drive from the keyboard, so throttle and
  steering aren't duplicated. AC or Content Manager shortcuts bound to the same letters still
  fire, so check Controls → Keyboard and CM's shortcuts for W/A/S/D/Q/E/Space.
- For full suppression, set `[suppress] mapped_keys = true`. The bridge then grabs the keyboard's
  input nodes and passes every other key through a virtual keyboard. This needs the optional
  rule 4 in `udev/70-cx68.rules`; read its trade-off first. While suppression is on, the mapped
  keys don't type anywhere until the bridge stops, and the Caps Lock LED doesn't update.

## Latency

`python3 -m cx68bridge measure --seconds 20` (bridge service stopped) prints a breakdown.
With the optional rule 4 it also compares against the keyboard's native key events.

Measured 2026-10-27 (no keys pressed, so no pad reports were sent):

| Stage | Time |
|---|---|
| Polling | 844–849 frames/s; frame period p50 1.09 ms, p99 1.9 ms |
| Sample age (reading one frame) | 1.07 ms |
| Frame → mapped and written to uinput | p50 19 µs, p99 36 µs |

Driving the emit path with synthetic travel, mapping plus writing took a 4 µs median, and the
event was timestamped in the kernel 3 µs after mapping started.

So the bridge adds about 0.02 ms. End to end, the polling interval dominates: a key change
reaches the pad 1–2.2 ms after the firmware measures it.

## Troubleshooting

- `check` says busy: `cx68app`, the vendor page (shim) or an active bridge holds the keyboard.
- The pad never appears: the game's process name isn't in the list. Start the game, then use
  Gamepad → *Add running program…* to pick its exact name.
- The game sees no controller: run `python3 -m cx68bridge check` and see if the
  pad appears in `evtest` as "Microsoft X-Box 360 pad". Disable Steam Input for the game.
- The throttle sticks after an unplug: it shouldn't. The bridge zeroes every axis when the
  keyboard disconnects and keeps the pad alive, so the game doesn't lose the controller.
