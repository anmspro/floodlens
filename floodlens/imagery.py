"""Imagery: organizer PNG tiles -> one GeoTIFF per tile (EPSG:3857) -> road chips and web map tiles.

The organizer PNGs carry no georeferencing, but each row of image_metadata.csv gives
WGS84 bounds and the pixel grid is Web Mercator, so the corners projected to EPSG:3857
define the exact affine transform. Each image is exactly one slippy-map tile at zoom z
(1024 px), so web tiles at zoom z+2 (256 px) are a straight cut, and lower zooms are
built by downsampling.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import Resampling
from rasterio.windows import from_bounds as window_from_bounds

from floodlens.config import Settings
from floodlens.geo import WEBMERC, WGS84, transformer

EPOCHS = ("pre", "post")
GTIFF_OPTS = dict(driver="GTiff", tiled=True, blockxsize=256, blockysize=256, compress="deflate", predictor=2)


def tif_path(s: Settings, epoch: str, image_id: str) -> Path:
    return s.data / "geotiff" / epoch / f"{image_id}.tif"


def _rgba(png: Path) -> np.ndarray:
    with Image.open(png) as im:
        return np.asarray(im.convert("RGBA"))


def png_to_geotiff(png: Path, dst: Path, west: float, south: float, east: float, north: float) -> None:
    t = transformer(WGS84, WEBMERC)
    x0, y0 = t.transform(west, south)
    x1, y1 = t.transform(east, north)
    rgba = _rgba(png)
    rgb = np.maximum(rgba[..., :3], 1)          # 0 is reserved for nodata
    rgb[rgba[..., 3] == 0] = 0
    h, w = rgb.shape[:2]
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.tif")
    with rasterio.open(tmp, "w", width=w, height=h, count=3, dtype="uint8", crs=WEBMERC,
                       transform=from_bounds(x0, y0, x1, y1, w, h), nodata=0, **GTIFF_OPTS) as ds:
        ds.write(np.moveaxis(rgb, -1, 0))
    tmp.replace(dst)


def build_geotiffs(s: Settings, tiles: pd.DataFrame, log=print) -> int:
    n = 0
    for r in tiles.itertuples():
        for epoch, iid, rel in (("pre", r.pre_id, r.pre_path), ("post", r.post_id, r.post_path)):
            if not isinstance(iid, str) or not iid:
                continue
            dst = tif_path(s, epoch, iid)
            if not dst.exists():
                png_to_geotiff(s.dataset_dir / rel, dst, r.west, r.south, r.east, r.north)
                n += 1
    log(f"[{s.area_id}] GeoTIFFs written: {n} new")
    return n


# ---------------------------------------------------------------- chips

class TileIndex:
    """Reads square chips by EPSG:3857 bounds from whichever per-tile GeoTIFFs they overlap."""

    def __init__(self, s: Settings, tiles: pd.DataFrame, epoch: str):
        t = transformer(WGS84, WEBMERC)
        self.items = []
        col_id = "pre_id" if epoch == "pre" else "post_id"
        for r in tiles.itertuples():
            iid = getattr(r, col_id)
            if not isinstance(iid, str) or not iid:
                continue
            x0, y0 = t.transform(r.west, r.south)
            x1, y1 = t.transform(r.east, r.north)
            self.items.append(((x0, y0, x1, y1), tif_path(s, epoch, iid), iid, getattr(r, f"{epoch}_date")))
        self._open: dict[Path, rasterio.DatasetReader] = {}

    def _ds(self, p: Path):
        if p not in self._open:
            self._open[p] = rasterio.open(p)
        return self._open[p]

    def tile_at(self, x: float, y: float):
        for b, p, iid, date in self.items:
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                return iid, date
        return None, None

    def chip(self, x0: float, y0: float, x1: float, y1: float, px: int) -> tuple[np.ndarray, float]:
        hits = [p for b, p, _, _ in self.items if b[0] < x1 and b[2] > x0 and b[1] < y1 and b[3] > y0]
        if not hits:
            return np.zeros((px, px, 3), np.uint8), 0.0
        if len(hits) == 1:
            ds = self._ds(hits[0])
            win = window_from_bounds(x0, y0, x1, y1, transform=ds.transform)
            arr = ds.read((1, 2, 3), window=win, out_shape=(3, px, px), boundless=True, fill_value=0,
                          resampling=Resampling.bilinear)
        else:
            res = (x1 - x0) / px
            arr, _ = merge([self._ds(p) for p in hits], bounds=(x0, y0, x1, y1), res=(res, res), nodata=0,
                           resampling=Resampling.bilinear)
            arr = arr[:3]
            if arr.shape[1:] != (px, px):
                arr = np.moveaxis(np.asarray(Image.fromarray(np.moveaxis(arr, 0, -1)).resize((px, px))), -1, 0)
        rgb = np.moveaxis(arr, 0, -1)
        return rgb, float((rgb.max(axis=2) > 0).mean())

    def close(self) -> None:
        for d in self._open.values():
            d.close()
        self._open.clear()


# ---------------------------------------------------------------- web tiles

def build_web_tiles(s: Settings, tiles: pd.DataFrame, log=print) -> dict:
    """XYZ pyramid per epoch: data/tiles/<area>/<epoch>/{z}/{x}/{y}.<fmt>"""
    fmt = s.get("imagery.tile_format", "webp")
    zmin = int(s.get("imagery.min_zoom", 12))
    out = {}
    for epoch in EPOCHS:
        col_id, col_path = f"{epoch}_id", f"{epoch}_path"
        root = s.tiles / epoch
        made: dict[int, set[tuple[int, int]]] = {}
        native = None
        for r in tiles.itertuples():
            iid, rel = getattr(r, col_id), getattr(r, col_path)
            if not isinstance(iid, str) or not iid:
                continue
            zn = int(r.z) + 2
            native = zn if native is None else max(native, zn)
            rgba = Image.fromarray(_rgba(s.dataset_dir / rel)).resize((1024, 1024))
            for i in range(4):
                for j in range(4):
                    x, y = r.tx * 4 + i, r.ty * 4 + j
                    dst = root / str(zn) / str(x) / f"{y}.{fmt}"
                    made.setdefault(zn, set()).add((x, y))
                    if dst.exists():
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    rgba.crop((i * 256, j * 256, (i + 1) * 256, (j + 1) * 256)).save(dst, quality=80)
        if native is None:
            continue
        for z in range(native - 1, zmin - 1, -1):
            parents = {(x // 2, y // 2) for x, y in made.get(z + 1, set())}
            made[z] = parents
            for px_, py_ in parents:
                dst = root / str(z) / str(px_) / f"{py_}.{fmt}"
                if dst.exists():
                    continue
                canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
                for i in range(2):
                    for j in range(2):
                        child = root / str(z + 1) / str(px_ * 2 + i) / f"{py_ * 2 + j}.{fmt}"
                        if child.exists():
                            with Image.open(child) as c:
                                canvas.paste(c.convert("RGBA"), (i * 256, j * 256))
                dst.parent.mkdir(parents=True, exist_ok=True)
                canvas.resize((256, 256), Image.LANCZOS).save(dst, quality=80)
        out[epoch] = {"minzoom": zmin, "maxzoom": native, "url": f"/tiles/{s.area_id}/{epoch}/{{z}}/{{x}}/{{y}}.{fmt}"}
        log(f"[{s.area_id}] web tiles {epoch}: zoom {zmin}-{native}, {sum(len(v) for v in made.values())} tiles")
    import json
    (s.derived / "web_tiles.json").write_text(json.dumps(out))
    return out
