"""Config and the travel -> gamepad mapping (pure, no I/O).

A key's travel becomes 0..1 through: rest offset (calibration) + deadzone at the top,
saturation near the bottom, then a response curve. Two keys can drive one centred axis
(steering: A negative, D positive); buttons trigger at a travel depth.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import uinput as U

CONFIG_DIR = Path.home() / ".config" / "cx68"
CONFIG_FILE = CONFIG_DIR / "bridge.toml"
CALIBRATION_FILE = CONFIG_DIR / "calibration.json"
BUTTON_HYSTERESIS_MM = 0.10
KEY_ALIASES = {"Up": "↑", "Down": "↓", "Left": "←", "Right": "→"}   # legends that are hard to type

DEFAULT_CONFIG = """\
# CX68 analog gamepad bridge. Key names are the legends shown in cx68app (W, A, Space, Shift, ...).
# Edit, then restart the bridge:  systemctl --user restart cx68-bridge

[pad]
# "xbox360": appears as an Xbox 360 pad, so SDL / Proton / Steam games treat it as an XInput
#            controller with no setup.
# "generic": a plain joystick named "CX68 Analog Gamepad" for games that bind raw DirectInput axes.
style = "xbox360"

[defaults]
deadzone_mm = 0.20      # travel ignored at the top (on top of the calibrated rest position)
saturation_mm = 3.20    # travel that already counts as 100 %
curve = 1.0             # 1 linear; >1 finer near the top; <1 more sensitive early
button_mm = 1.20        # travel at which a button mapping presses

# Steering on the left stick: A left, D right.
[[axis]]
output = "left_x"
negative = "A"
positive = "D"
combine = "deeper"      # both held: "deeper" key wins, "last" pressed wins, or "sum" (cancel out)

# Throttle and brake on the triggers.
[[axis]]
output = "right_trigger"
positive = "W"

[[axis]]
output = "left_trigger"
positive = "S"
# curve = 1.5           # any [defaults] value can be overridden per axis

[[button]]
output = "a"
key = "Space"

[[button]]
output = "lb"
key = "Q"

[[button]]
output = "rb"
key = "E"

