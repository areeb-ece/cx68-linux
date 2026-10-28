"""Live keyboard view: perspective-rendered keycaps that sink into the plate by measured travel.

Each key is a frustum (render3d.key_geometry) seen through a perspective camera tilted ~32°.
Pressing translates the cap down along Z by its real travel; the part below the plate
surface disappears into its well, so the visible sides shrink and the top face recedes.

The whole cap is coloured by its (modelled) trigger state, see keystate.py. A slim travel line
along the bottom of a moving or selected cap shows depth, with notches at actuation (white)
and release (amber). The configured backlight glows from the wells, under the caps.

Layers: _bg (plate + wells) -> backlight (rendered at 1/GLOW_DOWNSCALE size and scaled up,
which also softens it) -> _keys (every cap at rest, transparent) -> moving caps.

Rendering goes through OpenGL (QOpenGLWidget, vsync off): the cached layers become GPU
textures, so compositing them each frame is nearly free. CX68_RASTER=1 forces the CPU path.
"""
import collections
import math
import os
import time

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
                           QSurfaceFormat)
from PySide6.QtWidgets import QToolTip, QWidget

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
    USE_GL = os.environ.get("CX68_RASTER") != "1"
except ImportError:
    USE_GL = False

from . import render3d as R
from . import theme
from .keystate import IDLE, PRESSED, RESETTING, TRAVEL
from .layout import bounds

MAX_TRAVEL_MM = 3.5
PEAK_HOLD_S = 0.6
CASE_THICKNESS = 26.0

ACCENT = QColor(theme.ACCENT)
ACCENT_HI = QColor(theme.ACCENT_HI)
WHITE = QColor("#ffffff")
STATE_COLOR = {TRAVEL: QColor("#dde6f2"), PRESSED: ACCENT, RESETTING: QColor("#f0b04f")}
STATE_NAME = {IDLE: "at rest", TRAVEL: "travelling", PRESSED: "pressed", RESETTING: "resetting"}

