"""FastAPI app: map UI, per-area layers, chips, web tiles, analysis, routing, briefs."""
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

BASE = get_settings()
app = FastAPI(title="FloodLens")
LOCK = threading.Lock()
LAYERS = {"road_segments", "road_edges", "buildings", "facilities", "cutoff_zones_strict", "cutoff_zones_permissive",
          "coverage", "context_warnings", "context_tracts", "impact_buildings", "impact_fields", "relief_sites",
          "sa_crossings", "sa_sensors", "context_reports"}
HIGHWAY_RANK = {"primary": 0, "secondary": 1, "tertiary": 2, "unclassified": 3, "residential": 4}

for mount, path in (("/static", ROOT / "web"), ("/chips", BASE.data / "chips"), ("/tiles", BASE.data / "tiles")):
    path.mkdir(parents=True, exist_ok=True)
    app.mount(mount, StaticFiles(directory=path), name=mount.strip("/"))

_nets: dict[str, object] = {}


def area(area_id: str):
    if area_id not in BASE.area_ids:
        raise HTTPException(404, f"unknown area {area_id}")
    s = BASE.for_area(area_id)
    if not (s.derived / "area.json").exists():
        raise HTTPException(409, f"area {area_id} is not processed yet")
    return s


def net(s):
    if s.area_id not in _nets:
        from floodlens.analysis import Network
        _nets[s.area_id] = Network(s)
    return _nets[s.area_id]


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "vlm_mock": BASE.vlm_mock, "model": BASE.vlm_model, "time": now_iso()}


@app.get("/api/areas")
def areas():
    out = []
    for a in BASE.area_ids:
        s = BASE.for_area(a)
        info_p, sum_p = s.derived / "area.json", s.derived / "summary.json"
        info = json.loads(info_p.read_text()) if info_p.exists() else {"id": a, "name": s.area["name"]}
        info["ready"] = info_p.exists()
        info["partial"] = not ((s.derived / "summary.json").exists() and (s.derived / "road_segments.geojson").exists())
        info["summary"] = json.loads(sum_p.read_text()) if sum_p.exists() else None
        out.append(info)
    return {"default": BASE.raw.get("default_area"), "areas": out}


@app.get("/api/{area_id}/config")
def config(area_id: str):
    s = area(area_id)
    wt = s.derived / "web_tiles.json"
    return {"area": s.area_info, "event": s.event, "model": BASE.vlm_model, "paired": s.paired,
            "layers": sorted(LAYERS), "tiles": json.loads(wt.read_text()) if wt.exists() else {}}


@app.get("/api/{area_id}/layers/{name}")
def layer(area_id: str, name: str):
    s = area(area_id)
    if name not in LAYERS:
        raise HTTPException(404, "unknown layer")
    p = s.derived / f"{name}.geojson"
    if not p.exists():
        return {"type": "FeatureCollection", "features": []}
    return JSONResponse(json.loads(p.read_text()))


@app.get("/api/{area_id}/summary")
def summary(area_id: str):
    s = area(area_id)
    p = s.derived / "summary.json"
    if not p.exists() and (s.derived / "graph.graphml").exists() and (s.derived / "road_segments.geojson").exists():
        with LOCK:
            net(s).run()
    if not p.exists():
        return {"segments": {}, "people_cut_off": {"strict": None, "permissive": None}, "facilities_cut_off": [],
                "unverified": {}, "partial": True}
    return json.loads(p.read_text())


@app.get("/api/{area_id}/impact")
def impact(area_id: str):
    s = area(area_id)
    p = s.derived / "impact.json"
    return json.loads(p.read_text()) if p.exists() else None


@app.get("/api/{area_id}/relief")
def relief(area_id: str):
    s = area(area_id)
    p = s.derived / "relief.json"
    return json.loads(p.read_text()) if p.exists() else None


@app.get("/api/{area_id}/area-context")
def area_context(area_id: str):
    """River gauges and county flood-zone homes for the area (snapshots)."""
    s = area(area_id)
    out = {}
    for key, name in (("gauges", "context_gauges.json"), ("county", "context_county.json")):
        p = s.derived / name
        out[key] = json.loads(p.read_text()) if p.exists() else None
    return out


@app.get("/api/{area_id}/services")
def services(area_id: str):
    """Emergency services in and around the area, with reachability from outside over passable roads."""
    import networkx as nx
    import numpy as np
    from floodlens.osm import emergency_services
    s = area(area_id)
    if not (s.derived / "graph.graphml").exists():
        return []
    n = net(s)
    svcs = emergency_services(s)
    H = n.undirected("permissive")
    comp = {x: i for i, c in enumerate(nx.connected_components(H)) for x in c}
    exits = {comp[e] for e in n.exit_nodes if e in comp}
    ids = list(n.G.nodes)
    xy = np.array([n.to_utm.transform(n.G.nodes[i]["x"], n.G.nodes[i]["y"]) for i in ids])
    out = []
    for sv in svcs:
        px, py = n.to_utm.transform(sv["lon"], sv["lat"])
        d = (xy[:, 0] - px) ** 2 + (xy[:, 1] - py) ** 2
        k = int(d.argmin())
        inside = float(d[k] ** 0.5) < 300
        status = ("reachable" if comp.get(ids[k]) in exits else "cut off") if inside else "outside the photos"
        out.append({**sv, "status": status})
    order = {"cut off": 0, "reachable": 1, "outside the photos": 2}
    return sorted(out, key=lambda r: (order[r["status"]], r["kind"], r["name"]))


