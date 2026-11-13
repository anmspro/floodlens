"""Imagery: organizer PNG tiles -> GeoTIFF (EPSG:3857) -> per-epoch mosaic -> chips and web overlays.

The organizer PNGs carry no georeferencing, but each row of image_metadata.csv gives
WGS84 bounds and the pixel grid is Web Mercator, so the corners projected to EPSG:3857
define the exact affine transform. Your own GeoTIFFs (imagery.source = geotiff) are
reprojected to EPSG:3857 so both paths end in the same mosaic files.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import from_bounds as window_from_bounds

from floodlens.config import Settings
from floodlens.geo import WEBMERC, WGS84, transformer

EPOCHS = ("pre", "post")
GTIFF_OPTS = dict(driver="GTiff", tiled=True, blockxsize=256, blockysize=256, compress="deflate")


def tif_dir(s: Settings, epoch: str) -> Path:
    return s.derived / "imagery" / epoch


def mosaic_path(s: Settings, epoch: str) -> Path:
    return s.derived / "imagery" / f"{epoch}_mosaic.tif"


def _rgb_array(png: Path) -> np.ndarray:
    with Image.open(png) as im:
        rgba = np.asarray(im.convert("RGBA"))
    rgb = np.maximum(rgba[..., :3], 1)          # 0 is reserved for nodata
    rgb[rgba[..., 3] == 0] = 0                  # transparent -> nodata
    return rgb


def png_to_geotiff(png: Path, dst: Path, west: float, south: float, east: float, north: float) -> None:
    t = transformer(WGS84, WEBMERC)
    x0, y0 = t.transform(west, south)
    x1, y1 = t.transform(east, north)
    rgb = _rgb_array(png)
    h, w = rgb.shape[:2]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst, "w", width=w, height=h, count=3, dtype="uint8", crs=WEBMERC,
                       transform=from_bounds(x0, y0, x1, y1, w, h), nodata=0, **GTIFF_OPTS) as ds:
        ds.write(np.moveaxis(rgb, -1, 0))


def geotiff_to_3857(src_path: Path, dst: Path) -> None:
    with rasterio.open(src_path) as src:
        transform, w, h = calculate_default_transform(src.crs, WEBMERC, src.width, src.height, *src.bounds)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(dst, "w", width=w, height=h, count=3, dtype="uint8", crs=WEBMERC,
                           transform=transform, nodata=0, **GTIFF_OPTS) as out:
            for b in range(1, 4):
                reproject(rasterio.band(src, min(b, src.count)), rasterio.band(out, b),
                          dst_transform=transform, dst_crs=WEBMERC, resampling=Resampling.bilinear,
                          src_nodata=src.nodata, dst_nodata=0)


def build_geotiffs(s: Settings, pairs: pd.DataFrame, log=print) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {e: [] for e in EPOCHS}
    if s["imagery"]["source"] == "organizer":
        for _, r in pairs.iterrows():
            for epoch, iid, rel in (("pre", r.pre_id, r.pre_path), ("post", r.post_id, r.post_path)):
                dst = tif_dir(s, epoch) / f"{iid}.tif"
                if not dst.exists():
                    png_to_geotiff(s.dataset_dir / rel, dst, r.west, r.south, r.east, r.north)
                out[epoch].append(dst)
    else:
        for epoch in EPOCHS:
            files = sorted(glob.glob(str(s.path(s["imagery"][f"{epoch}_glob"]))))
            if not files:
                raise SystemExit(f"No GeoTIFFs match imagery.{epoch}_glob")
            for f in files:
                dst = tif_dir(s, epoch) / (Path(f).stem + "_3857.tif")
                if not dst.exists():
                    geotiff_to_3857(Path(f), dst)
                out[epoch].append(dst)
    log(f"GeoTIFFs: pre {len(out['pre'])}, post {len(out['post'])}")
    return out


def build_mosaics(s: Settings, tifs: dict[str, list[Path]], log=print) -> None:
    for epoch in EPOCHS:
        srcs = [rasterio.open(p) for p in tifs[epoch]]
        try:
            arr, transform = merge(srcs, nodata=0)
        finally:
            for d in srcs:
                d.close()
        dst = mosaic_path(s, epoch)
        with rasterio.open(dst, "w", width=arr.shape[2], height=arr.shape[1], count=3, dtype="uint8",
                           crs=WEBMERC, transform=transform, nodata=0, **GTIFF_OPTS) as ds:
            ds.write(arr[:3])
        log(f"mosaic {epoch}: {arr.shape[2]}x{arr.shape[1]} px -> {dst.relative_to(s.root)}")


def build_web_overlays(s: Settings, pairs: pd.DataFrame, log=print) -> None:
    """JPEG overlays for the map. Each tile is drawn by MapLibre as an image source on its own corners."""
    px = int(s["imagery"].get("web_overlay_px", 1024))
    index: dict[str, list[dict]] = {e: [] for e in EPOCHS}
    web = s.derived / "web"
    if s["imagery"]["source"] == "organizer":
        for _, r in pairs.iterrows():
            coords = [[r.west, r.north], [r.east, r.north], [r.east, r.south], [r.west, r.south]]
            for epoch, iid, rel in (("pre", r.pre_id, r.pre_path), ("post", r.post_id, r.post_path)):
                dst = web / epoch / f"{iid}.jpg"
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with Image.open(s.dataset_dir / rel) as im:
                        rgba = im.convert("RGBA")
                        bg = Image.new("RGB", rgba.size, (0, 0, 0))
                        bg.paste(rgba, mask=rgba.split()[3])
                        bg.thumbnail((px, px))
                        bg.save(dst, "JPEG", quality=82)
                index[epoch].append({"id": iid, "url": f"/imagery/{epoch}/{iid}.jpg", "coordinates": coords})
    else:
        t = transformer(WEBMERC, WGS84)
        for epoch in EPOCHS:
            with rasterio.open(mosaic_path(s, epoch)) as ds:
                scale = max(ds.width, ds.height) / 4096
                h, w = int(ds.height / max(scale, 1)), int(ds.width / max(scale, 1))
                arr = ds.read(out_shape=(3, h, w), resampling=Resampling.bilinear)
                b = ds.bounds
            dst = web / epoch / "mosaic.jpg"
            dst.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.moveaxis(arr, 0, -1)).save(dst, "JPEG", quality=82)
            (w0, n0), (e0, s0) = t.transform(b.left, b.top), t.transform(b.right, b.bottom)
            index[epoch].append({"id": "mosaic", "url": f"/imagery/{epoch}/mosaic.jpg",
                                 "coordinates": [[w0, n0], [e0, n0], [e0, s0], [w0, s0]]})
    (web / "index.json").write_text(json.dumps(index))
    log(f"web overlays: pre {len(index['pre'])}, post {len(index['post'])}")


class Mosaic:
    """Reads square chips from a per-epoch mosaic by EPSG:3857 bounds."""

    def __init__(self, path: Path):
        self.ds = rasterio.open(path)

    def chip(self, x0: float, y0: float, x1: float, y1: float, px: int) -> tuple[np.ndarray, float]:
        win = window_from_bounds(x0, y0, x1, y1, transform=self.ds.transform)
        arr = self.ds.read((1, 2, 3), window=win, out_shape=(3, px, px), boundless=True, fill_value=0,
                           resampling=Resampling.bilinear)
        rgb = np.moveaxis(arr, 0, -1)
        valid = float((rgb.max(axis=2) > 0).mean())
        return rgb, valid

    def close(self) -> None:
        self.ds.close()