# backlight preview
GLOW_ALPHA = 115                    # at full brightness; the caps hide most of it
GLOW_DOWNSCALE = 5
GLOW_FPS = 60                       # backlight animation; keys repaint as fast as frames arrive
PERIOD_S = [5.0, 3.8, 2.8, 2.0, 1.4]   # one effect cycle per vendor speed 0..4
STATIC_MODES = {1, 13, 20, 21, 22, 25}  # no motion to preview (or not reproducible: picture, music, screen)


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(*(int(x + (y - x) * t) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


def hue(h, sat=0.72):
    return QColor.fromHsvF(h % 1.0, sat, 1.0)


def _rand(i):
    """Stable per-key pseudo-random number in [0, 1)."""
    return (math.sin(i * 12.9898 + 78.233) * 43758.5453) % 1.0


class KeyboardView(QOpenGLWidget if USE_GL else QWidget):
    selection_changed = Signal(object)    # set of selected slots

    def __init__(self, selectable=False, parent=None, single=False):
        super().__init__(parent)
        if USE_GL:
            fmt = QSurfaceFormat()
            fmt.setSamples(4)          # the GL paint engine antialiases through multisampling
            fmt.setSwapInterval(0)     # don't wait for vsync: render every frame that arrives
            self.setFormat(fmt)
        self.paint_times = collections.deque(maxlen=1000)
        self.selectable = selectable
        self.single = single       # clicking selects just that key
        self.bar_when_selected = True   # travel line on selected keys at rest (Keys tab); else only while moving
        self.labels = {}           # slot -> legend override (e.g. a remapped key's new action)
        self.marks = {}            # slot -> QColor rim on the top face (keys with a mode / a remap)
        self.selected = set()
        self.keys, self.geo = [], []
        self.snap = None
        self.depth = [0.0] * 128
        self.states = [IDLE] * 128
        self.peak = {}             # slot -> (mm, time)
        self.pressed_at = {}       # slot -> time it last became pressed (reactive backlight)
        self.os_pressed = set()    # slots the OS reports as held (key test)
        self.key_test = {}         # slot -> {"press": mm, "release": mm}
        self._fit = None           # (size, scale, ox, oy)
        self._hit = []             # (QPolygonF of resting top face, key)
        self._bg = self._keys = self._rest = self._lit = self._glow = None
        self._glow_dirty = True
        self._shapes = {}          # (geometry, travel in 0.01 mm) -> projected cap, see _shape
        self._rest_hull = {}       # key index -> resting silhouette (to erase it when the key moves)
        self._wells = []           # key index -> well opening on screen
        self._uv = []              # key index -> centre in 0..1 board coordinates (x right, y down)
        self._aspect = 1.0
        self._glow_timer = QTimer(self, interval=1000 // GLOW_FPS, timeout=self._glow_tick)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(640, 260)

    # data ----------------------------------------------------------------------
    def set_keys(self, keys):
        self.keys = keys
        self.geo = [R.key_geometry(k.x, k.y, k.w, k.h) for k in keys]
        x0, y0, bw, bh = bounds(keys)
        self._uv = [((k.x + k.w / 2 - x0) / bw, (k.y + k.h / 2 - y0) / bh) for k in keys]
        self._aspect = bh / bw
        self.camera = R.Camera(cx=x0 + bw / 2, cy=-(y0 + bh / 2), dist=bw * 2.4)
        m = 22.0
        self.plate = R.rounded_rect(x0 - m, -(y0 + bh + m), x0 + bw + m, -(y0 - m), 26, 0, seg=6)
        self._fit = self._rest = None
        self.update()

    def set_snapshot(self, snap):
        self.snap = snap
        self._rest = None
        bl = self._backlight()
        if bl and bl.mode not in STATIC_MODES:
            self._glow_timer.start()
        else:
            self._glow_timer.stop()
        self.update()

    def set_frame(self, depth, peak, states=None):
        changed = depth != self.depth
        self.depth = depth
        now = time.monotonic()
        if states is not None:
            for s, st in enumerate(states):
                if st == PRESSED and self.states[s] != PRESSED:
                    self.pressed_at[s] = now
            changed = changed or states != self.states
            self.states = states
        for slot, mm in enumerate(peak):
            old_mm, old_t = self.peak.get(slot, (0.0, 0.0))
            if mm > 0.05 and (mm >= old_mm or now - old_t > PEAK_HOLD_S):
                self.peak[slot] = (mm, now)
                changed = True
        if changed or any(now - t < PEAK_HOLD_S + 0.05 for _, t in self.peak.values()):
            self.update()

    def _glow_tick(self):
        self._glow_dirty = True
        self.update()

    def render_fps(self):
        now = time.monotonic()
        return sum(1 for t in self.paint_times if now - t < 1.0)

    def _backlight(self):
        bl = getattr(self.snap, "backlight", None)
        return bl if bl is not None and bl.lit else None

    # projection ----------------------------------------------------------------
    def _screen(self, pts):
        scale, ox, oy = self._fit[1:]
        out = QPolygonF()
        for x, y, z in pts:
            sx, sy = self.camera.project(x, y, z)
            out.append(QPointF(ox + sx * scale, oy + sy * scale))
        return out

    def _ensure_fit(self):
        if self._fit and self._fit[0] == (self.width(), self.height()):
            return
        pts = [self.camera.project(*p) for p in self.plate]
        pts += [self.camera.project(x, y, -CASE_THICKNESS) for x, y, _ in self.plate]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        pad = 28
        scale = min((self.width() - 2 * pad) / (max(xs) - min(xs)), (self.height() - 2 * pad) / (max(ys) - min(ys)))
        ox = (self.width() - (max(xs) - min(xs)) * scale) / 2 - min(xs) * scale
        oy = (self.height() - (max(ys) - min(ys)) * scale) / 2 - min(ys) * scale
        self._fit = ((self.width(), self.height()), scale, ox, oy)
        self._hit = [(self._screen(g.top), k) for g, k in zip(self.geo, self.keys)]
        self._shapes = {}
        self._wells = [self._screen(g.well) for g in self.geo]
        self._rest = None

    # painting ------------------------------------------------------------------
    def _order(self, indices):
        """Painter's algorithm: back rows first, then outer keys before inner ones."""
        return sorted(indices, key=lambda i: (-self.geo[i].base[0][1], -abs(self.geo[i].base[0][0] - self.camera.cx)))

    def _font(self):
        font = QFont(self.font())
        font.setPixelSize(max(8, int(self._fit[1] * 10)))
        font.setWeight(QFont.DemiBold)
        return font

    def _pixmap(self, fill):
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(fill)
        return pm

    def _build_caches(self):
        """Plate, and every cap at rest on a transparent layer; only moving keys are redrawn per frame."""
        self._bg = self._pixmap(QColor(theme.BG))
        p = QPainter(self._bg)
        p.setRenderHint(QPainter.Antialiasing)
        self._paint_plate(p)
        p.end()
        self._keys = self._pixmap(Qt.transparent)
        p = QPainter(self._keys)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self._font())
        self._rest_hull = {}
        for i in self._order(range(len(self.keys))):
            self._rest_hull[i] = self._paint_key(p, self.keys[i], self.geo[i], 0.0, rest=True)
        p.end()
        self._glow = None
        self._glow_dirty = True
        self._lit = None
        bl = self._backlight()
        if bl is None or bl.mode in STATIC_MODES:
            # static board: plate + backlight (_lit) and that + caps (_rest), one texture each
            self._lit = QPixmap(self._bg)
            p = QPainter(self._lit)
            self._draw_glow(p, 0.0)
            p.end()
            self._rest = QPixmap(self._lit)
            p = QPainter(self._rest)
            p.drawPixmap(0, 0, self._keys)
            p.end()
        else:
            self._rest = self._keys

    def _draw_glow(self, p, now):
        """Backlight, painted small (cheap, and soft once scaled up) and stretched over the board."""
        bl = self._backlight()
        if bl is None:
            return
        if self._glow is None or self._glow_dirty:
            if self._glow is None:
                self._glow = QPixmap(self.width() // GLOW_DOWNSCALE + 1, self.height() // GLOW_DOWNSCALE + 1)
            self._glow.fill(Qt.transparent)
            g = QPainter(self._glow)
            g.setRenderHint(QPainter.Antialiasing)
            g.scale(1 / GLOW_DOWNSCALE, 1 / GLOW_DOWNSCALE)
            self._paint_backlight(g, bl, now)
            g.end()
            self._glow_dirty = False
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(QRect(0, 0, self._glow.width() * GLOW_DOWNSCALE, self._glow.height() * GLOW_DOWNSCALE), self._glow)
        p.restore()

    def _draw_under(self, p, now):
        """What is underneath the caps: plate, wells and backlight."""
        if self._lit is not None:
            p.drawPixmap(0, 0, self._lit)
        else:
            p.drawPixmap(0, 0, self._bg)
            self._draw_glow(p, now)

    def _active(self, now):
        out = []
        for i, k in enumerate(self.keys):
            s = k.slot
            if s is None:
                continue
            if (self.depth[s] > 0.005 or self.states[s] != IDLE or s in self.os_pressed or s in self.selected
                    or now - self.peak.get(s, (0, 0))[1] < PEAK_HOLD_S + 0.05):
                out.append(i)
        return out

    if USE_GL:
        def paintGL(self):
            self._render()
    else:
        def paintEvent(self, _event):
            self._render()

    def _render(self):
        now = time.monotonic()
        self.paint_times.append(now)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.keys:
            p.fillRect(self.rect(), QColor(theme.BG))
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "waiting for keyboard…")
            return
        self._ensure_fit()
        if self._rest is None:
            self._build_caches()
        if self._lit is None:            # animated backlight: layers are composited every frame
            self._draw_under(p, now)
        p.drawPixmap(0, 0, self._rest)
        active = self._order(self._active(now))
        if not active:
            return
        # erase each moving key's resting image by restoring what is underneath it
        erase = QPainterPath()
        for i in active:
            erase.addPolygon(self._rest_hull[i])
        p.save()
        p.setClipPath(erase.simplified())
        self._draw_under(p, now)
        p.restore()
        p.setFont(self._font())
        for i in active:
            self._paint_key(p, self.keys[i], self.geo[i], now)

    def _paint_plate(self, p):
        top = self._screen(self.plate)
        side = self._screen([(x, y, -CASE_THICKNESS) for x, y, _ in self.plate])
        hull = QPolygonF([QPointF(*q) for q in R.convex_hull([(pt.x(), pt.y()) for pt in list(top) + list(side)])])
        # soft floor shadow
        p.setPen(Qt.NoPen)
        for i in range(6, 0, -1):
            p.setBrush(QColor(0, 0, 0, 16))
            p.drawPolygon(hull.translated(0, i * 3))
        g = QLinearGradient(hull.boundingRect().topLeft(), hull.boundingRect().bottomLeft())
        g.setColorAt(0, QColor("#0d0f13"))
        g.setColorAt(1, QColor("#07080a"))
        p.setBrush(g)
        p.setPen(QPen(QColor(0, 0, 0, 200), 1))
        p.drawPolygon(hull)
        g = QLinearGradient(top.boundingRect().topLeft(), top.boundingRect().bottomLeft())
        g.setColorAt(0, QColor("#22262f"))
        g.setColorAt(1, QColor("#171a21"))
        p.setBrush(g)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawPolygon(top)
        # key wells: dark openings the caps sink into
        for well in self._wells:
            wg = QLinearGradient(well.boundingRect().topLeft(), well.boundingRect().bottomLeft())
            wg.setColorAt(0, QColor("#030405"))
            wg.setColorAt(1, QColor("#0b0d11"))
            p.setBrush(wg)
            p.setPen(QPen(QColor(255, 255, 255, 10), 1))
            p.drawPolygon(well)

    # backlight -----------------------------------------------------------------
    def _paint_backlight(self, p, bl, now):
        """Light from the LEDs under each cap: the well glows and, once scaled up, bleeds softly onto the plate."""
        scale = min(bl.brightness, 4) / 4
        p.setPen(Qt.NoPen)
        for i, well in enumerate(self._wells):
            colour, level = self._led(bl, i, now)
            a = int(GLOW_ALPHA * scale * level)
            if a < 3:
                continue
            c = QColor(colour)
            c.setAlpha(a)
            p.setBrush(c)
            p.drawPolygon(well)

    def _led(self, bl, i, now):
        """Approximation of the effect: (colour, level 0..1) of key i at time now."""
        u, v = self._uv[i]
        ph = now / PERIOD_S[min(bl.speed, 4)]
        base = QColor((bl.rgb >> 16) & 255, (bl.rgb >> 8) & 255, bl.rgb & 255)
        colour = hue(u * 0.85) if bl.rainbow else base
        m, slot = bl.mode, self.keys[i].slot

        def wave(s, sharp=1):
            return (0.5 + 0.5 * math.cos(2 * math.pi * (s - ph))) ** sharp

        def since_press():
            if slot is None or slot not in self.pressed_at:
                return 0.0
            return 1.0 if self.states[slot] == PRESSED else math.exp(-(now - self.pressed_at[slot]) / 0.4)

        if m == 2:                                            # breathing
            return (hue(math.floor(ph) * 0.17) if bl.rainbow else base), 0.1 + 0.9 * wave(0.5)
        if m == 3:                                            # neon: whole board cycles colour
            return hue(ph * 0.5), 1.0
        if m in (4, 10, 12):                                  # waves
            s = {0: u, 1: 1 - u, 2: v, 3: 1 - v}.get(bl.option if m == 4 else 0, u)
            return (hue(s * 0.85 - ph), 1.0) if bl.rainbow else (base, 0.25 + 0.75 * wave(s))
        if m in (9, 11, 15):                                  # radial: converge, kaleidoscope, circle
            r = math.hypot(u - 0.5, (v - 0.5) * self._aspect) * 1.6
            r = -r if m == 9 else r
            return (hue(r - ph), 1.0) if bl.rainbow else (base, 0.25 + 0.75 * wave(r))
        if m in (17, 18):                                     # rain down, meteor: per-column falling streaks
            return (hue(_rand(int(u * 16))) if bl.rainbow else base), wave(v - _rand(int(u * 16)), 6)
        if m in (6, 16, 24):                                  # raindrop, dazzle, fireworks: twinkles
            return (hue(_rand(i + 7)) if bl.rainbow else base), max(0.0, math.sin(2 * math.pi * (ph + _rand(i)))) ** 4
        if m in (7, 14, 23):                                  # snake, laser, train: a band sweeping across
            return colour, wave(u + v * 0.25, 10)
        if m == 8:                                            # reactive: pressed keys light up and fade
            return colour, since_press()
        if m == 19:                                           # reactive off: pressed keys go dark
            return colour, 1.0 - since_press()
        if m == 5:                                            # ripple from each press
            level = 0.0
            for s, t0 in self.pressed_at.items():
                age = now - t0
                if age > 1.6:
                    continue
                k = next((j for j, k in enumerate(self.keys) if k.slot == s), None)
                if k is None:
                    continue
                d = math.hypot(u - self._uv[k][0], (v - self._uv[k][1]) * self._aspect)
                level = max(level, math.exp(-((d - age * (0.25 + 0.15 * bl.speed)) ** 2) / 0.004) * (1 - age / 1.6))
            return colour, level
        return colour, 1.0                                    # always on and anything not previewable

    # keys ----------------------------------------------------------------------
    def _shape(self, geo, depth):
        """Screen geometry of a cap at a travel. Travel comes in 0.01 mm steps, so there are at most
        351 shapes per key: each is projected once and reused (cleared when the view resizes)."""
        q = min(round(depth * 100), round(MAX_TRAVEL_MM * 100))
        hit = self._shapes.get((id(geo), q))
        if hit is not None:
            return hit
        travel = q / 100
        base, top = geo.at_travel(travel * R.UNITS_PER_MM)
        top_poly = self._screen(top)
        base_poly = self._screen(base)
        hull = QPolygonF([QPointF(*c) for c in R.convex_hull([(pt.x(), pt.y()) for pt in list(top_poly) + list(base_poly)])])
        above = max(0.0, geo.height - travel * R.UNITS_PER_MM)
        shadow = self._screen([(x + above * 0.25, y - above * 0.35, 0) for x, y, _ in geo.base])
        if len(self._shapes) > 30000:
            self._shapes.clear()
        self._shapes[(id(geo), q)] = out = (top_poly, hull, shadow)
        return out

    def _paint_key(self, p, k, geo, now, rest=False):
        """Draw one keycap; returns its silhouette (used to erase it later)."""
        s = k.slot
        depth = 0.0 if rest or s is None else self.depth[s]
        state = IDLE if rest or s is None else self.states[s]
        frac = min(depth, MAX_TRAVEL_MM) / MAX_TRAVEL_MM
        top_poly, hull, shadow = self._shape(geo, depth)

        # how strongly the whole cap takes its state colour
        colour = STATE_COLOR.get(state)
        strength = {TRAVEL: 0.12 + 0.35 * frac, PRESSED: 0.78, RESETTING: 0.62}.get(state, 0.0)

        # contact shadow on the plate, shrinking as the cap sinks
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 55))
        p.drawPolygon(shadow)

        # soft halo around the whole cap while it is triggered
        if state in (PRESSED, RESETTING):
            halo = QColor(colour)
            halo.setAlpha(60)
            p.setPen(QPen(halo, 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(hull)

        # sides: everything of the silhouette not covered by the top face
        br = hull.boundingRect()
        sg = QLinearGradient(br.topLeft(), br.bottomLeft())
        if colour is None:
            sg.setColorAt(0, QColor("#2b303b"))
            sg.setColorAt(1, QColor("#0e1014"))
        else:
            sg.setColorAt(0, mix(QColor("#2b303b"), colour, strength * 0.8))
            sg.setColorAt(1, mix(QColor("#0e1014"), colour, strength * 0.45))
        p.setBrush(sg)
        p.setPen(QPen(QColor(0, 0, 0, 170), 1))
        p.drawPolygon(hull)

        # top face: lit from above, in the state colour
        tr = top_poly.boundingRect()
        tg = QLinearGradient(tr.topLeft(), tr.bottomLeft())
        face_hi, face_lo = QColor("#3a404d"), QColor("#2a2f39")
        if colour is not None:
            face_hi, face_lo = mix(face_hi, colour, strength), mix(face_lo, colour, strength * 0.85)
        tg.setColorAt(0, face_hi)
        tg.setColorAt(1, face_lo)
        p.setBrush(tg)
        if colour is None:
            p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        elif state == TRAVEL:
            p.setPen(QPen(QColor(255, 255, 255, 50 + int(90 * frac)), 1))
        else:
            p.setPen(QPen(mix(colour, WHITE, 0.4), 1.2))
        p.drawPolygon(top_poly)

        mark = self.marks.get(s) if s is not None else None
        if mark is not None and colour is None:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(mark, 1.8))
            p.drawPolygon(top_poly)
        if not rest and s in self.os_pressed:          # key test: what the OS actually received
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(WHITE, 1.8))
            p.drawPolygon(hull)
        if not rest and s in self.selected:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qc(theme.ACCENT, 90), 5))
            p.drawPolygon(hull)
            p.setPen(QPen(ACCENT, 2))
            p.drawPolygon(hull)
        if s is None or self.snap is None:
            return hull
        light_face = face_hi.lightnessF() > 0.62
        ink = QColor(theme.BG) if light_face else QColor(255, 255, 255, 230)
        inner = tr.adjusted(tr.width() * 0.08, tr.height() * 0.06, -tr.width() * 0.08, -tr.height() * 0.06)
        p.setPen(ink)
        legend = self.labels.get(s, k.name)
        if legend != k.name:
            legend = p.fontMetrics().elidedText(legend, Qt.ElideRight, int(inner.width()))
        p.drawText(inner, Qt.AlignLeft | Qt.AlignTop, legend)
        if self.snap.mode_bytes[s] & 0x80:            # rapid trigger: dot at the end of the travel line
            d = max(4.0, inner.height() * 0.13)
            p.setPen(Qt.NoPen)
            p.setBrush(ink if light_face or colour is not None else ACCENT)
            p.drawEllipse(QRectF(inner.right() - d, inner.bottom() - d * 0.75 - max(2.0, inner.height() * 0.07) / 2, d, d))
        if not rest:
            if depth > 0.02:
                p.setPen(ink)
                p.drawText(inner, Qt.AlignRight | Qt.AlignTop, f"{depth:.2f}")
            if depth > 0.005 or state != IDLE or self.bar_when_selected:
                self._paint_bar(p, inner, s, depth, state, light_face, now)
        return hull

    def _paint_bar(self, p, inner, s, depth, state, light_face, now):
        """Slim travel line along the bottom of the cap: 0 mm at the left, full travel at the right."""
        sn = self.snap
        h = max(2.0, inner.height() * 0.07)
        rt_dot = max(4.0, inner.height() * 0.13) * 1.8 if sn.mode_bytes[s] & 0x80 else 0.0
        w = min(inner.width() - rt_dot, inner.height() * 2.6)   # wide keys keep a key-sized scale
        bar = QRectF(inner.left(), inner.bottom() - h, w, h)

        def x_at(mm):
            return bar.left() + bar.width() * min(max(mm, 0.0), MAX_TRAVEL_MM) / MAX_TRAVEL_MM

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(bar, h / 2, h / 2)
        if depth > 0.01:
            p.setBrush(WHITE if state in (PRESSED, RESETTING) else ACCENT_HI)
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(h, x_at(depth) - bar.left()), h), h / 2, h / 2)
        mm, t = self.peak.get(s, (0.0, 0.0))
        if mm > depth + 0.05 and now - t < PEAK_HOLD_S:
            c = QColor(theme.BG) if light_face else WHITE
            c.setAlpha(int(200 * (1 - (now - t) / PEAK_HOLD_S)))
            p.setBrush(c)
            p.drawEllipse(QPointF(x_at(mm), bar.center().y()), h * 0.55, h * 0.55)
        dark = QColor(theme.BG)
        notches = [(sn.actuation[s], dark if light_face else WHITE)]
        if not sn.mode_bytes[s] & 0x80:               # with rapid trigger, release follows the RT distances
            notches.append((sn.release[s], dark if light_face or state == RESETTING else STATE_COLOR[RESETTING]))
        for mm, c in notches:
            p.setPen(QPen(c, max(1.5, h * 0.6), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x_at(mm), bar.top() - h * 1.1), QPointF(x_at(mm), bar.bottom() + h * 1.1))

    # tooltips ------------------------------------------------------------------
    def mouseMoveEvent(self, ev):
        for poly, k in self._hit:
            if poly.containsPoint(ev.position(), Qt.OddEvenFill) and k.slot is not None and self.snap:
                s, sn = k.slot, self.snap
                test = self.key_test.get(s)
                lines = [f"{k.name}  ·  slot {s}",
                         f"actuation {sn.actuation[s]:.2f} mm  ·  release {sn.release[s]:.2f} mm",
                         f"deadzone {sn.deadzone[s]:.2f} mm  ·  rapid trigger {'on' if sn.mode_bytes[s] & 0x80 else 'off'}"
                         f" ({sn.rt_press[s]:.2f} / {sn.rt_release[s]:.2f} mm)",
                         f"now {self.depth[s]:.2f} mm, {STATE_NAME[self.states[s]]}"]
                if test:
                    lines.append(f"key test: OS press at {test.get('press', float('nan')):.2f} mm, "
                                 f"release at {test.get('release', float('nan')):.2f} mm")
                QToolTip.showText(ev.globalPosition().toPoint(), "\n".join(lines), self)
                return
        QToolTip.hideText()

    # selection -------------------------------------------------------------------
    def set_annotations(self, labels=None, marks=None):
        """Legend overrides and coloured rims; the resting board is redrawn once."""
        if labels is not None:
            self.labels = dict(labels)
        if marks is not None:
            self.marks = dict(marks)
        self._rest = None
        self.update()

    def set_selected(self, slots):
        self.selected = set(slots)
        self.selection_changed.emit(set(self.selected))
        self.update()

    def mousePressEvent(self, ev):
        if not self.selectable or ev.button() != Qt.LeftButton:
            return super().mousePressEvent(ev)
        for poly, k in self._hit:
            if poly.containsPoint(ev.position(), Qt.OddEvenFill) and k.slot is not None:
                self.set_selected({k.slot} if self.single else self.selected ^ {k.slot})
                return
