"""Backlight settings: decode of the GET_LEDPARAM (0x87) reply and the matching 0x07 write.

Mirrors the vendor's getLightSetting (class Uga, bundle line ~554030):
  b[1] mode, b[2] speed (stored inverted: shown speed = MAXSPEED - b[2]), b[3] brightness,
  b[4] option: low nibble = colour (DAZZLE 8 = rainbow, NORMAL 7 = the RGB below, 0..6 = a preset),
               high nibble = direction / variant for some effects,
  b[5..7] r, g, b.
Brightness is assumed to use the same 0..4 scale as speed (MAXSPEED); not confirmed.

Writes (0x07, BIT8 checksum) follow Uga.setLightSetting: MODE_SPEC says, per mode, whether
speed and colour are sent and which direction/variant values exist (high nibble of b[4]).
0x85 (LED on/off) is not used: the page only reads it for another keyboard family, so its
meaning on this board is unconfirmed. "Off" is lighting mode 0, as the page does it.
"""
from dataclasses import dataclass, replace

from . import protocol as P

WRITES = (0x07,)

MAXSPEED = 4
MAXBRIGHT = 4
DAZZLE, NORMAL = 8, 7
PRESETS = {0: 0xFF0000, 1: 0xFF8000, 2: 0xFFFF00, 3: 0x00FF00, 4: 0x00FFFF, 5: 0x0000FF, 6: 0xFF00FF}
WAVE_DIRS = {0: "right", 1: "left", 2: "down", 3: "up"}
MODES = {
    0: "Off", 1: "Always on", 2: "Breathing", 3: "Neon", 4: "Wave", 5: "Ripple", 6: "Raindrop", 7: "Snake",
    8: "Reactive", 9: "Converge", 10: "Sine wave", 11: "Kaleidoscope", 12: "Line wave", 13: "Custom picture",
    14: "Laser", 15: "Circle wave", 16: "Dazzle", 17: "Rain down", 18: "Meteor", 19: "Reactive off",
    20: "Music 3", 21: "Screen colour", 22: "Music 2", 23: "Train", 24: "Fireworks", 25: "Custom colour",
}


# mode -> (uses speed, uses colour, {variant name: high-nibble value} or None)
MODE_SPEC = {
    0: (False, False, None),
    1: (False, True, None),
    2: (True, True, None),
    3: (True, False, None),
    4: (True, True, {"Right": 0, "Left": 1, "Down": 2, "Up": 3}),
    5: (True, True, None),
    6: (True, True, None),
    7: (True, True, {"Z pattern": 0, "Return": 1}),
    8: (True, True, None),
    9: (True, True, None),
    10: (True, True, None),
    11: (True, True, {"Outward": 0, "Inward": 1}),
    12: (True, True, {"Right": 0, "Left": 1}),
    14: (True, True, None),
    15: (True, True, {"Anti-clockwise": 0, "Clockwise": 1}),
    16: (True, True, None),
    17: (True, True, None),
    18: (True, True, None),
    19: (True, True, None),
    23: (True, True, None),
    24: (True, True, {"Right": 0, "Left": 1}),
}   # not offered: 13 user picture, 20/22 music, 21 screen colour, 25 user colour (need host software / data)


class VerifyError(Exception):
    """The keyboard does not read back what was written."""


@dataclass(frozen=True)
class Backlight:
    mode: int
    speed: int          # 0..MAXSPEED, higher is faster (vendor's displayed value)
    brightness: int
    option: int         # high nibble of b[4]
    rainbow: bool
    rgb: int            # 0xRRGGBB
    on: bool = True

    @property
    def name(self):
        return MODES.get(self.mode, f"mode {self.mode}")

    @property
    def lit(self):
        return self.on and self.mode != 0 and self.brightness > 0

    def describe(self):
        if not self.lit:
            return "off"
        parts = [self.name]
        if self.mode in (0, 3, 13, 25):
            pass
        elif self.rainbow:
            parts.append("rainbow")
        else:
            parts.append(f"#{self.rgb:06x}")
        variants = (MODE_SPEC.get(self.mode) or (0, 0, None))[2]
        if variants:
            parts.append(next((k for k, v in variants.items() if v == self.option), str(self.option)).lower())
        if self.mode not in (1, 13, 25):
            parts.append(f"speed {self.speed}/{MAXSPEED}")
        parts.append(f"brightness {min(self.brightness, MAXBRIGHT)}/{MAXBRIGHT}")
        return " · ".join(parts)


def parse(reply: bytes, on: bool = True) -> Backlight:
    """reply: the 0x87 reply (b[0] = 0x87)."""
    mode, speed_raw, bright, opt = reply[1], reply[2], reply[3], reply[4]
    rgb = (reply[5] << 16) | (reply[6] << 8) | reply[7]
    colour = opt & 0x0F
    if colour not in (DAZZLE, NORMAL) and colour in PRESETS:
        rgb = PRESETS[colour]
    if rgb == 0xFAFFFA:      # the vendor treats this as white
        rgb = 0xFFFFFF
    return Backlight(mode, max(0, MAXSPEED - speed_raw), bright, opt >> 4, colour == DAZZLE, rgb, bool(on))


def normalised(bl: Backlight) -> Backlight:
    """What the page would send for this setting (unused fields zeroed like Uga.setLightSetting)."""
    if bl.mode not in MODE_SPEC:
        raise ValueError(f"mode {bl.mode} ({bl.name}) can't be set from here")
    speed_used, colour_used, variants = MODE_SPEC[bl.mode]
    return replace(bl,
                   speed=min(max(bl.speed, 0), MAXSPEED) if speed_used else 0,
                   brightness=0 if bl.mode == 0 else min(max(bl.brightness, 0), MAXBRIGHT),
                   option=bl.option if variants and bl.option in variants.values() else 0,
                   rainbow=bl.rainbow and colour_used,
                   rgb=bl.rgb if colour_used else 0, on=True)


def encode(bl: Backlight) -> P.Cmd:
    bl = normalised(bl)
    colour_used = MODE_SPEC[bl.mode][1]
    low = (DAZZLE if bl.rainbow else NORMAL) if colour_used else NORMAL
    rgb = 0xFAFFFA if bl.rgb == 0xFFFFFF else bl.rgb       # the page never sends pure white
    return P.set_led(bl.mode, MAXSPEED - bl.speed, bl.brightness, (bl.option << 4) | low,
                     (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)


def read(dev) -> Backlight:
    return parse(dev.transact(*P.get_led()))


def apply(dev, bl: Backlight) -> Backlight:
    """Write the lighting setting and verify it by reading it back. dev must allow 0x07."""
    cmd = encode(bl)
    dev.transact(*cmd)
    dev.wait_ready()
    got = dev.transact(*P.get_led())
    if got[1:8] != cmd.msg[1:8]:
        raise VerifyError(f"lighting read back {got[1:8].hex(' ')}, wrote {cmd.msg[1:8].hex(' ')}")
    return parse(got)
