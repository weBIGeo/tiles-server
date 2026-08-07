# Zoom level vs. DTM/DSM native resolution

Which slippy-map zoom level to sample BEV ALS DTM/DSM data into for 256×256
height/normal tiles, and what each choice costs.

## TL;DR

BEV ALS DTM/DSM is **1 m/px native**. Zoom 17 (~0.80 m/px at Austrian latitudes)
is the closest honest 1:1 match on resolution grounds — but it costs **4× the
storage of z16** and the evidence suggests a meaningful share of that fourfold is
noise rather than terrain. Zoom 16 (~1.59 m/px) is the pragmatic default unless
the renderer specifically needs z17 detail.

## The resolution math

Web Mercator ground resolution at zoom `z` and latitude `lat`:

```
resolution(z, lat) = (156543.03392804097 / 2**z) * cos(lat)
```

`156543.034` is the standard Web Mercator meters/pixel at zoom 0 on the equator;
the `cos(lat)` term corrects for Web Mercator inflating horizontal distances away
from it.

At Austria's latitude (`cos(lat) ≈ 0.67` — Vienna 48.2°N and Großglockner 47.1°N
are close enough not to matter here):

| zoom | m/px | 256px tile footprint | vs. 1 m source |
|---|---|---|---|
| 16 | 1.59 | ~408 m | undersamples 1.6× |
| **17** | **0.80** | **~204 m** | oversamples 1.25× |
| 18 | 0.40 | ~102 m | oversamples 2.5× |
| 19 | 0.20 | ~51 m | oversamples 5× |

Solving `resolution(z, lat) = 1` gives **z ≈ 16.67** — between 16 and 17, with 17
the closer of the two. Neither is an exact match.

## What each zoom costs

Storage, from measured ~94 KB PNG normal tiles on real alpine terrain, per
50×50 km BEV source file (2500 km²), per product (DTM and DSM are separate
tilesets):

| max zoom | tiles at max zoom | incl. pyramid | per source file | Austria (~34 files) |
|---|---|---|---|---|
| z16 | ~14,650 | ~19,500 | ~1.8 GB | ~60 GB |
| z17 | ~58,600 | ~78,100 | ~7.4 GB | ~250 GB |
| z18 | ~234,400 | ~312,500 | ~30 GB | ~1 TB |

Two products doubles all of it.

## Is the extra z17 detail real?

Two independent signals say partly not:

- **PNG only compresses the z17 normal field by ~28%** on its two real channels.
  A near-incompressible field is a field with little spatial structure left at
  pixel scale.
- **Two honest gradient estimators disagree by 0.77° mean / 3.3° p99** on the same
  z17 tile (finite difference vs. Sobel — see
  [`normal_map_encoding.md`](normal_map_encoding.md)). That is the uncertainty the
  source data carries into the normal at this sampling rate.

Both are consistent with z17 resolving beyond what a 1 m survey actually pins
down. Sampling at z16 averages some of that away rather than storing it.

## Which to use, and when

- **Zoom 16** — the default. Slightly coarser than the source, 4× cheaper than
  z17, and the mild downsampling suppresses noise the source can't justify.
- **Zoom 17** — when output pixels should correspond ~1:1 with real measured
  source pixels: verifying that a reprojection pipeline is geometrically honest,
  or when the renderer genuinely samples normals that finely.
- **Zoom 18+** — never for accuracy; each source pixel is stretched across ~6
  output pixels by interpolation, which adds no real detail. Defensible only as a
  deliberate smoothing choice, the same trade real terrain renderers make by
  sampling normal textures finer than the underlying height grid to reduce shading
  facets — and at ~1 TB per product Austria-wide, an expensive way to buy it.

Not a hard rule enforced anywhere in code — `MAX_ZOOM` is a module constant in the
tile source, and this is the reasoning behind whatever it is set to.
