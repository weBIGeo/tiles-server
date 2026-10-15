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
#
# Builds the all-Austria BEV ALS height mosaic. See docs/als_mosaic.md for the
# measurements behind every choice made here.
#
#   sources      Download missing BEV tiles into SOURCE_DIR and write a float32
#                EPSG:3035 VRT over the untouched originals - one seamless
#                raster, each shared edge row/column taken from exactly one tile.
#   webmercator  Source VRT -> EPSG:3857 mosaic on the exact z17 tile grid, one
#                int32-centimetre GeoTIFF per z10 tile, plus a VRT over them that
#                unscales to float32 metres, so tile creators can window-read it.
#   verify       Read-only check of the 3857 mosaic against the source VRT,
#                through tile_creators/als_normals.py's own warp and normals.
#
# Usage (from the repo root):
#   python scripts/build_als_mosaic.py sources --product DTM
#   python scripts/build_als_mosaic.py webmercator --product DTM
#   python scripts/build_als_mosaic.py verify --product DTM
#############################################################################

import argparse
import glob
import math
import os
import random
import re
import sys
import time
from xml.sax.saxutils import escape

import numpy as np
import rasterio
import rasterio.warp
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.windows import Window

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import fetch_bev_als  # noqa: E402
from tile_creators import als_normals  # noqa: E402

# Where the BEV originals live (and stay - they are never modified or deleted),
# and where the compressed 3857 mosaic goes. Both overridable per run.
SOURCE_DIR = r"D:\DEM\AUT\{product}"
MOSAIC_DIR = os.path.join(REPO_ROOT, "data", "als_mosaic", "{product}", "3857_z17")

# Mosaic storage: int32 heights in units of HEIGHT_STEP metres. 1 cm costs the
# z17 normals 0.087 deg mean / 0.39 deg max - under the 8-bit encoding's own
# 0.26 / 0.55 deg (see docs).
HEIGHT_STEP = 0.01
INT_NODATA = np.iinfo(np.int32).min
# What the VRTs present to readers for nodata - matches the BEV originals.
FLOAT_NODATA = -9999.0

ZSTD_LEVEL = 9
BLOCK = 512

# BEV grid: pixel *centres* lie on the 50 km grid, so a tile is 50001 px wide and
# shares its last row/column with the south/east neighbour.
BEV_TILE_M = fetch_bev_als.TILE_SIZE_M
BEV_TILE_PX = BEV_TILE_M + 1
TILE_ID_RE = re.compile(r"(N\d+E\d+)\.tif$")

# Web Mercator mosaic: the exact TARGET_ZOOM tile grid, one file per CHUNK_ZOOM tile.
TARGET_ZOOM = 17
CHUNK_ZOOM = 10
TILE_SIZE = 256
CHUNK_PX = TILE_SIZE * 2 ** (TARGET_ZOOM - CHUNK_ZOOM)
# Destination window per reproject call - bounds memory (~64 MB float32 each).
# Also the same window size als_normals warps with (one BLOCK_ZOOM block), so
# GDAL's slightly window-dependent warp output behaves the same in both.
WARP_WINDOW = 4096
RESAMPLING = Resampling.bilinear  # same as als_normals.RESAMPLING at z17

GTIFF_PROFILE = dict(
    driver="GTiff", count=1, dtype="int32", nodata=INT_NODATA,
    compress="zstd", zstd_level=ZSTD_LEVEL, predictor=2,
    tiled=True, blockxsize=BLOCK, blockysize=BLOCK,
    bigtiff="IF_SAFER", num_threads="ALL_CPUS",
)


