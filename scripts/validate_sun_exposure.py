#############################################################################
# weBIGeo Tiles
# Copyright (C) 2026 Gerald Kimmersdorfer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#############################################################################
"""Synthetic checks for tile_creators/sun_exposure.py - no ALS data, no server.

    python scripts/validate_sun_exposure.py

Builds a small nested scene (1 m fine grid with a 1x1 m pole, a wall and a
house; an 8 m mid grid with a hill; a 32 m far grid with a mountain ridge) and
compares the production code against deliberately naive references:

1. util/sun.py against suncalc.js' own published test values.
2. The convex-hull sweep's horizon against a brute-force ray march from every
   receiver, over the *same* nested grids (so this tests the sweep and its
   digital lines, not the pooling).
3. Monthly sun hours and energy from the table lookups against brute-force
   per-sun-sample evaluation (ray march in each sample's exact azimuth) for a
   handful of receivers: in the open, right behind the pole, behind the wall,
   behind the house, and in the hill's shadow.
4. The tile pipeline (Web Mercator averaging, mean/std pyramid, RGB PNG
   encoding) on the synthetic result, decoded back.

Exits non-zero if any check fails. The tolerances are stated next to each
check, with the reason for them.
"""

import io
import math
import os
import sys
import tempfile
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tile_creators import sun_exposure as se  # noqa: E402
from util import sun  # noqa: E402
from util import tile_db  # noqa: E402

LAT, LON = 47.5, 13.5
MONTH = 6
# A real EPSG:3035 location in Austria for the tile pipeline check; the sweep
# itself only ever sees local coordinates.
ORIGIN_3035 = (4550000.0, 2700000.0)

failures: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(name)


# --------------------------------------------------------------------------
# Scene
# --------------------------------------------------------------------------
FINE_N = 600   # 600 m fine grid (1 m)
AREA = (200, 400, 200, 400)  # 200 m compute area in its centre
MID_HALF = 2000.0  # mid grid +-2 km around the fine grid centre (8 m)
FAR_HALF = 12000.0  # far grid +-12 km (32 m)


