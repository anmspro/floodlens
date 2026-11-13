"""San Antonio mode: low-water crossings + SmartSA flood sensors (organizer extra data).

There are no flood photos for San Antonio, so this mode is a planning scenario instead of a
photo check: "heavy rain, every low-water crossing floods". It reuses the same road graph,
cut-off and routing engine to show which neighbourhoods have no way out except across a
low-water crossing, ranked by census vulnerability. SmartSA streetlight flood sensors are
shown with their alert history.
"""
from __future__ import annotations

import csv
import io
import json
import math
from collections import defaultdict

import pandas as pd

from floodlens.config import Settings
from floodlens.geo import WGS84, feature, now_iso, read_geojson, reproject_geom, utm_crs_for, write_geojson

CROSSINGS_URL = ("https://services.arcgis.com/g1fRTDLeMgspWrYp/arcgis/rest/services/LowWaterCrossings/FeatureServer/0/query"
                 "?where=1%3D1&outFields=*&f=geojson&outSR=4326&resultOffset={off}&resultRecordCount=1000")
SENSOR_DUMPS = {
    "Brooks": "https://data.sanantonio.gov/datastore/dump/c0c546cd-fbfa-479c-b1ca-ac7a7244aa53",
    "Downtown": "https://data.sanantonio.gov/datastore/dump/346d33b7-0b74-4b92-aa22-452456954ed1",
    "Medical Center": "https://data.sanantonio.gov/datastore/dump/aaf0e6a5-8df7-4f0c-bf22-7b2f6ad6943d",
}
BUFFER_M = 1000


def fetch(s: Settings, log=print) -> dict:
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
    from floodlens.sources import _download_once
    feats, off = [], 0
    while True:
        p = _download_once(s, CROSSINGS_URL.format(off=off), f"cosa_lwc_{off}.geojson", timeout=120)
        js = json.loads(p.read_text())
        feats += js.get("features", [])
        if not js.get("properties", {}).get("exceededTransferLimit"):
            break
        off += 1000
    s.derived.mkdir(parents=True, exist_ok=True)
    cross = [{"type": "Feature", "geometry": f["geometry"], "properties": {"id": f["properties"].get("CartID") or f"lwc_{i}",
              "name": f["properties"].get("Name")}} for i, f in enumerate(feats) if f.get("geometry")]
    write_geojson(s.derived / "sa_crossings.geojson", cross)
    log(f"[{s.area_id}] low-water crossings: {len(cross)}")
    # sensors: one summary row per sensor from the full CKAN dumps
    rows = []
    for zone, url in SENSOR_DUMPS.items():
        try:
            p = _download_once(s, url, f"smartsa_flood_{zone.replace(' ', '_').lower()}.csv", timeout=600)
        except Exception as e:  # noqa: BLE001
            log(f"[{s.area_id}] sensor dump {zone} not downloaded: {e}")
            continue
        df = pd.read_csv(p, low_memory=False)
        for c in ("DistToWL_m", "DistToDF_m", "LAT", "LONG"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[df.DistToWL_m.between(0, 50)]
        for sid, g in df.groupby("Sensor_id"):
            base = g.DistToWL_m.median()
            rise = float(base - g.DistToWL_m.min()) if len(g) else None
            rows.append({"id": f"smartsa_{sid}", "zone": zone, "lat": g.LAT.median(), "lon": g.LONG.median(),
                         "readings": int(len(g)), "first": str(g.DateTime.min()), "last": str(g.DateTime.max()),
                         "alerts": int((g.AlertTriggered.astype(str).str.upper() == "Y").sum()),
                         "max_rise_m": round(rise, 2) if rise is not None else None,
                         "status_last": str(g.sort_values("DateTime").SensorStatus.iloc[-1])})
    sens = [feature(Point(r["lon"], r["lat"]), r) for r in rows if not (math.isnan(r["lat"]) or math.isnan(r["lon"]))]
    write_geojson(s.derived / "sa_sensors.geojson", sens)
    log(f"[{s.area_id}] SmartSA flood sensors: {len(sens)}")
    # coverage = 1.5 km around every crossing
    pts = [Point(*f["geometry"]["coordinates"][:2]) for f in cross]
    lon0 = sum(p.x for p in pts) / len(pts)
    lat0 = sum(p.y for p in pts) / len(pts)
    utm = utm_crs_for((lon0, lat0, lon0, lat0))
    cov = reproject_geom(unary_union([reproject_geom(p, WGS84, utm).buffer(BUFFER_M) for p in pts]), utm, WGS84)
    (s.derived / "coverage.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": mapping(cov)}]}))
    info = {"id": s.area_id, "name": s.area["name"], "event": s.event["name"], "mode": "scenario",
            "bbox": [round(v, 7) for v in cov.bounds], "n_tiles": 0, "zooms": [], "after_dates": [], "before_dates": [],
            "localities": ["San Antonio"], "crossings": len(cross), "sensors": len(sens)}
    (s.derived / "area.json").write_text(json.dumps(info, indent=1))
    pd.DataFrame(columns=["post_id", "post_path", "post_date", "pre_id", "pre_path", "pre_date", "west", "south", "east",
                          "north", "z", "tx", "ty", "locality", "spatial_block", "pair_key"]).to_csv(s.derived / "tiles.csv", index=False)
    return info


def apply_scenario(s: Settings, scenario: str = "all_crossings_flooded", log=print) -> dict:
    """Mark the road piece at every low-water crossing as blocked; every other road is assumed open."""
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    utm = utm_crs_for(s.bbox)
    segs = read_geojson(s.derived / "road_segments.geojson")
    geoms = [reproject_geom(shape(f["geometry"]), WGS84, utm) for f in segs]
    tree = STRtree(geoms)
    hit: dict[str, str] = {}
    for c in read_geojson(s.derived / "sa_crossings.geojson"):
        p = reproject_geom(shape(c["geometry"]), WGS84, utm)
        idx = [int(i) for i in tree.query(p.buffer(40))]
        if not idx:
            continue
        best = min(idx, key=lambda i: geoms[i].distance(p))
        if geoms[best].distance(p) <= 40:
            hit[segs[best]["properties"]["id"]] = c["properties"]["name"] or "low-water crossing"
    t = now_iso()
    for f in segs:
        p = f["properties"]
        if p["status_source"] == "human":
            continue
        if scenario == "all_crossings_flooded" and p["id"] in hit:
            p.update(status="blocked", status_source="scenario", confidence=None, updated_at=t,
                     label={"reason": f"Low-water crossing: {hit[p['id']]}. Scenario: every crossing floods.", "rule": "scenario"})
        else:
            p.update(status="open", status_source="assumed", confidence=None, updated_at=t,
                     label={"reason": "Not a low-water crossing; assumed passable in this scenario.", "rule": "scenario"})
    write_geojson(s.derived / "road_segments.geojson", segs)
    out = {"crossings_matched_to_roads": len(hit), "scenario": scenario}
    log(f"[{s.area_id}] scenario {scenario}: {out}")
    return out