# --------------------------------------------------------------------------
# VRT writing
# --------------------------------------------------------------------------
def write_vrt(vrt_path: str, crs: CRS, transform: rasterio.Affine, width: int, height: int,
              sources: list[dict], scale: float | None = None) -> None:
    """Hand-built single-band Float32 VRT (rasterio has no BuildVRT and osgeo
    isn't a dependency). Each source dict has path, full_w/full_h (the file's
    size), src_w/src_h (read from its top-left corner), x_off/y_off
    (destination pixel offset), dtype and nodata. Source nodata is
    transparent, so uncovered pixels keep FLOAT_NODATA; `scale` turns stored
    integers into metres. Source paths are absolute, since the VRT and its
    sources may sit on different drives."""
    scale_xml = f"\n      <ScaleOffset>0</ScaleOffset>\n      <ScaleRatio>{scale}</ScaleRatio>" if scale else ""
    parts = []
    for s in sources:
        parts.append(f"""    <ComplexSource>
      <SourceFilename relativeToVRT="0">{escape(os.path.abspath(s["path"]))}</SourceFilename>
      <SourceBand>1</SourceBand>
      <SourceProperties RasterXSize="{s["full_w"]}" RasterYSize="{s["full_h"]}" DataType="{s["dtype"]}" BlockXSize="{BLOCK}" BlockYSize="{BLOCK}"/>
      <SrcRect xOff="0" yOff="0" xSize="{s["src_w"]}" ySize="{s["src_h"]}"/>
      <DstRect xOff="{s["x_off"]}" yOff="{s["y_off"]}" xSize="{s["src_w"]}" ySize="{s["src_h"]}"/>
      <NODATA>{s["nodata"]}</NODATA>{scale_xml}
    </ComplexSource>""")
    t = transform
    xml = f"""<VRTDataset rasterXSize="{width}" rasterYSize="{height}">
  <SRS dataAxisToSRSAxisMapping="1,2">{escape(crs.to_wkt())}</SRS>
  <GeoTransform>{t.c!r}, {t.a!r}, 0.0, {t.f!r}, 0.0, {t.e!r}</GeoTransform>
  <VRTRasterBand dataType="Float32" band="1">
    <NoDataValue>{FLOAT_NODATA}</NoDataValue>
{chr(10).join(parts)}
  </VRTRasterBand>
</VRTDataset>
"""
    tmp = vrt_path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml)
    os.replace(tmp, vrt_path)


# --------------------------------------------------------------------------
# sources: BEV originals -> 3035 VRT
# --------------------------------------------------------------------------
def parse_tile_id(tile_id: str) -> tuple[int, int]:
    """'N2650000E4500000' -> (northing, easting) of the SW corner."""
    n, e = tile_id[1:].split("E")
    return int(n), int(e)


def neighbour_id(tile_id: str, dn: int, de: int) -> str:
    n, e = parse_tile_id(tile_id)
    return f"N{n + dn * BEV_TILE_M}E{e + de * BEV_TILE_M}"


def source_files(source_dir: str, product: str) -> dict[str, str]:
    """tile_id -> path of every BEV original present in source_dir."""
    found = {}
    for p in glob.glob(os.path.join(source_dir, f"ALS_{product}_CRS3035RES50000m*.tif")):
        m = TILE_ID_RE.search(os.path.basename(p))
        if m:
            found[m.group(1)] = p
    return found


def source_vrt_path(source_dir: str, product: str) -> str:
    return os.path.join(source_dir, f"austria_{product}_3035.vrt")


def check_shared_edges(files: dict[str, str]) -> None:
    """Report whether each tile's last column/row is identical to its east/
    south neighbour's first. Report only - the VRT takes the shared edge from
    the neighbour either way."""
    for tile_id, path in sorted(files.items()):
        for label, dn, de in (("east", 0, 1), ("south", -1, 0)):
            nid = neighbour_id(tile_id, dn, de)
            if nid not in files:
                continue
            mine_win = Window(BEV_TILE_PX - 1, 0, 1, BEV_TILE_PX) if de else Window(0, BEV_TILE_PX - 1, BEV_TILE_PX, 1)
            their_win = Window(0, 0, 1, BEV_TILE_PX) if de else Window(0, 0, BEV_TILE_PX, 1)
            with rasterio.open(path) as a, rasterio.open(files[nid]) as b:
                mine, theirs = a.read(1, window=mine_win).ravel(), b.read(1, window=their_win).ravel()
                mine_ok = np.isfinite(mine) & (mine != a.nodata)
                theirs_ok = np.isfinite(theirs) & (theirs != b.nodata)
            both = mine_ok & theirs_ok
            diff = np.abs(mine[both].astype(np.float64) - theirs[both])
            identical = int((diff == 0).sum())
            mismatch = int((mine_ok != theirs_ok).sum())
            ok = identical == both.sum() and mismatch == 0
            print(
                f"  {tile_id} | {label} {nid}: {both.sum()} px valid in both, "
                + ("identical" if ok else
                   f"{identical} identical, max diff {diff.max() if diff.size else 0:.4f} m, "
                   f"validity mismatches {mismatch}  <-- NOT IDENTICAL")
            )


