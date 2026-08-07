# exolabs / COSMOS snow data — access notes

Findings from probing the exolabs COSMOS service for snow-depth data over the Alps.
Companion to the standalone tool [`scripts/fetch_snow_cover.py`](../scripts/fetch_snow_cover.py).

- Official docs: <https://exolabs-ch.gitbook.io/cosmos>
- Credentials live in the git-ignored `config.py` (placeholders in `config.example.py`).

## TL;DR

exolabs publishes the **same Alps-wide product** through two completely separate
paths that return **different things**:

| Path | What you get | Format | Use it for |
|------|--------------|--------|------------|
| **WMS / XYZ tiles** | pre-rendered *picture* of the snow depth | 256×256 colormap PNG, EPSG:3857 | quick visual overlay |
| **S3 GeoTIFF** | the raw snow-height (HS) values | single GeoTIFF per product, overwritten daily | own styling, reprojection, re-tiling |

**Recommendation for weBIGeo:** use the **raw S3 GeoTIFFs**. The tiles lock you into
exolabs' colormap and expose no numbers; the GeoTIFF gives real values you can crop,
reproject and re-tile into this server's scheme.

## WMS / XYZ tiles

Custom token-based API on `https://p20.cosmos-project.ch`, HTTP Basic auth
(user `tuwien`).

1. `GET /urlrequest` → JSON catalog of products, each with an opaque `url` token:

   ```json
   {"snowdepth_map": [
     {"name": "snowdepth map - alps",                "url": "BfOlLX…"},
     {"name": "snowdepth map - alps - 24h forecast", "url": "idNxNh…"},
     {"name": "snowdepth map - alps - 48h forecast", "url": "Bfvaff…"}
   ]}
   ```

2. `GET /<token>/wms` → **302 redirect** whose `Location` is the canonical endpoint,
   e.g. `…/<token>_map/gmaps/sd20alps@epsg3857/wms`. The token alone is not a URL;
   you must follow this redirect to learn the tile base.

3. Strip the trailing `/wms` and request tiles at:

   ```
   <base>/{z}/{x}/{y}.png
   ```

- **Tiling scheme:** standard Web Mercator **EPSG:3857** "gmaps" (Google/OSM) XYZ
  pyramid. Tiles respond at every zoom I probed (z8–z16).
- **Native resolution:** 20 m (≈ z13–14). Beyond ~z14 is upsampling.
- **Pixels:** 256×256, 8-bit **colormap PNG** — a rendered visualization, *not* values.
- Layer names seen: `sd20alps` (analysis), `sd_forecast20alps` (24h),
  `sd_2dforecast20alps` (48h).
- I did **not** find a working OGC `GetCapabilities`/`GetMap` — access is the XYZ
  tile pattern above, not classic WMS request params.

## S3 raw GeoTIFFs

Daily product, **one GeoTIFF per file, overwritten every day**. Bucket
`s3://exolabs-swiss-project/alps/`, AWS CLI profile `tuwien` (creds from the CSV
exolabs handed over).

```
000_alps_HS_product.tif        analysis / nowcast   (HS = Höhe der Schneedecke)
000-024_alps_HS_product.tif    \
000-048_alps_HS_product.tif     |  24h / 48h forecast products
024_alps_HS_product.tif         |
024-000_alps_HS_product.tif     |
048_alps_HS_product.tif         |
048-000_alps_HS_product.tif    /
```

```bash
aws s3 cp s3://exolabs-swiss-project/alps/000_alps_HS_product.tif . --profile tuwien
```

**Not yet confirmed** (check on first real download): exact value encoding
(units/scale/nodata) and the bucket region — the tool currently assumes
`eu-central-1`.

## Geographic coverage / Austria

Everything is published **Alps-wide**; there is no Austria-only product. Subset it
yourself:

- **Tiles:** request only the XYZ tiles covering Austria's bbox
  (`AUSTRIA_BBOX = (9.53, 46.37, 17.16, 49.02)` in the tool).
- **GeoTIFF:** download the Alps-wide file, then crop by bbox —
  `gdal_translate -projwin` (the tool's `--crop-austria` does this via `gdal.Translate`).

## The tool

```bash
python scripts/fetch_snow_cover.py list
python scripts/fetch_snow_cover.py tiles --product "snowdepth map - alps" --zoom 10
python scripts/fetch_snow_cover.py tif --crop-austria --out data/exolabs
```

Standalone, not wired into the tiles-server. Deps in `scripts/requirements.txt`
(`requests`, `boto3`, optional GDAL).
