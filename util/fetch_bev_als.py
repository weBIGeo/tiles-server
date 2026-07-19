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
# Standalone utility to download BEV's ALS DTM (bare ground) / DSM (surface,
# incl. buildings/vegetation) 1m height raster tiles for Austria.
# This is NOT wired into the tiles-server in any way - it is a scratch tool
# for pulling source data to work with locally.
#
# Data source: BEV open data catalog (free / "unentgeltlich"), GeoTIFF,
# EPSG:3035 (ETRS89 / LAEA Europe), "Serie 15.09.2025":
#   DTM: https://data.bev.gv.at/geonetwork/srv/ger/catalog.search#/metadata/ec12896e-1ecd-47ad-8d48-44d236e383cc
#   DSM: https://data.bev.gv.at/geonetwork/srv/ger/catalog.search#/metadata/5848e256-511d-452b-a74d-db758917a1e9
# All of Austria in 55 tiles of 50x50km each, see ALS_Kacheluebersicht.pdf:
#   https://data.bev.gv.at/download/ALS/ALS_Kacheluebersicht.pdf
#
# The downloaded GeoTIFFs don't carry a valid CRS tag, so each tile is
# stamped with EPSG:3035 after download, via rasterio.
#
# Usage:
#   python util/fetch_bev_als.py --product DTM --out D:\DEM\AUT\DTM
#   python util/fetch_bev_als.py --product DSM --out D:\DEM\AUT\DSM --tiles N2800000E4750000
#############################################################################

import argparse
import os

import rasterio
import requests
from rasterio.crs import CRS

TILE_IDS = [
    "N2550000E4650000",
    "N2600000E4300000", "N2600000E4350000", "N2600000E4400000", "N2600000E4450000",
    "N2600000E4500000", "N2600000E4550000", "N2600000E4600000", "N2600000E4650000",
    "N2600000E4700000", "N2600000E4750000",
    "N2650000E4250000", "N2650000E4300000", "N2650000E4350000", "N2650000E4400000",
    "N2650000E4450000", "N2650000E4500000", "N2650000E4550000", "N2650000E4600000",
    "N2650000E4650000", "N2650000E4700000", "N2650000E4750000", "N2650000E4800000",
    "N2700000E4250000", "N2700000E4300000", "N2700000E4350000", "N2700000E4400000",
    "N2700000E4450000", "N2700000E4500000", "N2700000E4550000", "N2700000E4600000",
    "N2700000E4650000", "N2700000E4700000", "N2700000E4750000", "N2700000E4800000",
    "N2750000E4500000", "N2750000E4550000", "N2750000E4600000", "N2750000E4650000",
    "N2750000E4700000", "N2750000E4750000", "N2750000E4800000", "N2750000E4850000",
    "N2800000E4500000", "N2800000E4550000", "N2800000E4600000", "N2800000E4650000",
    "N2800000E4700000", "N2800000E4750000", "N2800000E4800000",
    "N2850000E4600000", "N2850000E4650000", "N2850000E4700000", "N2850000E4750000",
    "N2850000E4800000",
]

BASE_URL = "https://data.bev.gv.at/download/ALS"


def tile_url(product: str, tile_id: str) -> str:
    return f"{BASE_URL}/{product}/20250915/ALS_{product}_CRS3035RES50000m{tile_id}.tif"


def stamp_crs(path: str) -> None:
    with rasterio.open(path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as ds:
        ds.crs = CRS.from_epsg(3035)


def download_tile(product: str, tile_id: str, out_dir: str) -> str:
    """Download one tile (skipping if already present) and return its local path."""
    url = tile_url(product, tile_id)
    dst = os.path.join(out_dir, os.path.basename(url))
    if os.path.exists(dst):
        print(f"{tile_id}: exists, skipping")
        return dst
    print(f"{tile_id}: {url} -> {dst}")
    tmp = dst + ".part"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
    os.replace(tmp, dst)
    stamp_crs(dst)
    print(f"{tile_id}: done")
    return dst


def download_tiles(product: str, tile_ids: list[str], out_dir: str) -> list[str]:
    """Download several tiles into out_dir, returning their local paths in the
    same order as tile_ids. Used both by main() and by notebooks (e.g.
    dtm_fetch_merge_compressor.ipynb) that need a set of tiles fetched
    programmatically rather than via the CLI."""
    os.makedirs(out_dir, exist_ok=True)
    return [download_tile(product, tile_id, out_dir) for tile_id in tile_ids]


def main() -> None:
    p = argparse.ArgumentParser(description="Download BEV ALS DTM/DSM 1m height raster tiles for Austria.")
    p.add_argument("--product", required=True, choices=["DTM", "DSM"])
    p.add_argument("--out", required=True)
    p.add_argument("--tiles", nargs="+", choices=TILE_IDS, metavar="TILE_ID",
                    help="tile ids to download; omit to download all 55")
    args = p.parse_args()

    tile_ids = args.tiles or TILE_IDS
    download_tiles(args.product, tile_ids, args.out)
    print(f"done. {len(tile_ids)} tile(s) in {args.out}/")


if __name__ == "__main__":
    main()
