import contextlib
import io
import os
import shutil
import sys
import time
import traceback

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_als_mosaic as mosaic  # noqa: E402
import fetch_bev_als  # noqa: E402

PRODUCT = "DTM"
SKIP_DOWNLOAD = False
NTFY_URL = "https://ntfy.sh/webigeo-tiles-a7mC2FoQs"
DOWNLOAD_RETRIES = 3
RETRY_DELAY_S = 60
MOSAIC_NOTIFY_EVERY_S = 30 * 60


def ntfy(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    print(f"[ntfy] {title}: {body}")
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        requests.post(NTFY_URL, data=body.encode("utf-8"), headers=headers, timeout=15).raise_for_status()
    except Exception as e:
        print(f"[ntfy] failed to send: {e}")


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}m" if h else f"{rem // 60}m{rem % 60:02d}s"


def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1e9


def download_all(product: str, source_dir: str) -> None:
    os.makedirs(source_dir, exist_ok=True)
    tile_ids = fetch_bev_als.TILE_IDS
    present = set(mosaic.source_files(source_dir, product))
    missing = [t for t in tile_ids if t not in present]
    ntfy(f"ALS {product}: download started",
         f"{len(missing)} of {len(tile_ids)} tiles to fetch into {source_dir} "
         f"({free_gb(source_dir):.0f} GB free)", tags="arrow_down")

    t_start = time.monotonic()
    bytes_done = 0
    for i, tile_id in enumerate(missing):
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                t0 = time.monotonic()
                path = fetch_bev_als.download_tile(product, tile_id, source_dir)
                break
            except Exception as e:
                if attempt == DOWNLOAD_RETRIES:
                    raise RuntimeError(f"{tile_id}: download failed {attempt}x, last error: {e}") from e
                ntfy(f"ALS {product}: retrying {tile_id}", f"attempt {attempt} failed: {e}",
                     priority="high", tags="warning")
                time.sleep(RETRY_DELAY_S)
        took = max(time.monotonic() - t0, 1e-3)
        size = os.path.getsize(path)
        bytes_done += size
        elapsed = time.monotonic() - t_start
        eta = elapsed / (i + 1) * (len(missing) - i - 1)
        ntfy(f"ALS {product}: {i + 1}/{len(missing)} downloaded",
             f"{tile_id}: {size / 1e9:.1f} GB in {fmt_duration(took)} ({size / 1e6 / took:.0f} MB/s). "
             f"Total {bytes_done / 1e9:.0f} GB in {fmt_duration(elapsed)}, ~{fmt_duration(eta)} left, "
             f"{free_gb(source_dir):.0f} GB free", priority="low")

    ntfy(f"ALS {product}: download done",
         f"{len(missing)} tiles, {bytes_done / 1e9:.0f} GB in {fmt_duration(time.monotonic() - t_start)}",
         tags="white_check_mark")


def build_source_vrt(product: str, source_dir: str) -> None:
    files = mosaic.source_files(source_dir, product)
    if len(files) != len(fetch_bev_als.TILE_IDS):
        raise RuntimeError(f"only {len(files)}/{len(fetch_bev_als.TILE_IDS)} tiles present in {source_dir}")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mosaic.check_shared_edges(files)
    report = buf.getvalue()
    print(report, end="")
    pairs = report.splitlines()
    bad = [line.strip() for line in pairs if "NOT IDENTICAL" in line]
    vrt = mosaic.write_source_vrt(source_dir, product, files)
    body = f"{vrt}\nshared edges: {len(pairs) - len(bad)}/{len(pairs)} identical"
    if bad:
        body += "".join(f"\n{b}" for b in bad[:5])
        if len(bad) > 5:
            body += f"\n... and {len(bad) - 5} more"
    ntfy(f"ALS {product}: source VRT written", body,
         priority="high" if bad else "default", tags="warning" if bad else "page_facing_up")


def build_mosaic(product: str, source_dir: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    src_vrt = mosaic.source_vrt_path(source_dir, product)
    chunks = mosaic.covered_chunks(sorted(mosaic.source_files(source_dir, product)))
    todo = [c for c in chunks if not os.path.exists(mosaic.chunk_path(out_dir, product, *c))]
    ntfy(f"ALS {product}: mosaic started",
         f"{len(todo)} of {len(chunks)} z{mosaic.CHUNK_ZOOM} chunks to warp into {out_dir} "
         f"({free_gb(out_dir):.0f} GB free)", tags="globe_with_meridians")

    t_start = time.monotonic()
    last_notify = t_start
    empty = 0
    with mosaic.rasterio.open(src_vrt) as src:
        for i, (cx, cy) in enumerate(todo):
            t0 = time.monotonic()
            result = mosaic.warp_chunk(src, product, cx, cy, out_dir)
            empty += result == "empty"
            print(f"[{i + 1}/{len(todo)}] z{mosaic.CHUNK_ZOOM}/{cx}/{cy}: {result} in {time.monotonic() - t0:.0f}s")
            mosaic.write_mosaic_vrt(out_dir, product)
            now = time.monotonic()
            if now - last_notify >= MOSAIC_NOTIFY_EVERY_S:
                last_notify = now
                eta = (now - t_start) / (i + 1) * (len(todo) - i - 1)
                ntfy(f"ALS {product}: mosaic {i + 1}/{len(todo)}",
                     f"{fmt_duration(now - t_start)} elapsed, ~{fmt_duration(eta)} left, "
                     f"{free_gb(out_dir):.0f} GB free", priority="low")

    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".tif")]
    total_gb = sum(os.path.getsize(f) for f in files) / 1e9
    vrt = mosaic.write_mosaic_vrt(out_dir, product)
    ntfy(f"ALS {product}: mosaic done",
         f"{len(files)} chunks ({empty} empty skipped), {total_gb:.0f} GB, "
         f"in {fmt_duration(time.monotonic() - t_start)}\n{vrt}", tags="tada")


def main() -> None:
    source_dir = mosaic.SOURCE_DIR.format(product=PRODUCT)
    out_dir = mosaic.MOSAIC_DIR.format(product=PRODUCT)

    t_start = time.monotonic()
    step = "start"
    try:
        if not SKIP_DOWNLOAD:
            step = "download"
            download_all(PRODUCT, source_dir)
        step = "source VRT"
        build_source_vrt(PRODUCT, source_dir)
        step = "mosaic"
        build_mosaic(PRODUCT, source_dir, out_dir)
    except BaseException as e:
        kind = "interrupted" if isinstance(e, KeyboardInterrupt) else "FAILED"
        ntfy(f"ALS {PRODUCT}: {kind} during {step}",
             f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-1500:]}\nRerun to resume.",
             priority="urgent", tags="rotating_light")
        raise
    ntfy(f"ALS {PRODUCT}: all done", f"total {fmt_duration(time.monotonic() - t_start)}",
         priority="high", tags="tada")


if __name__ == "__main__":
    main()