def write_source_vrt(source_dir: str, product: str, files: dict[str, str]) -> str:
    """Float32 EPSG:3035 VRT over the originals. A tile contributes its top
    row and left column always, its last row/column only where the south/east
    neighbour is absent - so every pixel comes from exactly one file and the
    VRT is gapless at every stage of a partial download. The CRS is set here
    explicitly: the BEV files themselves only carry a LOCAL_CS."""
    ids = sorted(files)
    sw = [parse_tile_id(t) for t in ids]
    # Grid of pixel *centres* on whole metres -> edges at .5.
    left = min(e for _, e in sw) - 0.5
    top = max(n for n, _ in sw) + BEV_TILE_M + 0.5
    right = max(e for _, e in sw) + BEV_TILE_M + 0.5
    bottom = min(n for n, _ in sw) - 0.5
    sources = []
    for tile_id, (n, e) in zip(ids, sw):
        sources.append(dict(
            path=files[tile_id], dtype="Float32", nodata=FLOAT_NODATA,
            full_w=BEV_TILE_PX, full_h=BEV_TILE_PX,
            src_w=BEV_TILE_PX - (neighbour_id(tile_id, 0, 1) in files),
            src_h=BEV_TILE_PX - (neighbour_id(tile_id, -1, 0) in files),
            x_off=round(e - 0.5 - left), y_off=round(top - (n + BEV_TILE_M + 0.5)),
        ))
    vrt = source_vrt_path(source_dir, product)
    write_vrt(vrt, CRS.from_epsg(3035), rasterio.Affine(1.0, 0.0, left, 0.0, -1.0, top),
              round(right - left), round(top - bottom), sources)
    return vrt


def cmd_sources(args: argparse.Namespace) -> None:
    source_dir = args.dir or SOURCE_DIR.format(product=args.product)
    tile_ids = args.tiles or fetch_bev_als.TILE_IDS
    if not args.no_download:
        fetch_bev_als.download_tiles(args.product, tile_ids, source_dir)

    files = source_files(source_dir, args.product)
    if not files:
        raise SystemExit(f"no ALS_{args.product}_*.tif in {source_dir}")
    for tile_id, path in sorted(files.items()):
        with rasterio.open(path) as ds:
            n, e = parse_tile_id(tile_id)
            expected = rasterio.Affine(1.0, 0.0, e - 0.5, 0.0, -1.0, n + BEV_TILE_M + 0.5)
            if ds.shape != (BEV_TILE_PX, BEV_TILE_PX) or not ds.transform.almost_equals(expected) or ds.dtypes[0] != "float32":
                raise SystemExit(f"{path}: unexpected grid {ds.shape} {tuple(ds.transform)[:6]} {ds.dtypes[0]}")
    print(f"{len(files)} source tile(s) in {source_dir}, checking shared edges:")
    check_shared_edges(files)
    vrt = write_source_vrt(source_dir, args.product, files)
    print(f"done. VRT: {vrt}")


