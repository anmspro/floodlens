"""Coordinate helpers. Stored GeoJSON is EPSG:4326. Distances use local UTM."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pyproj import CRS, Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as shp_transform

WGS84 = "EPSG:4326"
WEBMERC = "EPSG:3857"


def utm_crs_for(bbox: tuple[float, float, float, float]) -> CRS:
    w, s, e, n = bbox
    lon, lat = (w + e) / 2, (s + n) / 2
    zone = int((lon + 180) // 6) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def transformer(src: Any, dst: Any) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def reproject_geom(geom, src: Any, dst: Any):
    t = transformer(src, dst)
    return shp_transform(t.transform, geom)


def bbox_polygon(bbox):
    return box(*bbox)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_geojson(path: Path, features: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "features": list(features)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(fc))
    tmp.replace(path)


def read_geojson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("features", [])


def feature(geom, props: dict) -> dict:
    return {"type": "Feature", "id": props.get("id"), "geometry": mapping(geom), "properties": props}


def geom_of(f: dict):
    return shape(f["geometry"])