[suppress]
# Hide the mapped keys from the keyboard while the bridge runs, so W does not also type "w"
# into the game. Needs read access to the keyboard's input nodes (see udev/70-cx68.rules).
mapped_keys = false
"""

# style -> output name -> (code, minimum, maximum) for axes, code for buttons
XBOX = {
    "axes": {"left_x": (U.ABS["ABS_X"], -32768, 32767), "left_y": (U.ABS["ABS_Y"], -32768, 32767),
             "right_x": (U.ABS["ABS_RX"], -32768, 32767), "right_y": (U.ABS["ABS_RY"], -32768, 32767),
             "left_trigger": (U.ABS["ABS_Z"], 0, 255), "right_trigger": (U.ABS["ABS_RZ"], 0, 255),
             "dpad_x": (U.ABS["ABS_HAT0X"], -1, 1), "dpad_y": (U.ABS["ABS_HAT0Y"], -1, 1)},
    "buttons": {"a": U.BTN["BTN_A"], "b": U.BTN["BTN_B"], "x": U.BTN["BTN_X"], "y": U.BTN["BTN_Y"],
                "lb": U.BTN["BTN_TL"], "rb": U.BTN["BTN_TR"], "back": U.BTN["BTN_SELECT"],
                "start": U.BTN["BTN_START"], "guide": U.BTN["BTN_MODE"], "ls": U.BTN["BTN_THUMBL"],
                "rs": U.BTN["BTN_THUMBR"]},
    # same identity as the kernel's xpad driver, so SDL's built-in Xbox 360 mapping applies
    "id": dict(name="Microsoft X-Box 360 pad", vendor=0x045E, product=0x028E, version=0x0114, bustype=U.BUS_USB),
}
GENERIC = {
    "axes": {name: (U.ABS[code], -4096, 4095) for name, code in
             (("x", "ABS_X"), ("y", "ABS_Y"), ("z", "ABS_Z"), ("rx", "ABS_RX"), ("ry", "ABS_RY"), ("rz", "ABS_RZ"),
              ("throttle", "ABS_THROTTLE"), ("rudder", "ABS_RUDDER"), ("wheel", "ABS_WHEEL"), ("gas", "ABS_GAS"),
              ("brake", "ABS_BRAKE"))},
    "buttons": {f"b{i}": 0x120 + i - 1 for i in range(1, 17)},    # BTN_TRIGGER .. BTN_BASE6, BTN_DEAD..
    "id": dict(name="CX68 Analog Gamepad", vendor=0x3151, product=0xC68A, version=1, bustype=U.BUS_VIRTUAL),
}
STYLES = {"xbox360": XBOX, "generic": GENERIC}


class ConfigError(ValueError):
    pass


@dataclass
class Shape:
    deadzone_mm: float = 0.20
    saturation_mm: float = 3.20
    curve: float = 1.0


@dataclass
class AxisMap:
    output: str
    code: int
    minimum: int
    maximum: int
    positive: str | None
    negative: str | None
    combine: str
    shape: Shape


@dataclass
class ButtonMap:
    output: str
    code: int
    key: str
    button_mm: float


@dataclass
class Config:
    style: str
    axes: list[AxisMap]
    buttons: list[ButtonMap]
    suppress_mapped: bool = False

    @property
    def device_id(self):
        return STYLES[self.style]["id"]

    def all_axes(self):
        """Every axis the pad declares (the Xbox style declares all of them, mapped or not,
        so SDL's built-in mapping lines up)."""
        table = STYLES[self.style]["axes"]
        names = table if self.style == "xbox360" else {"x", "y"} | {a.output for a in self.axes}
        return [U.AbsAxis(*table[n]) for n in sorted(names, key=lambda n: table[n][0])]

    def all_buttons(self):
        table = STYLES[self.style]["buttons"]
        if self.style == "xbox360":
            return sorted(set(table.values()))
        return sorted({table["b1"]} | {b.code for b in self.buttons})   # a joystick needs at least one

    def keys(self):
        out = set()
        for a in self.axes:
            out |= {a.positive, a.negative} - {None}
        return out | {b.key for b in self.buttons}


def parse(text: str) -> Config:
    raw = tomllib.loads(text)
    style = raw.get("pad", {}).get("style", "xbox360")
    if style not in STYLES:
        raise ConfigError(f"[pad] style must be one of {sorted(STYLES)}, not {style!r}")
    table = STYLES[style]
    d = raw.get("defaults", {})
    base = Shape(d.get("deadzone_mm", 0.20), d.get("saturation_mm", 3.20), d.get("curve", 1.0))
    button_mm = d.get("button_mm", 1.20)
    axes, buttons, used = [], [], set()
    for a in raw.get("axis", []):
        out = a.get("output")
        if out not in table["axes"]:
            raise ConfigError(f"axis output {out!r} is not one of {sorted(table['axes'])} for style {style}")
        if out in used:
            raise ConfigError(f"axis output {out!r} is mapped twice")
        used.add(out)
        if not a.get("positive") and not a.get("negative"):
            raise ConfigError(f"axis {out!r} needs a positive and/or negative key")
        code, lo, hi = table["axes"][out]
        if a.get("negative") and lo >= 0:
            raise ConfigError(f"axis {out!r} has no negative side (it is a trigger)")
        combine = a.get("combine", "deeper")
        if combine not in ("deeper", "last", "sum"):
            raise ConfigError(f"axis {out!r}: combine must be deeper, last or sum")
        shape = Shape(a.get("deadzone_mm", base.deadzone_mm), a.get("saturation_mm", base.saturation_mm),
                      a.get("curve", base.curve))
        if not 0 <= shape.deadzone_mm < shape.saturation_mm <= 3.5:
            raise ConfigError(f"axis {out!r}: need 0 <= deadzone_mm < saturation_mm <= 3.5")
        if shape.curve <= 0:
            raise ConfigError(f"axis {out!r}: curve must be > 0")
        axes.append(AxisMap(out, code, lo, hi, a.get("positive"), a.get("negative"), combine, shape))
    for b in raw.get("button", []):
        out = b.get("output")
        if out not in table["buttons"]:
            raise ConfigError(f"button output {out!r} is not one of {sorted(table['buttons'])} for style {style}")
        if not b.get("key"):
            raise ConfigError(f"button {out!r} needs a key")
        buttons.append(ButtonMap(out, table["buttons"][out], b["key"], b.get("button_mm", button_mm)))
    return Config(style, axes, buttons, bool(raw.get("suppress", {}).get("mapped_keys", False)))


def load(path=CONFIG_FILE) -> Config:
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG)
    return parse(path.read_text())


