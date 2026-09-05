"""Logo generator for the AstrBot plugin 'astrbot_plugin_anamnesis'.

Concept -- "an inward spiral built out of graph nodes":

  * nodes strung along a spiral arm   -> knowledge-graph memory (nodes / relations)
  * the arm coils inwards and thickens as it goes -> recall / anamnesis: retracing
    the way back to something the soul already knows (Plato)
  * outer end thin, small, translucent -> decay & forgetting
  * inner end thick, large, bright     -> reflect & consolidate
  * one warm amber core the arms plunge into, the only warm accent in the mark
    -> the recalled memory itself, a cold/warm focal contrast

Technique: every shape is an analytic antialiased coverage mask, composited with
numpy at SS x supersampling and LANCZOS-downsampled to 512 px.  Deterministic and
easy to re-tune: edit the specs in VARIANTS and re-run.

    python assets/logo/generate_logo.py

Outputs
    <repo>/logo.png                      512x512 final (transparent outside badge)
    assets/logo/logo-512.png             copy of the final, next to the source
    assets/logo/logo-128.png             thumbnail
    assets/logo/logo-64.png              thumbnail (hard legibility gate)
    assets/logo/drafts/<name>-512|64.png every variant, for comparison
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent          # <repo>/assets/logo
REPO = HERE.parent.parent                       # <repo>
DRAFTS = HERE / "drafts"

SIZE = 512          # final edge length
SS = 4              # supersampling factor
CENTER = SIZE / 2.0
BADGE_R = 240.0     # badge radius -> 16 px safety margin on every side
THUMBS = (128, 64)

# ---------------------------------------------------------------- palette ----
BG_IN = "#1d3b36"       # badge centre  (upstream #5f7f79 family, much deeper)
BG_OUT = "#142c29"      # badge rim
RIM = "#5f9f92"         # badge outline
FAR = "#3b7a6f"         # decayed / faint end of the cold ramp
NEAR = "#a6f6d8"        # consolidated / bright mint end of the cold ramp
CORE = "#e8b45f"        # the single warm focus
GLOW = "#efba6a"


def rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32) / 255.0


def mix(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    t = float(min(max(t, 0.0), 1.0))
    return a * (1.0 - t) + b * t


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def badge_color(x: float, y: float) -> np.ndarray:
    """Colour of the badge gradient at a point (used for knockout haloes)."""
    d = math.hypot(x - CENTER, y - CENTER)
    return mix(rgb(BG_IN), rgb(BG_OUT), float(smoothstep(d / BADGE_R)))


class Canvas:
    """Float RGB buffer with analytic antialiased primitives (logical coords)."""

    def __init__(self, size: int = SIZE, ss: int = SS, base: str = BG_OUT):
        self.ss = ss
        self.n = size * ss
        self.buf = np.empty((self.n, self.n, 3), dtype=np.float32)
        self.buf[:] = rgb(base)                     # avoids dark fringing later
        self.ax = (np.arange(self.n, dtype=np.float32) + 0.5) / ss

    # -- helpers -------------------------------------------------------------
    def _win(self, cx: float, cy: float, reach: float):
        x0 = max(0, int(math.floor((cx - reach) * self.ss)))
        x1 = min(self.n, int(math.ceil((cx + reach) * self.ss)) + 1)
        y0 = max(0, int(math.floor((cy - reach) * self.ss)))
        y1 = min(self.n, int(math.ceil((cy + reach) * self.ss)) + 1)
        return x0, x1, y0, y1

    def _dist(self, win, cx: float, cy: float) -> np.ndarray:
        x0, x1, y0, y1 = win
        dx = self.ax[x0:x1] - cx
        dy = self.ax[y0:y1] - cy
        return np.sqrt(dx[None, :] ** 2 + dy[:, None] ** 2)

    def _edge(self, signed: np.ndarray) -> np.ndarray:
        """Signed distance (inside > 0) -> one-subpixel-wide coverage ramp."""
        return np.clip(signed * self.ss + 0.5, 0.0, 1.0)

    def _blend(self, win, cov: np.ndarray, color) -> None:
        x0, x1, y0, y1 = win
        a = cov[..., None]
        sub = self.buf[y0:y1, x0:x1]
        sub *= 1.0 - a
        sub += a * color

    # -- primitives ----------------------------------------------------------
    def badge(self, r: float, c_in: str, c_out: str) -> None:
        win = self._win(CENTER, CENTER, r + 2)
        d = self._dist(win, CENTER, CENTER)
        t = smoothstep(d / r)[..., None]
        color = rgb(c_in) * (1.0 - t) + rgb(c_out) * t
        self._blend(win, self._edge(r - d), color)

    def disc(self, cx: float, cy: float, r: float, color, alpha: float = 1.0) -> None:
        win = self._win(cx, cy, r + 2)
        self._blend(win, self._edge(r - self._dist(win, cx, cy)) * alpha, color)

    def grad_disc(self, cx: float, cy: float, r: float, c_in, c_out,
                  alpha: float = 1.0, power: float = 1.0) -> None:
        win = self._win(cx, cy, r + 2)
        d = self._dist(win, cx, cy)
        t = (np.clip(d / r, 0.0, 1.0) ** power)[..., None]
        self._blend(win, self._edge(r - d) * alpha, c_in * (1.0 - t) + c_out * t)

    def ring(self, cx: float, cy: float, r: float, w: float, color,
             alpha: float = 1.0) -> None:
        win = self._win(cx, cy, r + w + 2)
        d = self._dist(win, cx, cy)
        self._blend(win, self._edge(w * 0.5 - np.abs(d - r)) * alpha, color)

    def glow(self, cx: float, cy: float, r: float, color, alpha: float,
             power: float = 2.4) -> None:
        win = self._win(cx, cy, r + 2)
        d = self._dist(win, cx, cy)
        self._blend(win, np.clip(1.0 - d / r, 0.0, 1.0) ** power * alpha, color)

    def segment(self, p, q, w: float, color, alpha: float = 1.0) -> None:
        """Round-capped straight edge of constant width."""
        (px, py), (qx, qy) = p, q
        win = self._win((px + qx) / 2, (py + qy) / 2,
                        math.hypot(qx - px, qy - py) / 2 + w + 2)
        x0, x1, y0, y1 = win
        vx, vy = qx - px, qy - py
        vv = max(vx * vx + vy * vy, 1e-6)
        gx = self.ax[x0:x1][None, :] - px
        gy = self.ax[y0:y1][:, None] - py
        t = np.clip((gx * vx + gy * vy) / vv, 0.0, 1.0)
        d = np.sqrt((gx - t * vx) ** 2 + (gy - t * vy) ** 2)
        self._blend(win, self._edge(w * 0.5 - d) * alpha, color)

    def taper_stroke(self, samples, c_far, c_near, a_far: float, a_near: float) -> None:
        """Stroke smooth paths whose width / colour / opacity ramp along them.

        'samples' yields (x, y, half_width, e) with e in [0, 1] rising from the
        decayed outer tip to the consolidated inner end.  Coverage is unioned into
        one mask, so overlapping stamps never compound their opacity, and the
        whole set of arms is composited in a single pass.
        """
        cov = np.zeros((self.n, self.n), dtype=np.float32)
        ev = np.zeros((self.n, self.n), dtype=np.float32)
        for x, y, hw, e in samples:
            win = self._win(x, y, hw + 2)
            x0, x1, y0, y1 = win
            c = self._edge(hw - self._dist(win, x, y))
            sub = cov[y0:y1, x0:x1]
            np.maximum(sub, c, out=sub)
            ev[y0:y1, x0:x1][c > 0.0] = e       # e rises -> brighter stamp wins
        ramp = ev[..., None]
        color = c_far * (1.0 - ramp) + c_near * ramp
        alpha = (cov * (a_far + (a_near - a_far) * ev))[..., None]
        self.buf *= 1.0 - alpha
        self.buf += alpha * color

    # -- export --------------------------------------------------------------
    def finish(self, r: float = BADGE_R) -> Image.Image:
        arr = np.clip(self.buf, 0.0, 1.0)
        img = Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8), "RGB")
        img = img.resize((SIZE, SIZE), Image.LANCZOS)
        ax = np.arange(SIZE, dtype=np.float32) + 0.5
        d = np.sqrt((ax[None, :] - CENTER) ** 2 + (ax[:, None] - CENTER) ** 2)
        mask = np.clip(r + 0.5 - d, 0.0, 1.0)      # crisp circular badge edge
        out = img.convert("RGBA")
        out.putalpha(Image.fromarray((mask * 255.0 + 0.5).astype(np.uint8), "L"))
        return out


# ------------------------------------------------------------------ specs ----
@dataclass
class Spec:
    """One design draft.  Angles in degrees, lengths in 512-px logical units."""

    arms: int = 3                   # spiral arms, evenly spaced
    nodes: int = 4                  # graph nodes per arm
    a0: float = 96.0                # heading of the outermost tip
    sweep: float = -168.0           # negative -> coils clockwise inwards
    r_out: float = 202.0            # radius of the outer tip
    r_in: float = 90.0              # radius of the innermost node
    t_max: float = 1.0              # how far the spiral is traced (1.0 = last node)
    straight: bool = False          # True -> straight graph edges, False -> arcs
    node_q: float = 1.0             # >1 spreads the nodes out towards the core
    r_pow: float = 0.0              # 0 -> log spiral, >0 -> Archimedean r**r_pow
    w_out: float = 4.0              # arm width at the decayed tip
    w_in: float = 19.0              # arm width where it meets the core
    node_out: float = 8.0
    node_in: float = 21.0
    a_far: float = 0.28             # opacity of the most decayed end
    a_near: float = 1.0
    gamma: float = 0.78             # shapes the size / brightness ramp
    core_r: float = 50.0
    knockout: float = 1.8           # badge-coloured gap drawn around each node
    branches: tuple = ()            # (node_i, angle_off, dist, r_scale, a_scale)


def polar(r: float, deg: float):
    a = math.radians(deg)
    return CENTER + r * math.cos(a), CENTER - r * math.sin(a)


def render(spec: Spec) -> Image.Image:
    c_far, c_near = rgb(FAR), rgb(NEAR)
    decay = spec.r_in / spec.r_out
    ease = lambda t: min(max(t, 0.0), 1.0) ** spec.gamma
    width = lambda e: spec.w_out + (spec.w_in - spec.w_out) * e
    node_r = lambda e: spec.node_out + (spec.node_in - spec.node_out) * e
    alpha = lambda e: spec.a_far + (spec.a_near - spec.a_far) * e
    if spec.r_pow > 0.0:        # Archimedean: evenly spaced whorls -> reads as a coil
        radius = lambda t: spec.r_out - (spec.r_out - spec.r_in) * t ** spec.r_pow
    else:                       # logarithmic: whorls crowd towards the centre
        radius = lambda t: spec.r_out * decay ** t
    arm_at = lambda t, arm: polar(radius(t), spec.a0 + arm + spec.sweep * t)
    node_t = lambda i: spec.t_max * (i / (spec.nodes - 1)) ** spec.node_q
    offsets = [i * 360.0 / spec.arms for i in range(spec.arms)]

    cv = Canvas()
    cv.badge(BADGE_R, BG_IN, BG_OUT)
    cv.ring(CENTER, CENTER, BADGE_R - 1.8, 3.4, rgb(RIM), 0.5)
    cv.glow(CENTER, CENTER, spec.core_r * 3.6, rgb(GLOW), 0.13, 2.6)

    # Edges.  Every arm ends at its innermost node and then sends one short radial
    # spoke into the core; the big inner node caps that junction, so there is no
    # visible corner and no tail wrapping around the core into a fake orbit ring.
    samples = []
    for arm in offsets:
        if spec.straight:                               # straight graph edges
            ts = [node_t(i) for i in range(spec.nodes)]
            path = [arm_at(t, arm) for t in ts]
            evs = [ease(t) for t in ts]
        else:                                           # smooth spiral edges
            path, evs = [], []
            for k in range(int(spec.t_max * 700) + 1):
                t = k / 700.0
                path.append(arm_at(t, arm))
                evs.append(ease(t))
        path.append((CENTER, CENTER))                   # the spoke into the core
        evs.append(1.0)
        for i in range(len(path) - 1):
            (px, py), (qx, qy) = path[i], path[i + 1]
            n = max(2, int(math.hypot(qx - px, qy - py) * 3.0))
            for k in range(n + 1):
                f = k / n
                e = evs[i] + (evs[i + 1] - evs[i]) * f
                samples.append((px + (qx - px) * f, py + (qy - py) * f,
                                width(e) / 2.0, e))
    cv.taper_stroke(samples, c_far, c_near, spec.a_far, spec.a_near)

    for arm in offsets:
        pts, es = [], []
        for i in range(spec.nodes):
            t = node_t(i)
            pts.append(arm_at(t, arm))
            es.append(ease(t))

        # side branches: peripheral memories, already fading out
        for i, off, dist, rs, a_s in spec.branches:
            nx, ny = pts[i]
            base = math.degrees(math.atan2(CENTER - ny, nx - CENTER))
            ang = math.radians(base + off)
            sx, sy = nx + dist * math.cos(ang), ny - dist * math.sin(ang)
            col = mix(c_far, c_near, es[i] * 0.5)
            a = alpha(es[i]) * a_s
            nr = node_r(es[i]) * rs
            cv.segment((nx, ny), (sx, sy), max(4.0, nr * 0.6), col, a * 0.8)
            cv.disc(sx, sy, nr + spec.knockout, badge_color(sx, sy), 0.95)
            cv.disc(sx, sy, nr, col, a)

        for (px, py), e in zip(pts, es):
            # a thin badge-coloured gap keeps every node readable on top of the arm
            if spec.knockout > 0.0:
                cv.disc(px, py, node_r(e) + spec.knockout, badge_color(px, py), 0.72)
            cv.disc(px, py, node_r(e), mix(c_far, c_near, 0.12 + 0.88 * e),
                    min(1.0, alpha(e) * 1.1))

    r = spec.core_r
    cv.glow(CENTER, CENTER, r * 2.2, rgb(GLOW), 0.34, 1.9)
    cv.disc(CENTER, CENTER, r, rgb(CORE))   # deliberately flat -- no sphere shading
    return cv.finish()


VARIANTS = {
    # Why these numbers: the badge is a circle, so a strand only reads as a
    # spiral if its *pitch* is steep.  Pitch = atan(radial travel / arc length).
    # Earlier drafts swept 260-320 deg over ~130 px of radius -> pitch ~13 deg,
    # i.e. almost parallel to the rim -> read as an orbit.  Here each arm spends
    # only ~120 deg covering the same radius -> pitch ~27 deg, unmistakably a
    # coil diving at the core, and three of them make a pinwheel with wide empty
    # wedges, so no ray of the badge ever crosses two whorls (no nested rings).

    # A -- 3 arms x 3 nodes = 9 nodes, snappy ramp
    "a-vortex": Spec(
        arms=3, nodes=3,
        a0=96.0, sweep=-116.0,
        r_out=206.0, r_in=80.0,
        w_out=3.2, w_in=16.5,
        node_out=6.0, node_in=19.5,
        a_far=0.22, gamma=0.60,
        core_r=44.0, knockout=2.2,
    ),
    # A2 -- same skeleton, more even bead progression and a roomier core
    "a2-vortex": Spec(
        arms=3, nodes=3,
        a0=96.0, sweep=-120.0,
        r_out=206.0, r_in=84.0,
        w_out=3.4, w_in=16.0,
        node_out=6.6, node_in=19.0,
        a_far=0.26, gamma=0.85,
        core_r=46.0, knockout=2.4,
    ),
    # A3 -- 3 arms x 4 nodes = 12 nodes: a longer chain of lifecycle stages
    "a3-vortex-12": Spec(
        arms=3, nodes=4,
        a0=96.0, sweep=-140.0,
        r_out=210.0, r_in=84.0,
        w_out=2.6, w_in=15.5,
        node_out=5.0, node_in=18.5,
        a_far=0.18, gamma=0.85,
        core_r=45.0, knockout=2.2,
    ),
}

FINAL = "a2-vortex"     # chosen after reviewing every draft at 64 px

def save(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    print(f"  {path}  {path.stat().st_size / 1024:.1f} KB  {img.size[0]}px")


def main() -> None:
    rendered = {}
    print("drafts:")
    for name, spec in VARIANTS.items():
        img = render(spec)
        rendered[name] = img
        save(img, DRAFTS / f"{name}-512.png")
        save(img.resize((64, 64), Image.LANCZOS), DRAFTS / f"{name}-64.png")

    final = rendered[FINAL]
    print(f"final ({FINAL}):")
    save(final, REPO / "logo.png")
    save(final, HERE / "logo-512.png")
    for s in THUMBS:
        save(final.resize((s, s), Image.LANCZOS), HERE / f"logo-{s}.png")


if __name__ == "__main__":
    main()
