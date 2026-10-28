"""Small 3D helpers for the keyboard view: perspective camera, rounded outlines, convex hull.

World units are the vendor layout's units (1u key ~ 47 units ~ 19 mm). X right, Y away from
the viewer (back rows), Z up from the plate surface at Z = 0.
"""
import math
from dataclasses import dataclass

UNITS_PER_MM = 2.45      # 1u keycap (~19 mm) spans ~47 layout units


@dataclass
class Camera:
    """Perspective camera above and in front of the board, tilted by `tilt` radians from
    straight down. `dist` is the camera distance; larger = flatter perspective."""
    cx: float
    cy: float
    tilt: float = math.radians(32)
    dist: float = 1900.0

    def project(self, x, y, z):
        dy = y - self.cy
        s, c = math.sin(self.tilt), math.cos(self.tilt)
        depth = self.dist + dy * s - z * c          # distance along the view axis
        k = self.dist / depth
        return (x - self.cx) * k, -(dy * c + z * s) * k


def rounded_rect(x0, y0, x1, y1, r, z, seg=4):
    """Counter-clockwise outline of a rounded rectangle in the plane Z = z."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = []
    for (cx, cy, a0) in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a), z))
    return pts


def lerp_outline(a, b, t):
    return [(p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t) for p, q in zip(a, b)]


def convex_hull(points):
    """Andrew's monotone chain on 2D points; returns hull counter-clockwise."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


@dataclass
class KeyGeometry:
    """A keycap as a frustum: rounded base footprint, smaller rounded top, height above plate."""
    base: list            # outline at z = 0 (rest position)
    top: list             # outline at z = height (rest position)
    well: list            # slightly larger opening in the plate
    height: float

    def at_travel(self, travel_units):
        """(visible base outline clipped at the plate, top outline) after sinking by travel."""
        zb, zt = -travel_units, self.height - travel_units
        base = [(x, y, zb) for x, y, _ in self.base]
        top = [(x, y, zt) for x, y, _ in self.top]
        if zb < 0 < zt:      # the part below the plate surface is hidden inside the well
            base = lerp_outline(base, top, (0 - zb) / (zt - zb))
        return base, top


def key_geometry(x, y, w, h, grow=7.0, height=10.5):
    """Build a keycap from a vendor layout box (top-left x, y; y grows towards the viewer)."""
    x0, x1 = x - grow, x + w + grow
    # world Y: away from the viewer is positive, so the layout's front edge becomes Y min
    yf, yb = -(y + h + grow * 0.9), -(y - grow * 0.8)
    r = min(x1 - x0, yb - yf) * 0.2
    base = rounded_rect(x0, yf, x1, yb, r, 0)
    ins = min(x1 - x0, yb - yf) * 0.13
    top = rounded_rect(x0 + ins, yf + ins * 1.25, x1 - ins, yb - ins * 0.55, r * 0.75, height)
    well = rounded_rect(x0 - 1.5, yf - 1.5, x1 + 1.5, yb + 1.5, r + 1.5, 0)
    return KeyGeometry(base, top, well, height)