@dataclass
class Calibration:
    """Per-slot rest offset and full-press depth in mm, measured by `calibrate`."""
    rest: dict = field(default_factory=dict)
    bottom: dict = field(default_factory=dict)


def amount(depth, shape: Shape, rest=0.0, bottom=None):
    """Travel in mm -> 0..1."""
    start = rest + shape.deadzone_mm
    end = shape.saturation_mm if bottom is None else min(shape.saturation_mm, bottom - 0.05)
    end = max(end, start + 0.05)
    v = (depth - start) / (end - start)
    if v <= 0:
        return 0.0
    if v >= 1:
        return 1.0
    return v ** shape.curve


class Mapper:
    """Turns a frame of per-slot travel into {(type, code): value} for the pad."""

    def __init__(self, cfg: Config, slots: dict, cal: Calibration | None = None):
        missing = sorted(k for k in cfg.keys() if k not in slots)
        if missing:
            raise ConfigError(f"unknown key name(s) {missing}; use the legends shown in cx68app")
        self.cfg = cfg
        self.slots = slots
        self.cal = cal or Calibration()
        self._pressed = {id(b): False for b in cfg.buttons}
        self._last = {}               # axis output -> side that was pressed last ("pos"/"neg")
        self._was = {}

    def _amount(self, key, depth, shape):
        s = self.slots[key]
        return amount(depth[s], shape, self.cal.rest.get(s, 0.0), self.cal.bottom.get(s))

    def __call__(self, depth) -> dict:
        out = {}
        for a in self.cfg.axes:
            p = self._amount(a.positive, depth, a.shape) if a.positive else 0.0
            n = self._amount(a.negative, depth, a.shape) if a.negative else 0.0
            if a.combine == "last":
                for side, v in (("pos", p), ("neg", n)):
                    if v > 0 and not self._was.get((a.output, side)):
                        self._last[a.output] = side
                    self._was[(a.output, side)] = v > 0
                if p > 0 and n > 0:
                    v = p if self._last.get(a.output) == "pos" else -n
                else:
                    v = p - n
            elif a.combine == "sum":
                v = p - n
            else:
                v = p if p >= n else -n
            out[(U.EV_ABS, a.code)] = round(v * a.maximum) if v >= 0 else round(-v * a.minimum)
        for b in self.cfg.buttons:
            d = depth[self.slots[b.key]]
            on = d >= (b.button_mm - BUTTON_HYSTERESIS_MM if self._pressed[id(b)] else b.button_mm)
            self._pressed[id(b)] = on
            out[(U.EV_KEY, b.code)] = out.get((U.EV_KEY, b.code), 0) | int(on)   # several keys may share a button
        return out

    def neutral(self) -> dict:
        return ({(U.EV_ABS, a.code): 0 for a in self.cfg.axes}
                | {(U.EV_KEY, b.code): 0 for b in self.cfg.buttons})
