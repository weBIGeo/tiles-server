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
# Standalone utility to download BEV's ALS DTM (Airborne Laser Scanning
# digital terrain model) 1m height raster tiles for Austria.
# This is NOT wired into the tiles-server in any way - it is a scratch tool
# for pulling source data to work with locally.
#
# Data source: BEV open data catalog (free / "unentgeltlich"), GeoTIFF/COG,
# EPSG:3035 (ETRS89 / LAEA Europe):
#   https://data.bev.gv.at/geonetwork/srv/ger/catalog.search#/metadata/ec12896e-1ecd-47ad-8d48-44d236e383cc
# "Serie ALS DTM Hoehenraster 1m" - all of Austria in 55 tiles of 50x50km
# each. Tile URLs were derived from BEV's INSPIRE download service (ATOM
# feed, uuid 208cff7a-c8aa-42fe-bf4f-2b8156e37528) and follow a fixed
# pattern per PRODUCT/STICHTAG below - see tile_url().
#
# IMPORTANT - full coverage is about 246 GiB (55 tiles, 84 MB to 7.7 GB
# each depending on how much of the tile is actually inside Austria). See
# SELECTED_TILES below: that's the knob to only fetch the tile(s) you have
# room for and actually need. Use `list` to see the full catalog (size +
# WGS84 bbox per tile) and cross-reference against the official overview to
# figure out which tile(s) cover your area of interest:
#   https://data.bev.gv.at/download/ALS/ALS_Kacheluebersicht.pdf
#   https://data.bev.gv.at/download/ALS/ALS_Kacheluebersicht.zip  (shapefile)
#
# Usage examples:
#   python util/fetch_bev_als_dtm.py list
#   python util/fetch_bev_als_dtm.py download --out data/bev_als_dtm
#   python util/fetch_bev_als_dtm.py download --out data/bev_als_dtm --all   # all 55 tiles, ~246 GiB!
#############################################################################

import argparse
import os
import sys

import requests

# --------------------------------------------------------------------------
# Which tiles `download` actually fetches. This is the primary knob for this
# script - edit it to match your storage budget. Each entry is a tile id
# "N<northing>E<easting>" (EPSG:3035, 50km grid); run `list` to see every
# tile's size and WGS84 bounding box and pick the ones covering your area.
#
# None = every tile in TILE_CATALOG (~246 GiB total, see module docstring).
# --------------------------------------------------------------------------
SELECTED_TILES: list[str] | None = [
    "N2800000E4750000",  # Vienna area, ~7.34 GiB
]

PRODUCT = "DTM"          # "DTM" (bare ground) or "DSM" (surface incl. buildings/vegetation)
STICHTAG = "15.09.2025"  # which vintage of the yearly "Serie" to fetch, as "DD.MM.YYYY"

OUT_DIR_DEFAULT = "data/bev_als_dtm"

BASE_URL = "https://data.bev.gv.at/download/ALS"

