# Zoom level vs. DTM/DSM native resolution

Reasoning on which slippy-map zoom level makes sense for sampling BEV ALS DTM/DSM
data into 256x256 height/normal tiles. Companion to
[`util/normal_map_playground.ipynb`](../util/normal_map_playground.ipynb) and
[`util/fetch_bev_als.py`](../util/fetch_bev_als.py).

## TL;DR

BEV ALS DTM/DSM data is 1m/pixel native resolution. **Zoom 17** (~0.8m/px at
Austria's latitude) is the closest honest 1:1 match for a 256x256 tile - it
resamples the source without fabricating extra detail. The playground notebook
currently defaults to **zoom 18** (~0.4m/px), which oversamples ~2.5x; that's a
deliberate, defensible choice if the goal is smoother-looking rendered normals
(matching how real terrain engines like weBIGeo's oversample normal textures
relative to the height grid to reduce shading facets), but not if the goal is to
sanity-check whether the reprojection pipeline preserves real terrain accuracy.

## The math

Web Mercator ground resolution at zoom `z` and latitude `lat`:

```
resolution(z, lat) = (156543.03392804097 / 2**z) * cos(lat)
```

(`156543.034` is the standard Web Mercator meters/pixel at zoom 0, equator; the
`cos(lat)` term corrects for Web Mercator inflating horizontal distances away
from the equator - see the same correction applied to `quad_width`/`quad_height`
and `altitude_correction_factor` in the notebook.)

At Austria's latitude (cos(lat) ≈ 0.67, e.g. Vienna 48.2°N or Großglockner
47.1°N - the two are close enough not to matter here):

| zoom | m/px | 256px tile footprint |
|---|---|---|
| 16 | 1.59 | ~408 m |
| **17** | **0.80** | **~204 m** |
| 18 (current default) | 0.40 | ~102 m |
| 19 | 0.20 | ~51 m |

Solving `resolution(z, lat) = 1` (matching the source's 1m/pixel exactly) gives
`z ≈ 16.67` - between zoom 16 (slightly coarser than source) and zoom 17
(slightly finer). Zoom 17 is the closer of the two.

## Which to use, and when

- **Zoom 17** - use when testing whether the DTM/DSM → tile reprojection pipeline
  itself is geometrically honest: output pixels correspond almost 1:1 with real
  measured source pixels, so any artifacts seen are from the pipeline, not from
  interpolation.
- **Zoom 18+** - use when evaluating how a normal map would actually look
  rendered. Each source pixel gets stretched across ~6 output pixels via
  bilinear interpolation at zoom 18, which doesn't add real detail but does
  smooth out shading facets/aliasing - the same tradeoff real terrain renderers
  make deliberately by sampling normal textures finer than the underlying height
  grid.

Not a hard rule enforced anywhere in code - just the reasoning behind the choice
of `ZOOM` in the notebook, to revisit if that constant is ever questioned again.
