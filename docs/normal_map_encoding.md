# Normal map tile encoding

How surface normals are stored in a tile, and why this format rather than the
obvious alternatives. Reference implementation:
[`util/encoding.py`](../util/encoding.py). Every number below was measured in
[`scripts/normal_map_playground.ipynb`](../scripts/normal_map_playground.ipynb)
on a real z17 tile over the Großglockner (65,536 px, BEV ALS DTM and DSM).

## TL;DR

**Hemi-octahedral projection, 127-centred 8-bit quantization, R/G of an RGBA
PNG. B and A carry two more per-pixel quantities, plain 0..255 unorm.**

| channel | contents |
|---|---|
| R | hemi-oct `e.x`, `round(e.x * 127) + 127` — codes 0..254 |
| G | hemi-oct `e.y`, same mapping |
| B | Toksvig roughness factor `L`, `round(L * 255)` — see "Roughness & snow visibility" below |
| A | snow steepness visibility, `round(v * 255)` — see "Roughness & snow visibility" below |

Two bytes per pixel of geometry (R/G). Costs 0.26° mean / 0.55° max angular
error, against a data uncertainty of 0.77° mean / 3.3° p99. B and A add two
more bytes each carrying an independent per-pixel scalar. A 256×256 tile is
~94 KB at R/G-only (see "Storage" below for the RGBA figure).

Putting real data in alpha overrides this doc's own general rejection of
that (see "Rejected alternatives") — deliberately, and only for this format.
The reasoning is spelled out where that rejection is stated, not silently
contradicted.

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
B and A are plain `[0,1]` unorm already once fetched — no undo needed there.

```wgsl
// Decodes a normal-tile texel into an ENU surface normal (+X east, +Y north,
// +Z up), a Toksvig roughness factor, and a snow visibility fraction.
// tex: an rgba8unorm sample of the tile.
fn normal_tile_to_v3f32(tex: vec4<f32>) -> vec3<f32> {
    let e: vec2<f32> = (tex.rg * 255.0 - 127.0) / 127.0;
    let t: vec2<f32> = vec2<f32>(e.x + e.y, e.x - e.y) * 0.5;
    return normalize(vec3<f32>(t, 1.0 - abs(t.x) - abs(t.y)));
}

fn normal_tile_roughness(tex: vec4<f32>) -> f32 {
    return tex.b; // Toksvig factor L: 1.0 = smooth/coherent, 0.0 = fully divergent
}

fn normal_tile_snow_visibility(tex: vec4<f32>) -> f32 {
    return tex.a; // fraction of the underlying area within the snow-visible band
}
```

Pixels with no valid source data are written as the flat normal `(0,0,1)`,
full roughness factor and full snow visibility → `(127, 127, 255, 255)`.
There is no separate validity mask: a void reads as flat, smooth,
snow-visible ground.

## Roughness & snow visibility

### Why B and A exist

Building a coarser pyramid level averages four child normals together. That
average has to be renormalized back to a unit vector before it can be stored
as a direction — but the *length* of the average before renormalizing is
thrown away, and it is not nothing: it is exactly a measure of how much the
four children agreed. Four children pointing the same way average to a
vector of length ~1; four children pointing in wildly different directions
average to something much shorter. This is the classic "Toksvig factor"
(Toksvig, *Mipmapping Normal Maps*, 2004) — the standard real-time-rendering
signal for fading in extra roughness at coarse mip levels to suppress
specular popping, since a coarse texel's stored direction is otherwise a
smoothed lie about terrain that is actually rough underneath it.

B stores that factor, `L`, directly (not a pre-baked roughness/specular-power
conversion — the shader decides how to turn `L` into an actual BRDF
parameter). A stores a related but distinct quantity: what fraction of the
area under a coarse texel would actually show snow, given the shader's
existing steepness-based visibility band. Both need the same underlying
machinery — a pyramid reduction that does not throw information away before
it's needed — so they're documented together.

### Leaf-count-weighted reduction, not "count of valid children"

The natural-looking shortcut — weight a 2×2 reduction step by how many of
*its own* 4 immediate children were valid — is not exact once validity is
non-uniform deeper in the tree. Worked counterexample: quadrant A has 4 valid
leaves averaging to `(1,0,0)`; quadrant B has 1 valid leaf `(0,1,0)` and 3
invalid; C and D are fully invalid. The true 5-leaf mean is
`(4·(1,0,0) + 1·(0,1,0)) / 5 = (0.8, 0.2, 0)`. Naively averaging the two
valid quadrants as if they carried equal weight gives
`((1,0,0) + (0,1,0)) / 2 = (0.5, 0.5, 0)` — wrong, because a quadrant with
one surviving leaf gets to outvote a quadrant with four.

The fix is to track the *exact* number of MAX_ZOOM leaves under every pixel
(`count`), not a per-step 0..4 tally, and combine four children by:

```
count_parent = count_1 + count_2 + count_3 + count_4
mean_parent  = (count_1·mean_1 + count_2·mean_2 + count_3·mean_3 + count_4·mean_4) / count_parent
```