# --------------------------------------------------------------------------
# webmercator: source VRT -> 3857 z17 mosaic
# --------------------------------------------------------------------------
def quantize(heights: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """float metres -> int32 HEIGHT_STEP units, INT_NODATA where not valid.
    Rounded in float64: float32 at ~4e5 cm is too coarse to round exactly."""
    q = np.rint(heights.astype(np.float64) / HEIGHT_STEP)
    return np.where(valid, q, INT_NODATA).astype(np.int32)


def covered_chunks(tile_ids: list[str]) -> list[tuple[int, int]]:
    """CHUNK_ZOOM tiles that can hold data from any source tile. Same idea as
    als_normals._covered_blocks: bbox in 3857 for candidates, then reject the
    corners of the rotated 3035 quad by checking overlap back in 3035."""
    chunks = set()
    for tile_id in tile_ids:
        n, e = parse_tile_id(tile_id)
        sb = (e - 0.5, n - 0.5, e + BEV_TILE_M + 0.5, n + BEV_TILE_M + 0.5)
        left, bottom, right, top = rasterio.warp.transform_bounds("EPSG:3035", "EPSG:3857", *sb, densify_pts=21)
        x0, y0 = als_normals._point_to_tile_3857(left, top, CHUNK_ZOOM)
        x1, y1 = als_normals._point_to_tile_3857(right, bottom, CHUNK_ZOOM)
        for cy in range(y0, y1 + 1):
            for cx in range(x0, x1 + 1):
                minx, miny, maxx, maxy = rasterio.warp.transform_bounds(
                    "EPSG:3857", "EPSG:3035", *als_normals._tile_bounds_3857(CHUNK_ZOOM, cx, cy), densify_pts=21
                )
                if maxx <= sb[0] or minx >= sb[2] or maxy <= sb[1] or miny >= sb[3]:
                    continue
                chunks.add((cx, cy))
    return sorted(chunks, key=lambda c: (c[1], c[0]))


def chunk_path(out_dir: str, product: str, cx: int, cy: int) -> str:
    return os.path.join(out_dir, f"ALS_{product}_z{CHUNK_ZOOM}_{cx}_{cy}.tif")


def warp_chunk(src: rasterio.DatasetReader, product: str, cx: int, cy: int, out_dir: str) -> str:
    dst = chunk_path(out_dir, product, cx, cy)
    bounds = als_normals._tile_bounds_3857(CHUNK_ZOOM, cx, cy)
    transform = from_bounds(*bounds, CHUNK_PX, CHUNK_PX)
    tmp = dst + ".part"
    profile = dict(GTIFF_PROFILE, width=CHUNK_PX, height=CHUNK_PX, crs=CRS.from_epsg(3857), transform=transform)
    valid_px = 0
    with rasterio.open(tmp, "w", **profile) as out:
        out.scales = (HEIGHT_STEP,)
        out.offsets = (0.0,)
        out.units = ("m",)
        # Row-major over block-aligned windows: every output block is written
        # exactly once, which is what keeps a compressed GeoTIFF unfragmented.
        for row in range(0, CHUNK_PX, WARP_WINDOW):
            for col in range(0, CHUNK_PX, WARP_WINDOW):
                win = Window(col, row, WARP_WINDOW, WARP_WINDOW)
                buf = np.full((WARP_WINDOW, WARP_WINDOW), np.nan, dtype=np.float32)
                rasterio.warp.reproject(
                    source=rasterio.band(src, 1),
                    destination=buf,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=rasterio.windows.transform(win, transform),
                    dst_crs="EPSG:3857",
                    dst_nodata=np.nan,
                    resampling=RESAMPLING,
                    num_threads=os.cpu_count() or 1,
                )
                ok = np.isfinite(buf)
                valid_px += int(ok.sum())
                out.write(quantize(buf, ok), 1, window=win)

    if valid_px == 0:
        os.remove(tmp)
        return "empty"
    os.replace(tmp, dst)
    return f"{os.path.getsize(dst) / 1e9:.2f} GB, {100 * valid_px / CHUNK_PX ** 2:.1f}% valid"


def write_mosaic_vrt(out_dir: str, product: str) -> str:
    vrt = os.path.join(out_dir, f"austria_{product}_3857_z{TARGET_ZOOM}.vrt")
    coords = []
    for p in glob.glob(os.path.join(out_dir, f"ALS_{product}_z{CHUNK_ZOOM}_*_*.tif")):
        cx, cy = map(int, os.path.basename(p)[:-4].split("_")[-2:])
        coords.append((cx, cy, p))
    if not coords:
        return vrt
    x0, y0 = min(c[0] for c in coords), min(c[1] for c in coords)
    x1, y1 = max(c[0] for c in coords), max(c[1] for c in coords)
    left, _, _, top = als_normals._tile_bounds_3857(CHUNK_ZOOM, x0, y0)
    px = (2 * als_normals.WEBMERCATOR_ORIGIN / 2 ** CHUNK_ZOOM) / CHUNK_PX
    sources = [dict(path=p, dtype="Int32", nodata=INT_NODATA, full_w=CHUNK_PX, full_h=CHUNK_PX,
                    src_w=CHUNK_PX, src_h=CHUNK_PX, x_off=(cx - x0) * CHUNK_PX, y_off=(cy - y0) * CHUNK_PX)
               for cx, cy, p in sorted(coords)]
    write_vrt(vrt, CRS.from_epsg(3857), rasterio.Affine(px, 0.0, left, 0.0, -px, top),
              (x1 - x0 + 1) * CHUNK_PX, (y1 - y0 + 1) * CHUNK_PX, sources, scale=HEIGHT_STEP)
    return vrt


def cmd_webmercator(args: argparse.Namespace) -> None:
    source_dir = args.sources or SOURCE_DIR.format(product=args.product)
    out_dir = args.out or MOSAIC_DIR.format(product=args.product)
    src_vrt = source_vrt_path(source_dir, args.product)
    if not os.path.exists(src_vrt):
        raise SystemExit(f"{src_vrt} not found - run `sources` first")
    os.makedirs(out_dir, exist_ok=True)

    chunks = covered_chunks(sorted(source_files(source_dir, args.product)))
    print(f"{len(chunks)} z{CHUNK_ZOOM} chunk(s) of {CHUNK_PX}x{CHUNK_PX} px to cover {src_vrt}")
    with rasterio.open(src_vrt) as src:
        for i, (cx, cy) in enumerate(chunks):
            if os.path.exists(chunk_path(out_dir, args.product, cx, cy)):
                print(f"[{i + 1}/{len(chunks)}] z{CHUNK_ZOOM}/{cx}/{cy}: exists, skipping")
                continue
            t0 = time.monotonic()
            result = warp_chunk(src, args.product, cx, cy, out_dir)
            print(f"[{i + 1}/{len(chunks)}] z{CHUNK_ZOOM}/{cx}/{cy}: {result} in {time.monotonic() - t0:.0f}s")
            write_mosaic_vrt(out_dir, args.product)
    print(f"done. VRT: {write_mosaic_vrt(out_dir, args.product)}")


# --------------------------------------------------------------------------
# verify: 3857 mosaic vs. source VRT, through als_normals
# --------------------------------------------------------------------------
def _altitude_factor(by: int) -> np.ndarray:
    """Per-row 1/cos(lat) als_normals._read_block_heights applies - so its
    output can be compared with raw mosaic heights and vice versa."""
    _, _, _, top = als_normals._tile_bounds_3857(als_normals.BLOCK_ZOOM, 0, by)
    px = (2 * als_normals.WEBMERCATOR_ORIGIN / 2 ** als_normals.BLOCK_ZOOM) / als_normals.BLOCK_PX
    ys = top + px * als_normals.APRON - (np.arange(als_normals.APRON_PX, dtype=np.float64) + 0.5) * px
    lat = 2.0 * np.arctan(np.exp(ys / als_normals.EARTH_RADIUS)) - math.pi / 2.0
    return (1.0 / np.cos(lat))[:, None]


def _angle_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan2(np.linalg.norm(np.cross(a, b), axis=-1), (a * b).sum(-1)))


