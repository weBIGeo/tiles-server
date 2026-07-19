# Idea: nonlinear (quadratic) quantization for the snow-height avg/min/max bytes

Explored while designing the avg/min/max byte encoding in
[`tile_creators/cosmos_snow.py`](../tile_creators/cosmos_snow.py) (`_encode_cm`/
`_decode_cm`). **Not implemented** - the shipped encoding uses a plain linear curve
instead. Written up here so the reasoning isn't lost.

## The idea

Each of `avg`/`min`/`max` is quantized into a single byte (0-255) representing a
cm-depth value up to `CEILING_CM`. A plain linear curve spends the same resolution
everywhere: with `CEILING_CM=500`, that's a uniform `500/255 ≈ 1.96 cm` per step
across the whole range.

Since snow depth in this dataset is skewed toward small values (most texels are
mostly shallow snow or bare ground, with occasional deep pockets), a nonlinear curve
could spend more of the 256 codes on the common low end and less on the rare high
end - "increasing accuracy for measurements close to 0" without changing the ceiling
or the byte budget.

A quadratic compander was worked out for this: `cm = A*byte^2 + byte`, with
`A = (CEILING_CM - 255) / 255**2` chosen so the derivative at `byte=0` is exactly 1
- i.e. a **hard floor of 1 cm/step**, matching the source GeoTIFF's own integer-cm
precision (going finer than that would just waste code space distinguishing values
the source data can't even produce). The step size then grows smoothly with `byte`.
Inverting the curve (encode): `byte = round((-1 + sqrt(1 + 4*A*cm)) / (2*A))`,
clamped to `[0, 255]`.

With `CEILING_CM=500` (`A ≈ 0.0037678`):

| byte | cm | step to next |
|---|---|---|
| 0 | 0.00 | 1.00 |
| 10 | 10.38 | 1.08 |
| 32 | 35.86 | 1.25 |
| 64 | 79.43 | 1.49 |
| 128 | 189.73 | 1.97 |
| 192 | 330.90 | 2.45 |
| 224 | 413.05 | 2.69 |
| 255 | 500.00 | - |

Round-trip check against realistic values:

```
cm_in -> byte -> cm_out (error)
   0 ->   0 ->    0.00   err=+0.00
   1 ->   1 ->    1.00   err=+0.00
  25 ->  23 ->   24.99   err=-0.01
  50 ->  43 ->   49.97   err=-0.03
 100 ->  77 ->   99.34   err=-0.66
 150 -> 107 ->  150.14   err=+0.14
 200 -> 133 ->  199.65   err=-0.35
 300 -> 179 ->  299.72   err=-0.28
 416 -> 225 ->  415.74   err=-0.26
 500 -> 255 ->  500.00   err=+0.00
```

The bottom quarter of the byte range (0-64) already covers 0-~80cm at sub-1.5cm
resolution - close to where most of the real, measured medians in this dataset
actually sit - while the top end coarsens to ~2.7-2.9cm/step, only slightly worse
than the linear curve's uniform ~1.96cm.

## Why it wasn't used

- **Marginal real benefit here.** The plain linear curve already gives sub-2cm
  resolution *everywhere*, including the low end. The nonlinear curve's win is real
  but small (sub-1.5cm vs ~2cm for most of the practically-observed range) - not a
  dramatic improvement for a field whose whole purpose is a rough visual/uncertainty
  indicator, not exact reconstruction.
- **More implementation complexity for that small win.** Linear encode/decode is a
  single multiply/divide; the quadratic version needs a square-root-based inversion
  per channel, on every encode - more code, more room for a sign/precision mistake,
  harder for the next person to reason about at a glance.
- **Doesn't fix the deeper limitations either way.** Both curves share the same
  caveats: independent per-channel quantization means a decoded `min` can in
  principle land marginally above a decoded `max` at extreme byte values, and
  min/max quantization noise doesn't cancel across pyramid levels the way avg's does
  (each level re-quantizes from the *previous* level's already-quantized bytes, not
  raw data, so min drifts slightly low and max slightly high the more levels are
  stacked). Neither of these is specific to - or solved by - going nonlinear.

If a future need arises for genuinely tighter low-end precision (e.g. a UI feature
that visibly benefits from sub-cm resolution near zero), this curve is a ready-made
starting point - the math and numbers above already check out.