This is exact by induction: a leaf has `count=1` (or `0` if invalid) and
`mean` equal to itself; if every child is exact for its own subtree,
`count_i·mean_i` is exactly that subtree's leaf-vector *sum*, so the formula
above is exactly `sum(all leaves) / count(all leaves)` for the combined
subtree. `tile_creators/als_normals.py`'s `_reduce_2x` implements this for
both the mean normal and the snow-visibility scalar, and never renormalizes
the mean vector to a unit direction until the final byte-encoding step
(`util/encoding.py`'s `encode_tile`) — renormalizing any earlier would
discard `L` before it's ever used.

Two numeric consequences: `count` needs `int64` (a leaf count can reach
`4**(MAX_ZOOM-MIN_ZOOM)`, e.g. `4**16 ≈ 4.3e9` at today's `MAX_ZOOM=16` —
already past `uint32`'s ceiling), and the weighted sums need `float64`
intermediates (at that magnitude, `float32` can no longer represent the
resulting fractional weights).

`tile_creators/cosmos_snow.py`'s own pyramid reduction *does* use a local
valid-children count, and that is fine there — it rebuilds each parent from
already re-quantized children anyway (see its own docstring, "modulo the
per-level quantization noise"), so it never had an exactness invariant to
preserve. This format's B channel specifically needs one.

### Snow visibility

At MAX_ZOOM, computed once per leaf directly from the shader's own
steepness-visibility formula: `1.0` for a slope in
`[SNOW_ANGLE_MIN, SNOW_ANGLE_MAX]` (0°–45°), linearly ramping to `0` over
`SNOW_ANGLE_BLEND` (5°) beyond `SNOW_ANGLE_MAX`. The symmetric ramp below
`SNOW_ANGLE_MIN` is dead in practice — a slope angle from vertical is never
negative, and `SNOW_ANGLE_MIN` is `0`. These three constants
(`tile_creators/als_normals.py`) mirror fixed WGSL shader constants and are
baked into every tile byte: changing them requires regenerating the whole
tileset. The linear falloff shape is an assumption (the shader's
`calculate_falloff` body wasn't available to match exactly) — worth
revisiting if it turns out to be smoothstep or another curve.

At coarser levels, A is the leaf-count-weighted average described above — a
"fraction of the underlying area that's snow-visible," not a re-evaluation
of the band formula against an already-averaged direction (which would
behave very differently on bimodal terrain, e.g. a texel half cliff and half
flat ground).

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

  **Overridden for this format's A channel** (snow steepness visibility, see
  "Roughness & snow visibility" above): the failure modes above only trigger
  through a filtered/blended sample, a WebP re-encode, or a canvas round-trip,
  and none of those apply to this tileset. Its only consumer is a raw WGSL
  `textureLoad` (never filtered or blended sampling), and it is never
  re-encoded to WebP or read back through a canvas. If either of those ever
  becomes true for this tileset, this override needs revisiting, not just the
  code.
- **2-channel PNG (grayscale+alpha, colour type 4).** Genuinely tempting: the
  format has only two real channels, and this drops the reserved B channel from
  the file entirely, measurably shrinking tiles. Rejected because it puts the
  second component back in the alpha channel, and because the channel budget is
  wanted open while the format is still being evaluated. Still measured in the
  notebook. (Now moot: B and A both carry real data — see "Roughness & snow
  visibility" above.)
- **WebP.** The renderer cannot decode it. Measured in the notebook so the cost of
  that constraint is a number rather than a guess.
- **Lossy anything.** These bytes are vector components, not pixels; a codec
  optimizing perceptual image similarity has no idea it is destroying geometry.
- **BC5.** A GPU block-compression format built for exactly this shape of data
  (two independent 8-bit channels), decoded by sampling hardware instead of a
  CPU-side PNG inflate — worth a number rather than a guess given it's the
  standard normal-map texture format. No encoder was available to measure it
  directly, so `scripts/normal_map_playground.ipynb` (§7) simulates the
  algorithm: each 4x4 block re-quantized to 8 values interpolated between that
  block's own min and max. Two problems, both measured on real alpine z17
  data: it only saves 31% over the already-compressed PNG (a fixed 65,536 B vs
  ~94,415 B — BC5 is fixed-rate, so it can't adapt to flat regions the way PNG
  does, and would lose outright on the 38 KB urban DTM case above), and the
  per-block quantization pushes error to 1.17° mean / 5.49° p99 / 12.7° max —
  *past* the 0.77° / 3.35° / 9.23° finite-diff-vs-Sobel noise floor that
  justified 8 bits per component in the first place, because a block's own
  min/max range gets inflated by the same near-random low-byte noise that
  makes this data resist PNG compression. It would also be a bigger lift than
  a codec swap: real GPU-texture loading (KTX2 or similar) in the renderer,
  not an image decoder.

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

The ~94 KB/tile figure above predates the B/A channels and is now a lower
bound, not current: B and A each add a raw byte per pixel before compression.
B (Toksvig `L`) should compress well — it's 255 almost everywhere at MAX_ZOOM
(every leaf starts at `L=1`) and only drops meaningfully once the pyramid
starts averaging divergent terrain, so it's mostly uniform within a tile. A
(snow visibility) is terrain-dependent in the same way R/G already are. Not
re-measured in `scripts/normal_map_playground.ipynb` yet — flagged as a
follow-up alongside that notebook's `encode_normals`/`decode_normals`/
`FLAT_NORMAL_RGB` calls, which still use the pre-RGBA API names and need
updating to `encode_tile`/`decode_tile`/`FLAT_TILE_RGBA`.