@app.get("/api/{area_id}/review")
def review_queue(area_id: str, status: str = "uncertain", limit: int = 100):
    s = area(area_id)
    if not (s.derived / "road_segments.geojson").exists():
        return []
    segs = [f["properties"] for f in read_geojson(s.derived / "road_segments.geojson")
            if f["properties"]["status"] == status and f["properties"].get("chip_url")
            and f["properties"]["status_source"] != "human"]
    segs.sort(key=lambda p: (HIGHWAY_RANK.get(p.get("highway") or "", 9), -(p.get("length_m") or 0)))
    return segs[:limit]


class ReviewBody(BaseModel):
    status: Literal["open", "blocked", "uncertain"]
    reviewer: str | None = None


@app.post("/api/{area_id}/review/{segment_id}")
def review(area_id: str, segment_id: str, body: ReviewBody):
    s = area(area_id)
    with LOCK:
        feats = read_geojson(s.derived / "road_segments.geojson")
        hit = next((f for f in feats if f["properties"]["id"] == segment_id), None)
        if hit is None:
            raise HTTPException(404, "unknown segment")
        p = hit["properties"]
        if p["status_source"] == "vlm":
            p["vlm_status"] = p["status"]
        p.update(status=body.status, status_source="human", updated_at=now_iso(), reviewer=body.reviewer)
        write_geojson(s.derived / "road_segments.geojson", feats)
        from floodlens import evaluate
        evaluate.record_label(s, segment_id, body.status, body.reviewer, p.get("vlm_status"))
        result = net(s).run()
    return {"segment": p, "analysis": result}


_meta = None


@app.get("/api/{area_id}/context/{segment_id}")
def context(area_id: str, segment_id: str):
    """Per-tile organizer metadata + public context for the tile under this road piece."""
    global _meta
    import pandas as pd
    s = area(area_id)
    if _meta is None:
        _meta = pd.read_csv(s.dataset_dir / "image_metadata.csv", dtype="string", keep_default_na=False).set_index("image_id")
    f = next((f for f in read_geojson(s.derived / "road_segments.geojson") if f["properties"]["id"] == segment_id), None)
    if f is None:
        raise HTTPException(404, "unknown segment")
    tid = f["properties"].get("tile_id")
    tile = None
    if tid and tid in _meta.index:
        r = _meta.loc[tid]
        keep = ["acquisition_date", "locality", "fema_flood_zones", "fema_sfha_tf", "precipitation_mm",
                "preceding_3d_precipitation_mm", "preceding_7d_precipitation_mm", "precipitation_station_name",
                "usgs_gage_height_station_name", "usgs_daily_mean_gage_height_si", "usgs_gage_height_si_unit",
                "usgs_daily_mean_discharge_si", "usgs_discharge_si_unit", "elevation_m"]
        tile = {"image_id": tid, **{k: r[k] for k in keep if k in r.index and r[k] != ""}}
    try:
        from floodlens import sources
        public = sources.context_for_segment(s, f)
    except Exception as e:  # noqa: BLE001
        public = {"error": str(e)}
    return {"tile": tile, "public": public}


@app.post("/api/{area_id}/analyze")
def analyze(area_id: str):
    s = area(area_id)
    with LOCK:
        return net(s).run()


class RouteBody(BaseModel):
    lat: float
    lon: float
    mode: Literal["strict", "permissive"] = "permissive"
    target_kinds: list[str] = ["shelter", "hospital", "exit"]


@app.post("/api/{area_id}/route")
def route(area_id: str, body: RouteBody):
    s = area(area_id)
    with LOCK:
        n = net(s)
        n.recompute_inputs()
        return n.route(body.lat, body.lon, body.mode, tuple(body.target_kinds))


class BriefBody(BaseModel):
    zone_id: str | None = None
    mode: Literal["strict", "permissive"] = "permissive"
    lang: Literal["en", "es"] = "en"
    prefer_cache: bool = False


@app.post("/api/{area_id}/brief")
def brief(area_id: str, body: BriefBody):
    s = area(area_id)
    from floodlens import brief as b
    try:
        return b.make_brief(s, body.zone_id, body.mode, body.lang, prefer_cache=body.prefer_cache)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"model unreachable and no saved brief for this zone ({type(e).__name__})")


@app.get("/api/{area_id}/sources")
def sources_status(area_id: str):
    s = area(area_id)
    from floodlens import sources
    return sources.status(s)


class FaultBody(BaseModel):
    source: str
    mode: Literal["none", "down", "slow", "corrupt"]


@app.post("/api/admin/fault")
def fault(body: FaultBody):
    from floodlens import sources
    return sources.set_fault(body.source, body.mode)
