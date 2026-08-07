# Normal map tile encoding

How surface normals are stored in a tile, and why this format rather than the
obvious alternatives. Reference implementation:
[`util/encoding.py`](../util/encoding.py). Every number below was measured in
[`scripts/normal_map_playground.ipynb`](../scripts/normal_map_playground.ipynb)
on a real z17 tile over the Großglockner (65,536 px, BEV ALS DTM and DSM).

## TL;DR

**Hemi-octahedral projection, 127-centred 8-bit quantization, R/G of an RGB PNG.**

| channel | contents |
|---|---|
| R | hemi-oct `e.x`, `round(e.x * 127) + 127` — codes 0..254 |
| G | hemi-oct `e.y`, same mapping |
| B | 0 — reserved |
| — | no alpha channel |

Two bytes per pixel of real data. Costs 0.26° mean / 0.55° max angular error,
against a data uncertainty of 0.77° mean / 3.3° p99. A 256×256 tile is ~94 KB.

## Frame convention

Stored normals are **ENU — +X east, +Y north, +Z up** — and metric: the Web
Mercator `1/cos(lat)` altitude correction is applied to the heights *before* the
normal is computed, so what is stored is the true surface normal, not the
cos-latitude-flattened one.

Getting this wrong is the classic source of inverted-lighting bugs, so to be
explicit: slippy-map rows increase *southward*, which is why the weBIGeo shader's
`nx = hL - hR` / `ny = hD - hU` pairing looks asymmetric but isn't. `hD` is the
**south** neighbour, so

```
nx = h_west  - h_east  = -dh/dx_east  * 2*quad_width
ny = h_south - h_north = -dh/dy_north * 2*quad_height
```

and `(nx, ny, 2)` is proportional to `(-dh/dx, -dh/dy, 1)` — a consistent ENU
normal. The `2` is not arbitrary: the differences span two cells but are divided
by one `quad_width`, so it cancels exactly.

A client rendering in Mercator-flattened space gets back to its own frame with
`normalize(n.x, n.y, n.z * cos(lat))`.

## Encode / decode

Encoding, given a normalized normal with `n.z >= 0` (always true for a
heightfield):

```
p    = n.xy / (abs(n.x) + abs(n.y) + n.z)
e    = vec2(p.x + p.y, p.x - p.y)      // fills [-1,1]^2
byte = round(e * 127) + 127            // 0..254, symmetric about 127
```

Decoding in WGSL. Note the `* 255.0`: an `rgba8unorm` fetch has already divided
by 255, and that has to be undone before the 127-centred mapping is reversed.

```wgsl
// Decodes a normal-tile texel into an ENU surface normal (+X east, +Y north, +Z up).
// tex: an rgba8unorm sample of the tile. Only .rg carry data.
fn normal_tile_to_v3f32(tex: vec4<f32>) -> vec3<f32> {
    let e: vec2<f32> = (tex.rg * 255.0 - 127.0) / 127.0;
    let t: vec2<f32> = vec2<f32>(e.x + e.y, e.x - e.y) * 0.5;
    return normalize(vec3<f32>(t, 1.0 - abs(t.x) - abs(t.y)));
}
```

Pixels with no valid source data are written as the flat normal `(0,0,1)` →
`(127, 127, 0)`. There is no validity mask: a void reads as flat ground.

## Why hemi-octahedral

