"""FastAPI app: serves the map UI, derived layers, chips and analysis endpoints."""
from __future__ import annotations

import json
import threading
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from floodlens.config import ROOT, get_settings
from floodlens.geo import now_iso, read_geojson, write_geojson

s = get_settings()
app = FastAPI(title="FloodLens")
LOCK = threading.Lock()
LAYERS = {"road_segments", "road_edges", "buildings", "facilities", "cutoff_zones_strict", "cutoff_zones_permissive"}
HIGHWAY_RANK = {"primary": 0, "secondary": 1, "tertiary": 2, "unclassified": 3, "residential": 4}

for mount, path in (("/static", ROOT / "web"), ("/chips", s.chips), ("/imagery", s.derived / "web")):
    path.mkdir(parents=True, exist_ok=True)
    app.mount(mount, StaticFiles(directory=path), name=mount.strip("/"))

_net = None


def net():
    global _net
    if _net is None:
        from floodlens.analysis import Network
        _net = Network(s)
    return _net


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "vlm_mock": s.vlm_mock, "model": s.vlm_model, "time": now_iso()}


@app.get("/api/config")
def config():
    idx = s.derived / "web" / "index.json"
    return {"event": s["event"], "bbox": s.bbox, "aoi": s["aoi"]["name"], "model": s.vlm_model,
            "layers": sorted(LAYERS), "overlays": json.loads(idx.read_text()) if idx.exists() else {"pre": [], "post": []}}


@app.get("/api/layers/{name}")
def layer(name: str):
    if name not in LAYERS:
        raise HTTPException(404, "unknown layer")
    p = s.derived / f"{name}.geojson"
    if not p.exists():
        return {"type": "FeatureCollection", "features": []}
    return JSONResponse(json.loads(p.read_text()))


@app.get("/api/review")
def review_queue(status: str = "uncertain", limit: int = 100):
    segs = [f["properties"] for f in read_geojson(s.derived / "road_segments.geojson")
            if f["properties"]["status"] == status and f["properties"].get("chip_url")
            and f["properties"]["status_source"] != "human"]
    segs.sort(key=lambda p: (HIGHWAY_RANK.get(p.get("highway") or "", 9), -(p.get("length_m") or 0)))
    return segs[:limit]


class ReviewBody(BaseModel):
    status: Literal["open", "blocked", "uncertain"]


@app.post("/api/review/{segment_id}")
def review(segment_id: str, body: ReviewBody):
    with LOCK:
        feats = read_geojson(s.derived / "road_segments.geojson")
        hit = next((f for f in feats if f["properties"]["id"] == segment_id), None)
        if hit is None:
            raise HTTPException(404, "unknown segment")
        hit["properties"].update(status=body.status, status_source="human", updated_at=now_iso())
        write_geojson(s.derived / "road_segments.geojson", feats)
        summary = net().run()
    return {"segment": hit["properties"], "analysis": summary}


@app.get("/api/summary")
def summary():
    p = s.derived / "summary.json"
    if not p.exists():
        with LOCK:
            net().run()
    return json.loads(p.read_text())


_ctx = None


@app.get("/api/context/{segment_id}")
def context(segment_id: str):
    """Per-tile context from the organizer metadata for the tile under this road segment."""
    global _ctx
    import pandas as pd
    from shapely.geometry import shape
    if _ctx is None:
        m = pd.read_csv(s.dataset_dir / "image_metadata.csv", dtype="string", keep_default_na=False)
        pairs = pd.read_csv(s.derived / "aoi_pairs.csv")
        _ctx = m[m.image_id.isin(pairs.post_id)].copy()
        for c in ("west", "south", "east", "north"):
            _ctx[c] = pd.to_numeric(_ctx[c])
    f = next((f for f in read_geojson(s.derived / "road_segments.geojson") if f["properties"]["id"] == segment_id), None)
    if f is None:
        raise HTTPException(404, "unknown segment")
    c = shape(f["geometry"]).centroid
    row = _ctx[(_ctx.west <= c.x) & (_ctx.east >= c.x) & (_ctx.south <= c.y) & (_ctx.north >= c.y)]
    if row.empty:
        return {"tile": None}
    r = row.iloc[0]
    keep = ["image_id", "acquisition_date", "locality", "fema_flood_zones", "fema_sfha_tf", "precipitation_mm",
            "preceding_3d_precipitation_mm", "preceding_7d_precipitation_mm", "precipitation_station_name",
            "usgs_gage_height_station_name", "usgs_daily_mean_gage_height_si", "usgs_gage_height_si_unit",
            "usgs_daily_mean_discharge_si", "usgs_discharge_si_unit", "elevation_m"]
    return {"tile": {k: r[k] for k in keep if k in r.index and r[k] != ""}}


@app.post("/api/analyze")
def analyze():
    with LOCK:
        return net().run()


class RouteBody(BaseModel):
    lat: float
    lon: float
    mode: Literal["strict", "permissive"] = "permissive"
    target_kinds: list[str] = ["shelter", "hospital", "exit"]


@app.post("/api/route")
def route(body: RouteBody):
    with LOCK:
        n = net()
        n.recompute_inputs()
        return n.route(body.lat, body.lon, body.mode, tuple(body.target_kinds))