# Snapshot of the full tile catalog, taken from the live ATOM feed on
# 2026-07-16: tile id -> (size_bytes, south, west, north, east) in WGS84
# degrees. Sizes/bboxes are for the DTM/15.09.2025 series specifically; if
# you change PRODUCT or STICHTAG above, treat these as approximate (`list
# --verify-sizes` re-checks the actual sizes for the tiles you've selected
# via HTTP HEAD, no download).
TILE_CATALOG: dict[str, tuple[int, float, float, float, float]] = {
    "N2550000E4650000": (130139476, 45.951, 14.243, 46.428, 14.927),
    "N2600000E4300000": (903000507, 46.513, 9.727, 46.963, 10.381),
    "N2600000E4350000": (2139636328, 46.508, 10.377, 46.963, 11.037),
    "N2600000E4400000": (335969339, 46.500, 11.028, 46.959, 11.693),
    "N2600000E4450000": (484196082, 46.488, 11.679, 46.950, 12.349),
    "N2600000E4500000": (4858413607, 46.472, 12.329, 46.938, 13.005),
    "N2600000E4550000": (5993070801, 46.452, 12.979, 46.922, 13.660),
    "N2600000E4600000": (6794760446, 46.428, 13.629, 46.902, 14.315),
    "N2600000E4650000": (6680677141, 46.400, 14.278, 46.877, 14.969),
    "N2600000E4700000": (3459396709, 46.368, 14.927, 46.849, 15.622),
    "N2600000E4750000": (1186173003, 46.333, 15.575, 46.817, 16.275),
    "N2650000E4250000": (1275729506, 46.963, 9.068, 47.410, 9.722),
    "N2650000E4300000": (6977385743, 46.963, 9.724, 47.413, 10.384),
    "N2650000E4350000": (7743564388, 46.959, 10.381, 47.413, 11.046),
    "N2650000E4400000": (7301909228, 46.950, 11.037, 47.409, 11.708),
    "N2650000E4450000": (6785192987, 46.938, 11.693, 47.400, 12.369),
    "N2650000E4500000": (7744367147, 46.922, 12.349, 47.388, 13.031),
    "N2650000E4550000": (7784986168, 46.902, 13.005, 47.371, 13.692),
    "N2650000E4600000": (7635593294, 46.877, 13.660, 47.351, 14.352),
    "N2650000E4650000": (7620182258, 46.849, 14.315, 47.327, 15.012),
    "N2650000E4700000": (8049840906, 46.817, 14.969, 47.298, 15.671),
    "N2650000E4750000": (7264911948, 46.781, 15.622, 47.266, 16.329),
    "N2650000E4800000": (1051263840, 46.741, 16.275, 47.229, 16.986),
    "N2700000E4250000": (309891515, 47.413, 9.060, 47.860, 9.720),
    "N2700000E4300000": (1232264822, 47.413, 9.722, 47.863, 10.387),
    "N2700000E4350000": (1755799372, 47.409, 10.384, 47.863, 11.055),
    "N2700000E4400000": (1398997056, 47.400, 11.046, 47.859, 11.723),
    "N2700000E4450000": (4324871029, 47.388, 11.708, 47.850, 12.390),
    "N2700000E4500000": (4449509798, 47.371, 12.369, 47.838, 13.057),
    "N2700000E4550000": (7981893543, 47.351, 13.031, 47.821, 13.724),
    "N2700000E4600000": (8089893982, 47.327, 13.692, 47.800, 14.390),
    "N2700000E4650000": (8152550101, 47.298, 14.352, 47.776, 15.055),
    "N2700000E4700000": (8051124192, 47.266, 15.012, 47.747, 15.720),
    "N2700000E4750000": (7820697536, 47.229, 15.671, 47.714, 16.384),
    "N2700000E4800000": (2462946148, 47.189, 16.329, 47.677, 17.047),
    "N2750000E4500000": (2055954064, 47.821, 12.390, 48.287, 13.084),
    "N2750000E4550000": (7333052003, 47.800, 13.057, 48.270, 13.757),
    "N2750000E4600000": (7635737451, 47.776, 13.724, 48.250, 14.429),
    "N2750000E4650000": (7990803922, 47.747, 14.390, 48.225, 15.100),
    "N2750000E4700000": (8085052174, 47.714, 15.055, 48.196, 15.771),
    "N2750000E4750000": (7942963345, 47.677, 15.720, 48.163, 16.441),
    "N2750000E4800000": (6463739370, 47.637, 16.384, 48.125, 17.109),
    "N2750000E4850000": (201601137, 47.592, 17.047, 48.084, 17.777),
    "N2800000E4500000": (87634481, 48.270, 12.411, 48.737, 13.112),
    "N2800000E4550000": (2910984741, 48.250, 13.084, 48.720, 13.791),
    "N2800000E4600000": (6460122708, 48.225, 13.757, 48.699, 14.469),
    "N2800000E4650000": (7345713438, 48.196, 14.429, 48.674, 15.146),
    "N2800000E4700000": (7687480995, 48.163, 15.100, 48.644, 15.822),
    "N2800000E4750000": (7878343422, 48.125, 15.771, 48.611, 16.498),
    "N2800000E4800000": (5273429740, 48.084, 16.441, 48.573, 17.173),
    "N2850000E4600000": (311752429, 48.674, 13.791, 49.148, 14.509),
    "N2850000E4650000": (2158819151, 48.644, 14.469, 49.122, 15.193),
    "N2850000E4700000": (4708983586, 48.611, 15.146, 49.093, 15.875),
    "N2850000E4750000": (3045140355, 48.573, 15.822, 49.059, 16.557),
    "N2850000E4800000": (2026642737, 48.532, 16.498, 49.021, 17.238),
}