def cmd_verify(args: argparse.Namespace) -> None:
    if als_normals.MAX_ZOOM != TARGET_ZOOM:
        raise SystemExit(f"als_normals.MAX_ZOOM={als_normals.MAX_ZOOM} but the mosaic is z{TARGET_ZOOM}")
    source_dir = args.sources or SOURCE_DIR.format(product=args.product)
    out_dir = args.mosaic or MOSAIC_DIR.format(product=args.product)
    src_vrt = source_vrt_path(source_dir, args.product)
    mosaic_vrt = os.path.join(out_dir, f"austria_{args.product}_3857_z{TARGET_ZOOM}.vrt")
    if not os.path.exists(src_vrt) or not os.path.exists(mosaic_vrt):
        raise SystemExit(f"need {src_vrt} and {mosaic_vrt}")

    tile_ids = set(source_files(source_dir, args.product))
    seam_x = {parse_tile_id(t)[1] for t in tile_ids if neighbour_id(t, 0, -1) in tile_ids}
    seam_y = {parse_tile_id(t)[0] for t in tile_ids if neighbour_id(t, -1, 0) in tile_ids}

    with rasterio.open(src_vrt) as ref, rasterio.open(mosaic_vrt) as mos:
        blocks = als_normals._covered_blocks(ref)

        def straddles(b):
            minx, miny, maxx, maxy = rasterio.warp.transform_bounds(
                "EPSG:3857", "EPSG:3035", *als_normals._tile_bounds_3857(als_normals.BLOCK_ZOOM, *b), densify_pts=21
            )
            return any(minx < x < maxx for x in seam_x) or any(miny < y < maxy for y in seam_y)

        rng = random.Random(42)
        seam = [b for b in blocks if straddles(b)]
        seam_set = set(seam)
        other = [b for b in blocks if b not in seam_set]
        picked = rng.sample(seam, min(len(seam), args.samples // 2))
        picked += rng.sample(other, min(len(other), args.samples - len(picked)))
        print(f"{len(blocks)} z{als_normals.BLOCK_ZOOM} block(s), {len(seam)} straddle a 3035 seam; checking {len(picked)}")

        all_dh, all_ang = [], []
        for bx, by in picked:
            t0 = time.monotonic()
            corr = _altitude_factor(by)
            # (a) reference: exactly what als_normals does today, warping the sources.
            h_ref = als_normals._read_block_heights(ref, bx, by)
            # (b) mosaic: a plain window read - what a creator would do.
            left, bottom, right, top = als_normals._tile_bounds_3857(als_normals.BLOCK_ZOOM, bx, by)
            px = (right - left) / als_normals.BLOCK_PX
            col = round((left - mos.transform.c) / px) - als_normals.APRON
            row = round((mos.transform.f - top) / px) - als_normals.APRON
            raw = mos.read(1, window=Window(col, row, als_normals.APRON_PX, als_normals.APRON_PX),
                           boundless=True, fill_value=FLOAT_NODATA).astype(np.float64)
            raw[raw == FLOAT_NODATA] = np.nan
            h_mos = (raw * corr).astype(np.float32)

            ref_ok, mos_ok = np.isfinite(h_ref), np.isfinite(h_mos)
            both = ref_ok & mos_ok
            dh = np.abs(h_ref[both].astype(np.float64) / np.broadcast_to(corr, h_ref.shape)[both] - raw[both])
            v_ref, v_mos = als_normals._stencil_valid(h_ref), als_normals._stencil_valid(h_mos)
            n_ref = als_normals._block_normals(h_ref, v_ref)
            n_mos = als_normals._block_normals(h_mos, v_mos)
            ang = _angle_deg(n_ref[v_ref & v_mos], n_mos[v_ref & v_mos])
            all_dh.append(dh)
            all_ang.append(ang)
            tag = "seam" if (bx, by) in seam_set else "    "
            print(
                f"  z{als_normals.BLOCK_ZOOM}/{bx}/{by} {tag}: valid ref {ref_ok.mean() * 100:5.1f}% "
                f"mosaic {mos_ok.mean() * 100:5.1f}%, voids in mosaic where ref valid: {int((ref_ok & ~mos_ok).sum())}, "
                f"dh max {dh.max() * 100 if dh.size else 0:.2f} cm mean {dh.mean() * 100 if dh.size else 0:.3f} cm, "
                f"normal diff mean {ang.mean() if ang.size else 0:.4f} p99 "
                f"{np.percentile(ang, 99) if ang.size else 0:.4f} max {ang.max() if ang.size else 0:.3f} deg "
                f"({time.monotonic() - t0:.1f}s)"
            )

        dh = np.concatenate(all_dh)
        ang = np.concatenate(all_ang)
        if dh.size:
            print(
                f"overall: dh max {dh.max() * 100:.2f} cm mean {dh.mean() * 100:.3f} cm; normal diff "
                f"mean {ang.mean():.4f} p99 {np.percentile(ang, 99):.4f} max {ang.max():.3f} deg"
            )


# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Build the all-Austria BEV ALS height mosaic (see docs/als_mosaic.md).")
    sub = p.add_subparsers(dest="cmd", required=True)
    products = ["DTM", "DSM"]

    s = sub.add_parser("sources", help="download missing BEV tiles and write the EPSG:3035 source VRT")
    s.add_argument("--product", required=True, choices=products)
    s.add_argument("--dir", help=f"source directory (default: {SOURCE_DIR})")
    s.add_argument("--tiles", nargs="+", choices=fetch_bev_als.TILE_IDS, metavar="TILE_ID",
                   help="tile ids to download; omit for all 55. The VRT always covers every tile present.")
    s.add_argument("--no-download", action="store_true", help="only (re)write the VRT over what is present")
    s.set_defaults(func=cmd_sources)

    w = sub.add_parser("webmercator", help=f"source VRT -> EPSG:3857 z{TARGET_ZOOM} int32-cm mosaic + VRT")
    w.add_argument("--product", required=True, choices=products)
    w.add_argument("--sources", help=f"source directory (default: {SOURCE_DIR})")
    w.add_argument("--out", help=f"mosaic directory (default: {MOSAIC_DIR})")
    w.set_defaults(func=cmd_webmercator)

    v = sub.add_parser("verify", help="read-only: compare the 3857 mosaic with the source VRT")
    v.add_argument("--product", required=True, choices=products)
    v.add_argument("--sources", help=f"source directory (default: {SOURCE_DIR})")
    v.add_argument("--mosaic", help=f"mosaic directory (default: {MOSAIC_DIR})")
    v.add_argument("--samples", type=int, default=6, help="z13 blocks to check (half of them on seams)")
    v.set_defaults(func=cmd_verify)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
