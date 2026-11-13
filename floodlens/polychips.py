"""Before/after chips around a polygon (building, field, open area) with its outline in magenta."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw
from shapely.geometry import box

from floodlens.config import Settings
from floodlens.geo import WEBMERC, WGS84, reproject_geom, utm_crs_for
from floodlens.imagery import TileIndex

MAGENTA = (255, 0, 255, 170)


class PolyChipper:
    def __init__(self, s: Settings, kind: str, buffer_m: float, min_side_m: float, px: int):
        self.s, self.kind, self.buffer_m, self.min_side, self.px = s, kind, buffer_m, min_side_m, px
        tiles = pd.read_csv(s.derived / "tiles.csv", keep_default_na=False)
        self.idx = {"post": TileIndex(s, tiles, "post")}
        if s.paired:
            self.idx["pre"] = TileIndex(s, tiles, "pre")
        self.utm = utm_crs_for(s.bbox)
        self.dir = s.chips / kind
        self.dir.mkdir(parents=True, exist_ok=True)

    def files(self, cid: str) -> dict[str, Path]:
        return {e: self.dir / f"{cid}_{e}.jpg" for e in ("pre", "post")}

    def url(self, cid: str, epoch: str = "post") -> str:
        return f"/chips/{self.s.area_id}/{self.kind}/{cid}_{epoch}.jpg"

    def make(self, cid: str, geom_wgs, force: bool = False) -> tuple[dict[str, Path] | None, str | None, str | None]:
        """Returns (files or None if no imagery, tile_id, after_date)."""
        files = self.files(cid)
        g_utm = reproject_geom(geom_wgs, WGS84, self.utm)
        minx, miny, maxx, maxy = g_utm.buffer(self.buffer_m).bounds
        side = max(maxx - minx, maxy - miny, self.min_side)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
        bounds = reproject_geom(box(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2), self.utm, WEBMERC).bounds
        g3857 = reproject_geom(geom_wgs, WGS84, WEBMERC)
        c = g3857.centroid
        tile_id, date = self.idx["post"].tile_at(c.x, c.y)
        if not force and files["post"].exists():
            return files, tile_id, date
        x0, y0, x1, y1 = bounds
        for epoch, ti in self.idx.items():
            rgb, valid = ti.chip(*bounds, self.px)
            if valid < 0.6:
                return None, tile_id, date
            im = Image.fromarray(rgb).convert("RGBA")
            over = Image.new("RGBA", im.size, (0, 0, 0, 0))
            polys = list(g3857.geoms) if hasattr(g3857, "geoms") else [g3857]
            for poly in polys:
                pts = [((x - x0) / (x1 - x0) * self.px, (y1 - y) / (y1 - y0) * self.px) for x, y in poly.exterior.coords]
                ImageDraw.Draw(over).line(pts, fill=MAGENTA, width=max(2, self.px // 160))
            Image.alpha_composite(im, over).convert("RGB").save(files[epoch], "JPEG", quality=88)
        return files, tile_id, date

    def close(self):
        for t in self.idx.values():
            t.close()