def height(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Analytic scene in local metres (+x east, +y north, fine grid top-left at
    (0, 0), so the fine grid spans x in [0, 600], y in [-600, 0]).
    Every feature is a max() of simple shapes, so pooled grids stay honest."""
    h = 500.0 + 0.01 * x  # gently tilted ground
    # 1x1 m pole, 25 m tall, at pixel (col 300, row 330) -> centre (300.5, -330.5)
    pole = (np.abs(x - 300.5) < 0.5) & (np.abs(y + 330.5) < 0.5)
    h = np.where(pole, h + 25.0, h)
    # east-west wall 40 m long, 1 m thick, 12 m tall, south of the pole
    wall = (x > 260) & (x < 300) & (np.abs(y + 360.5) < 0.5)
    h = np.where(wall, h + 12.0, h)
    # 10x10 m house, 8 m tall, in the apron just west of the compute area
    house = (x > 185) & (x < 195) & (y < -300) & (y > -310)
    h = np.where(house, h + 8.0, h)
    # 60 m block just north-east of the fine grid. In the tight scene of
    # check_diagonal_stretch it lies beyond the mid grid, where only diagonal
    # lines reach it - in the stretch before they enter the fine grid
    block = (x > 560) & (x < 598) & (y > 160) & (y < 198)
    h = np.where(block, h + 60.0, h)
    # hill (cone) 150 m tall, radius 400 m, ~1.3 km east -> lives in the mid grid
    r = np.hypot(x - 1600.0, y + 300.0)
    h = np.maximum(h, 500.0 + 0.01 * x + 150.0 * np.clip(1.0 - r / 400.0, 0, 1))
    # far ridge running north-south, 8 km west, 1500 m above the ground
    ridge = 500.0 + 1500.0 * np.clip(1.0 - np.abs(x + 8000.0) / 1500.0, 0, 1)
    return np.maximum(h, ridge)


def pooled_grid(left: float, top: float, n: int, res: float, sub: int) -> se._Grid:
    """Max over sub x sub samples per cell - the synthetic stand-in for
    _read_pooled's max-pooling of 1 m source pixels."""
    off = (np.arange(sub) + 0.5) / sub * res
    cx = left + np.arange(n) * res
    cy = top - np.arange(n) * res
    xs = (cx[None, :, None, None] + off[None, None, None, :])
    ys = (cy[:, None, None, None] - off[None, None, :, None])
    return se._Grid(height(xs, ys).max(axis=(2, 3)).astype(np.float32), left, top, res)


def build_scene(mid_half=MID_HALF, far_half=FAR_HALF, far_res=32.0, far_sub=4):
    fine = pooled_grid(0.0, 0.0, FINE_N, 1.0, 1)
    c = FINE_N / 2.0
    mid = pooled_grid(c - mid_half, -c + mid_half, int(2 * mid_half / 8), 8.0, 8)
    far = pooled_grid(c - far_half, -c + far_half, int(2 * far_half / far_res), far_res, far_sub)
    return fine, mid, far


# --------------------------------------------------------------------------
# Brute force: ray march over the same nested grids
# --------------------------------------------------------------------------
R_EFF = se.EARTH_RADIUS / (1.0 - se.REFRACTION_COEFF)


def _lookup(grid: se._Grid, x, y):
    """Nearest cell of `grid` at (x, y): (height, cell centre x, cell centre y),
    height NaN outside the grid."""
    c = np.floor((x - grid.left) / grid.res).astype(np.int64)
    r = np.floor((grid.top - y) / grid.res).astype(np.int64)
    ok = (r >= 0) & (c >= 0) & (r < grid.heights.shape[0]) & (c < grid.heights.shape[1])
    out = np.full(x.shape, np.nan, dtype=np.float64)
    out[ok] = grid.heights[r[ok], c[ok]]
    return out, grid.left + (c + 0.5) * grid.res, grid.top - (r + 0.5) * grid.res


def brute_tan(fine, mid, far, rx, ry, rh, theta):
    """Max tangent of the elevation angle from receivers (rx, ry, rh) along
    grid azimuth(s) theta (radians; scalar or one per receiver).

    The ray is marched in small steps - 0.25 m out to 650 m (beyond the fine
    grid's reach from any receiver), 2 m to 3 km, 8 m beyond - and every cell it passes through
    counts as an occluder at its *centre* (height and distance), the finest
    grid covering the position winning, i.e. the same nesting and the same
    "heights are point samples at cell centres" model the sweep uses. Using
    the ray point's distance with the cell's height instead would turn every
    pixel of a tilted plane into a little step and bias the reference upward
    (the first version of this script did exactly that)."""
    theta = np.broadcast_to(theta, rx.shape)
    de, dn = np.sin(theta), np.cos(theta)
    us = np.concatenate([
        np.arange(0.25, 650.0, 0.25),
        np.arange(650.0, 3000.0, 2.0),
        np.arange(3000.0, 17000.0, 8.0),
    ])
    best = np.full(rx.shape, -np.inf)
    for chunk in np.array_split(us, max(1, len(us) // 400)):
        x = rx[:, None] + de[:, None] * chunk[None, :]
        y = ry[:, None] + dn[:, None] * chunk[None, :]
        in_fine = (x >= fine.left) & (x < fine.right) & (y <= fine.top) & (y > fine.bottom)
        h, cx, cy = _lookup(fine, x, y)
        h = np.where(in_fine, h, np.nan)
        for grid in (mid, far):
            hg, gx, gy = _lookup(grid, x, y)
            use = np.isnan(h) & ~in_fine
            h = np.where(use, hg, h)
            cx = np.where(use, gx, cx)
            cy = np.where(use, gy, cy)
            in_fine = in_fine | ~np.isnan(hg)  # from here on: "claimed by a finer grid"
        dist = np.hypot(cx - rx[:, None], cy - ry[:, None])
        own = dist < 1e-6  # the receiver's own cell
        t = (h - rh[:, None]) / np.where(own, 1.0, dist) - dist / (2 * R_EFF)
        t = np.where(own | np.isnan(t), -np.inf, t)
        best = np.maximum(best, t.max(axis=1))
    return best


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_sun_position() -> None:
    # suncalc.js test/test.js: 2013-03-05 UTC at 50.5N 30.5E -> azimuth
    # -2.5003175907168385 rad (from south), true altitude -0.7000406838781611
    # rad. Our altitude is apparent (refraction added, clamped at h=0 below
    # the horizon, as in Meeus), so compare against that.
    az, alt = sun.positions(np.datetime64("2013-03-05T00:00:00"), 50.5, 30.5)
    exp_az = math.degrees(-2.5003175907168385 + math.pi) % 360.0
    exp_alt = math.degrees(-0.7000406838781611 + 0.0002967 / math.tan(0.00312536 / 0.08901179))
    check("sun position vs suncalc", abs(az - exp_az) < 1e-9 and abs(alt - exp_alt) < 1e-9,
          f"az {az:.6f} (expected {exp_az:.6f}), alt {alt:.6f} (expected {exp_alt:.6f})")


def run_sweep(fine, mid, far, directions, t_hours, t_energy, tan_dir=None):
    normals = se._surface_normals(fine)
    return se._run_sweeps(fine, mid, far, AREA, directions, t_hours, t_energy, normals,
                          lambda d, t: None, out_tan_dir=tan_dir)


def check_horizons(fine, mid, far, directions, t_hours, t_energy) -> None:
    """Hull sweep vs brute force, per receiver, for several directions.

    The two differ by construction in two places, which sets the tolerance:
    the sweep displaces a receiver by <= 0.5 px perpendicular to the line and
    walks coarse grids one cell per step, while the brute force marches the
    exact ray in sub-pixel steps. On smooth terrain that's a few hundredths of
    a degree; right next to a 1 m obstacle, a half-pixel shift can decide
    whether the ray grazes it at all, so a small fraction of receivers may
    differ a lot. Hence: median must be tiny, p95 small, and the thin
    obstacles' shadows must exist in both."""
    r0, r1, c0, c1 = AREA
    rows, cols = np.mgrid[r0:r1, c0:c1]
    rx = (cols + 0.5).ravel().astype(np.float64)
    ry = -(rows + 0.5).ravel().astype(np.float64)
    rh = fine.heights[rows, cols].ravel().astype(np.float64)

    for az in (90.0, 135.0, 180.0, 217.3, 270.0):
        k = int(np.argmin(np.abs(directions - az)))
        theta = math.radians(directions[k])
        _, _, tan_sweep = run_sweep(fine, mid, far, directions[k:k + 1], t_hours[:, k:k + 1] + 1.0,
                                    t_energy[:, k:k + 1], tan_dir=0)
        a_sweep = np.degrees(np.arctan(tan_sweep.ravel()))
        a_brute = np.degrees(np.arctan(brute_tan(fine, mid, far, rx, ry, rh, theta)))
        diff = np.abs(a_sweep - a_brute)
        med, p95, p99 = np.median(diff), np.percentile(diff, 95), np.percentile(diff, 99)
        check(f"horizon sweep vs brute force @ {directions[k]:.1f} deg",
              med < 0.05 and p95 < 0.5,
              f"|diff| median {med:.3f}, p95 {p95:.3f}, p99 {p99:.3f}, max {diff.max():.2f} deg")


def check_diagonal_stretch(directions, t_hours, t_energy) -> None:
    """The stretch of a diagonal line *before* it enters the fine grid can lie
    beyond the mid grid (with the defaults: a 4 km fine grid, lines at 45 deg
    start up to 4 km off it, the mid grid reaches only 3 km). Those samples
    must come from the far grid, not be dropped. The main scene's mid grid is
    too large to ever get there, so this uses a tight one: mid only 150 m
    beyond the fine grid, far grid at 4 m so the comparison stays within the
    resolution rule (a 32 m far grid 150 m from receivers would not be)."""
    fine, mid, far = build_scene(mid_half=450.0, far_half=1500.0, far_res=4.0, far_sub=2)
    r0, r1, c0, c1 = AREA
    rows, cols = np.mgrid[r0:r1, c0:c1]
    rx = (cols + 0.5).ravel().astype(np.float64)
    ry = -(rows + 0.5).ravel().astype(np.float64)
    rh = fine.heights[rows, cols].ravel().astype(np.float64)
    for az in (47.0, 135.0, 225.0):
        k = int(np.argmin(np.abs(directions - az)))
        _, _, tan_sweep = run_sweep(fine, mid, far, directions[k:k + 1], t_hours[:, k:k + 1] + 1.0,
                                    t_energy[:, k:k + 1], tan_dir=0)
        a_sweep = np.degrees(np.arctan(tan_sweep.ravel()))
        a_brute = np.degrees(np.arctan(brute_tan(fine, mid, far, rx, ry, rh, math.radians(directions[k]))))
        diff = np.abs(a_sweep - a_brute)
        shaded = int((a_brute > 3.0).sum())
        med, p95 = np.median(diff), np.percentile(diff, 95)
        check(f"diagonal stretch beyond the mid grid @ {directions[k]:.1f} deg",
              med < 0.05 and p95 < 0.5,
              f"|diff| median {med:.3f}, p95 {p95:.3f}, p99 {np.percentile(diff, 99):.3f} deg "
              f"({shaded} receivers with a horizon > 3 deg)")


def month_brute_force(fine, mid, far, receivers, gamma=0.0):
    """Minutes/day and Wh/m^2/day lit, per receiver, evaluating every 1-minute
    sun sample of the month with its own exact-azimuth ray march."""
    times = se._month_times(MONTH)
    n_days = len(np.unique(times.astype("datetime64[D]")))
    az, alt = sun.positions(times, LAT, LON)
    keep = alt > se.ALT_MIN_DEG
    times, az, alt = times[keep], az[keep] + gamma, alt[keep]
    doy = (times - times.astype("datetime64[Y]")).astype("timedelta64[D]").astype(np.int64) + 1
    normals = se._surface_normals(fine)
    s = sun.direction_vectors(az, alt)
    out = []
    for (r, c) in receivers:
        rh = float(fine.heights[r, c])
        n = normals[r, c]
        tan_h = brute_tan(fine, mid, far, np.full(az.shape, c + 0.5), np.full(az.shape, -(r + 0.5)),
                          np.full(az.shape, rh), np.radians(az))
        lit = alt > np.degrees(np.arctan(tan_h))
        minutes = lit.sum() * se.SUN_STEP_S / 60.0 / n_days
        # energy elevation interpolation mirrors the tables' linear one
        e_levels = np.asarray(se.ENERGY_ELEVATIONS_M)
        b = np.interp(rh, e_levels, [0, 1, 2, 3, 4])
        i0 = int(min(math.floor(b), len(e_levels) - 2))
        w = b - i0
        beam = ((1 - w) * sun.beam_normal_irradiance(alt, e_levels[i0], se.LINKE_TURBIDITY[MONTH], doy)
                + w * sun.beam_normal_irradiance(alt, e_levels[i0 + 1], se.LINKE_TURBIDITY[MONTH], doy))
        cos_i = np.maximum(s @ n, 0.0)
        energy = (beam * cos_i * lit).sum() * se.SUN_STEP_S / 3600.0 / n_days
        out.append((minutes / 60.0, energy))
    return out


def check_month(fine, mid, far, directions, t_hours, t_energy) -> tuple[np.ndarray, np.ndarray]:
    """Table lookups vs per-sample brute force for a handful of receivers.

    Tolerance: binning each sun sample to the nearest 0.5 deg direction and
    interpolating the tables in 0.1 deg altitude steps moves individual
    shadow-transition times by ~1-2 min; over a month that averages out to a
    few minutes/day at most. 0.1 h/day (6 min) and 3% energy are the bars."""
    t0 = time.time()
    hours, energy, _ = run_sweep(fine, mid, far, directions, t_hours, t_energy)
    print(f"       full sweep of {len(directions)} directions over the synthetic scene: {time.time() - t0:.1f}s")
    r0, _, c0, _ = AREA
    receivers = {
        "open ground": (250, 250),
        "5 m north of the pole": (325, 300),
        "north of the wall": (358, 280),
        "10 m east of the house": (305, 205),
        "west foot of the hill side": (300, 399),
    }
    adjacent = {"1 m north of the pole": (329, 300), "2 m north of the pole": (328, 300)}
    brute = month_brute_force(fine, mid, far, list(receivers.values()) + list(adjacent.values()))
    for (name, (r, c)), (bh, be) in zip(receivers.items(), brute):
        sh = float(hours[0, r - r0, c - c0])
        sen = float(energy[0, r - r0, c - c0])
        ok = abs(sh - bh) < 0.1 and abs(sen - be) < 0.03 * max(be, 100.0)
        check(f"month {MONTH:02d} '{name}'", ok,
              f"sweep {sh:.3f} h/day {sen:.0f} Wh/m2, brute force {bh:.3f} h/day {be:.0f} Wh/m2")

    # Not pass/fail checks - a documented resolution limit. Seen from the
    # neighbouring pixel's centre, a 1x1 m pole spans +-45 deg, but a digital
    # line only hits it within +-26.5 deg (while the line is within half a
    # pixel of the pole's column); 2 px away it's +-18.4 vs +-14 deg. The
    # sweep treats a pixel as a point on the line, the brute force as the
    # square it is. The gap closes with distance (5 m is checked above).
    for (name, (r, c)), (bh, be) in zip(adjacent.items(), brute[len(receivers):]):
        sh = float(hours[0, r - r0, c - c0])
        print(f"[INFO] month {MONTH:02d} '{name}' (known resolution limit next to 1 px obstacles): "
              f"sweep {sh:.3f} h/day, brute force {bh:.3f} h/day, diff {sh - bh:+.3f}")
    return hours, energy


def check_tiles(fine, hours) -> None:
    """3035 -> Web Mercator -> pyramid -> PNG, decoded back."""
    shifted = se._Grid(fine.heights, ORIGIN_3035[0], ORIGIN_3035[1], fine.res)
    with tempfile.TemporaryDirectory() as d:
        conn = tile_db.TileDb(os.path.join(d, "t.db"))
        n = se._write_tileset(conn, hours[0], shifted, AREA, "EPSG:3035", "hours")
        z_counts = {}
        rows = conn._conn.execute("SELECT z, x, y, data FROM tiles").fetchall()
        decoded_means = []
        z0 = np.full((1, 1, 3), se.NODATA_CODE, dtype=np.uint8)
        for row in rows:
            z_counts[row["z"]] = z_counts.get(row["z"], 0) + 1
            rgb = np.asarray(Image.open(io.BytesIO(row["data"])))
            assert rgb.shape == (se.TILE_SIZE, se.TILE_SIZE, 3), rgb.shape
            valid = rgb[..., 0] != se.NODATA_CODE
            if row["z"] == se.MAX_ZOOM:
                decoded_means.append(rgb[..., 0][valid].astype(np.float64) * se.HOURS_MAX / 254.0)
            if row["z"] == se.MIN_ZOOM:
                z0 = rgb
        dec = np.concatenate(decoded_means)
        levels_ok = all(z_counts.get(z, 0) >= 1 for z in range(se.MIN_ZOOM, se.MAX_ZOOM + 1))
        mean_ok = abs(dec.mean() - np.nanmean(hours[0])) < 0.1
        z0_valid = int((z0[..., 0] != se.NODATA_CODE).sum())
        check("tile pipeline", levels_ok and mean_ok and z0_valid == 1,
              f"{n} tiles over z{se.MIN_ZOOM}..z{se.MAX_ZOOM}, z{se.MAX_ZOOM} decoded mean {dec.mean():.3f} h/day "
              f"vs field mean {np.nanmean(hours[0]):.3f}, z0 valid pixels {z0_valid}")


def main() -> int:
    check_sun_position()

    fine, mid, far = build_scene()
    directions, alt_max = se._sweep_directions(LAT, LON, 0.0)
    n_alt = int(math.ceil((alt_max + 1.0 - se.ALT_MIN_DEG) / se.ALT_STEP_DEG)) + 1
    t_hours, t_energy = se._build_tables(MONTH, LAT, LON, 0.0, directions, n_alt)
    t_hours, t_energy = t_hours[None], t_energy[None]

    check_horizons(fine, mid, far, directions, t_hours, t_energy)
    check_diagonal_stretch(directions, t_hours, t_energy)
    hours, _ = check_month(fine, mid, far, directions, t_hours, t_energy)
    check_tiles(fine, hours)

    print()
    print("all checks passed" if not failures else f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