Heightfield normals always point up, so only a hemisphere needs encoding. Plain
octahedral (weBIGeo's existing `v3f32_to_oct`) maps the *whole sphere* to the
`[-1,1]` square, which confines every upward normal to the `|x|+|y| <= 1` diamond
and leaves the square's four corners permanently unused — half the code space.

The hemi-oct 45° rotation fills the square instead. Expected gain is √2; measured
gain is **1.40×**:

| | mean | p99 | max |
|---|---|---|---|
| **hemi-oct 8:8** | **0.257°** | **0.481°** | **0.546°** |
| plain oct 8:8 | 0.359° | 0.784° | 0.936° |

It also removes the octahedral fold from the sampled domain, so GPU bilinear
filtering between two encoded texels can never interpolate across a discontinuity.

The cost is that the renderer needs a decode function that is not the shared
`oct_to_v3f32` — four lines, given above.

## Why 8 bits and not 16

The original proposal was 16 bits per component packed as R/G = hi/lo of `oct.x`,
B/A = hi/lo of `oct.y`. It buys precision that is not there to measure.

**How much is actually known about a normal?** Finite-difference and Sobel are
both defensible estimators of the same surface, computed from the same heights on
the same grid. Where they disagree, the disagreement comes from the data:

| | mean | p99 | max |
|---|---|---|---|
| finite-diff vs Sobel (alpine z17) | 0.77° | 3.35° | 9.23° |

So the 8-bit quantization error (0.26° mean) sits about 3× under the mean
estimator spread and 13× under p99. 16 bits reaches 0.0014° mean — resolving a
quantity that is itself uncertain by degrees.

This is terrain-dependent and worth being honest about: over *flat urban* DTM the
two estimators agree to roughly 0.06°, which is below the quantization error. The
argument there is perceptual rather than statistical — a 0.26° normal
perturbation shifts Lambertian shading by at most 0.0045, i.e. **about one code
of an 8-bit framebuffer**. Either way, invisible.

**And it is not free.** Measured PNG sizes for the same tile:

| encoding | alpine DTM | alpine DSM | urban DTM | urban DSM |
|---|---|---|---|---|
| **hemi-oct 8:8 RGB** | **94,415 B** | **94,618 B** | **38,318 B** | **113,036 B** |
| plain oct 16:16 RGBA | 2.41× | 2.40× | 4.45× | 2.09× |
| naive xyz→rgb | 1.23× | 1.23× | 0.88× | 1.23× |

The low byte of a value quantized from LiDAR-derived normals is close to random,
so PNG stores it nearly raw. The penalty is worst exactly where the baseline
compresses best — flat urban DTM, at 4.45×.

The `naive xyz→rgb` row is the one surprise: on *flat urban* DTM it beats hemi-oct
by 12%, because there `nz` is a near-constant 255 that compresses to nothing while
`nx`/`ny` barely leave 128. Hemi-oct deliberately spreads values across the full
square — that is where the √2 precision comes from — which raises byte entropy.
On alpine terrain, which dominates the actual coverage, hemi-oct wins by 23% *and*
is twice as accurate.

## Why the quantization is centred on 127

The conventional unorm mapping `round((e * 0.5 + 0.5) * 255)` spreads 256 codes
over 255 intervals, so the midpoint falls *between* codes. A perfectly flat
surface encodes to 128 and decodes to `e = 0.0039`:

```
127-centred (used)  -> R,G = [127, 127]   error 0.0000 deg
conventional unorm  -> R,G = [128, 128]   error 0.2256 deg
```

0.2256° is far below the data's own noise, but unlike noise it is a *systematic*
bias in one fixed direction, so it never averages out. An entire lake, glacier
plateau or flat roof gets the same false shading offset across its whole surface,
and the pyramid reduction preserves it at every zoom level.

Centring on 127 with a half-range of 127 puts `e = 0` exactly on a code and makes
the encoding symmetric under negation — mirrored slopes land equidistant from 127.
It costs one unused code (255) and a 0.4% coarser step.

## Rejected alternatives

- **Store `nx, ny` and reconstruct `nz = sqrt(1 - nx² - ny²)`.** Simplest option,
  and fine for a DTM, but it collapses on the DSM: a building wall spanning one
  pixel yields a normal 85°+ off vertical, exactly where this parameterization is
  least precise and where quantization can push `nx² + ny² > 1`.
- **Any data in the alpha channel.** Alpha carries semantics for image decoders —
  browsers premultiply RGB by it, and libwebp's `lossless=True` zeroes RGB behind
  `A=0` unless `exact=True` is passed (see
  [`webp-instead-png.md`](webp-instead-png.md)). A 3-channel RGB tile has no alpha
  to mishandle.
- **2-channel PNG (grayscale+alpha, colour type 4).** Genuinely tempting: the
  format has only two real channels, and this drops the reserved B channel from
  the file entirely, measurably shrinking tiles. Rejected because it puts the
  second component back in the alpha channel, and because the channel budget is
  wanted open while the format is still being evaluated. Still measured in the
  notebook.
- **WebP.** The renderer cannot decode it. Measured in the notebook so the cost of
  that constraint is a number rather than a guess.
- **Lossy anything.** These bytes are vector components, not pixels; a codec
  optimizing perceptual image similarity has no idea it is destroying geometry.

## Pitfall: don't measure angular error with `arccos(dot)`

Worth recording, because it silently produced wrong numbers here first.

Near `dot = 1`, `arccos(1 - d) ≈ sqrt(2d)`, so an error `d` in the dot product
blows up to `sqrt(2d)` in the angle. With a **float32** dot product that puts a
floor of ~0.028° on anything measurable — comparing a vector to *itself* reports
up to 0.044°. The 16:16 row was originally measured this way and read
0.0280°/0.0343°/0.0396°, which are simply consecutive rungs of the float32
`arccos` ladder, not error values. The tell was p99 being *identically* 0.0280
across four different datasets.

`util/encoding.py`'s `angle_between_deg` uses the chord form `2*asin(|a-b|/2)` in
float64 instead, which is exact for unit vectors and well-conditioned at small
angles. The true oct 16:16 error is 0.0014° mean — 4× smaller than the broken
measurement suggested.

## Storage

At ~94 KB/tile, per 50×50 km BEV ALS source file (~58,600 tiles at z17, ~78,100
including the pyramid), per product:

| max zoom | tiles incl. pyramid | per source file | Austria (~34 files) |
|---|---|---|---|
| z17 | ~78,100 | ~7.4 GB | ~250 GB |
| z16 | ~19,500 | ~1.8 GB | ~60 GB |

PNG only achieves ~28% on the two real channels at z17, i.e. the normal field is
close to incompressible there — which, together with the 0.77° estimator spread,
suggests a meaningful share of what z17 stores is noise rather than terrain. See
[`max_zoomlevel_normal_height_maps.md`](max_zoomlevel_normal_height_maps.md) for
the resolution side of that trade.