def _selected_tile_ids() -> list[str]:
    if SELECTED_TILES is None:
        return sorted(TILE_CATALOG)
    unknown = [t for t in SELECTED_TILES if t not in TILE_CATALOG]
    if unknown:
        raise SystemExit(f"Unknown tile id(s) in SELECTED_TILES: {unknown}. Run `list` for valid ids.")
    return list(SELECTED_TILES)


def tile_url(tile_id: str) -> str:
    d, m, y = STICHTAG.split(".")
    stichtag_path = f"{y}{m}{d}"
    return f"{BASE_URL}/{PRODUCT}/{stichtag_path}/ALS_{PRODUCT}_CRS3035RES50000m{tile_id}.tif"


def _fmt_gib(num_bytes: int) -> str:
    return f"{num_bytes / 1024**3:.2f} GiB"


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------
def list_tiles(verify_sizes: bool) -> None:
    selected = set(_selected_tile_ids())
    total_selected = 0
    total_all = 0
    print(f"{'':2} {'tile id':22} {'size':>10}  {'bbox (south, west, north, east)':38}")
    for tile_id in sorted(TILE_CATALOG):
        size, south, west, north, east = TILE_CATALOG[tile_id]
        if verify_sizes and tile_id in selected:
            size = head_size(tile_url(tile_id))
        mark = "x" if tile_id in selected else " "
        if tile_id in selected:
            total_selected += size
        total_all += size
        print(f"[{mark}] {tile_id:22} {_fmt_gib(size):>10}  {south:.3f}, {west:.3f}, {north:.3f}, {east:.3f}")
    print()
    print(f"selected: {len(selected)}/{len(TILE_CATALOG)} tiles, {_fmt_gib(total_selected)}")
    print(f"full catalog: {_fmt_gib(total_all)}")


def head_size(url: str) -> int:
    r = requests.head(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return int(r.headers["Content-Length"])


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------
def download_tile(tile_id: str, out_dir: str, force: bool) -> None:
    url = tile_url(tile_id)
    dst = os.path.join(out_dir, os.path.basename(url))
    remote_size = head_size(url)

    if not force and os.path.exists(dst) and os.path.getsize(dst) == remote_size:
        print(f"{tile_id}: already downloaded ({_fmt_gib(remote_size)}), skipping")
        return

    print(f"{tile_id}: {url} -> {dst} ({_fmt_gib(remote_size)})")
    tmp = dst + ".part"
    resume_from = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    mode = "ab" if resume_from else "wb"

    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        if resume_from and r.status_code != 206:
            resume_from = 0  # server ignored the Range request, start over
            mode = "wb"
        r.raise_for_status()
        written = resume_from
        next_report = written + 500 * 1024**2
        with open(tmp, mode) as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                written += len(chunk)
                if written >= next_report:
                    print(f"  {_fmt_gib(written)} / {_fmt_gib(remote_size)}")
                    next_report = written + 500 * 1024**2

    os.replace(tmp, dst)
    print(f"{tile_id}: done ({_fmt_gib(os.path.getsize(dst))})")


def download_tiles(out_dir: str, force: bool) -> None:
    tile_ids = _selected_tile_ids()
    total = sum(TILE_CATALOG[t][0] for t in tile_ids)
    print(f"downloading {len(tile_ids)} tile(s), {_fmt_gib(total)} total, into {out_dir}/")
    os.makedirs(out_dir, exist_ok=True)
    for tile_id in tile_ids:
        download_tile(tile_id, out_dir, force)
    print(f"done. {len(tile_ids)} tile(s) in {out_dir}/")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Download BEV ALS DTM 1m height raster tiles for Austria.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="show the tile catalog (size + bbox) and which tiles SELECTED_TILES picks")
    pl.add_argument("--verify-sizes", action="store_true",
                     help="re-check the size of selected tiles via HTTP HEAD instead of using the snapshot")

    pd = sub.add_parser("download", help="download the tiles listed in SELECTED_TILES (edit the constant first!)")
    pd.add_argument("--out", default=OUT_DIR_DEFAULT)
    pd.add_argument("--all", action="store_true", help="ignore SELECTED_TILES and download all 55 tiles (~246 GiB!)")
    pd.add_argument("--force", action="store_true", help="re-download even if a matching-size file already exists")

    args = p.parse_args()

    if args.cmd == "list":
        list_tiles(args.verify_sizes)
    elif args.cmd == "download":
        global SELECTED_TILES
        if args.all:
            SELECTED_TILES = None
        download_tiles(args.out, args.force)


if __name__ == "__main__":
    main()
