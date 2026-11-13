"""OSM preparation: drive network, segments (<=100 m), buildings, facilities.

Two-way streets appear twice in an OSMnx drive graph (u->v and v->u). Both directions
share one canonical edge id "{min(u,v)}_{max(u,v)}_{key}" so each road is labelled once.
"""
from __future__ import annotations

import csv
import math
import shutil
from pathlib import Path

import numpy as np
import osmnx as ox
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import substring

from floodlens.config import Settings
from floodlens.geo import (WGS84, feature, now_iso, reproject_geom, utm_crs_for, write_geojson)

RESIDENTIAL = {"house", "residential", "apartments", "detached", "semidetached_house", "terrace",
               "bungalow", "dormitory", "yes"}
FACILITY_TAGS = {
    "amenity": ["hospital", "clinic", "doctors", "fire_station", "police", "school", "social_facility"],
    "healthcare": ["nursing_home", "dialysis", "hospital", "clinic"],
}


def edge_id(u: int, v: int, k: int) -> str:
    a, b = sorted((int(u), int(v)))
    return f"{a}_{b}_{int(k)}"


def first(v):
    if isinstance(v, list):
        return v[0] if v else None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def split_line(line: LineString, max_len: float, split_if_longer: float) -> list[LineString]:
    L = line.length
    if L <= split_if_longer:
        return [line]
    n = math.ceil(L / max_len)
    return [substring(line, i * L / n, (i + 1) * L / n) for i in range(n)]


def _osmnx_settings(s: Settings) -> None:
    ox.settings.cache_folder = str(s.cache / "osmnx")
    ox.settings.use_cache = True


def snapshot_dir(s: Settings) -> Path:
    return s.data / "raw" / "snapshots" / "osm"


def load_graph(s: Settings, log=print):
    """Live fetch with snapshot fallback. Returns (graph, meta)."""
    _osmnx_settings(s)
    w, so, e, n = s.bbox
    snap = snapshot_dir(s) / "graph.graphml"
    try:
        G = ox.graph_from_bbox(bbox=(w, so, e, n), network_type="drive", simplify=True, truncate_by_edge=True)
        snap.parent.mkdir(parents=True, exist_ok=True)
        ox.save_graphml(G, snap)
        return G, {"source": "osm", "status": "live", "fetched_at": now_iso(), "error": None}
    except Exception as ex:  # noqa: BLE001
        if snap.exists():
            log(f"OSM live fetch failed ({ex}); using snapshot {snap}")
            return ox.load_graphml(snap), {"source": "osm", "status": "snapshot", "fetched_at": None, "error": str(ex)}
        raise


def _features(s: Settings, tags: dict, name: str, log=print):
    w, so, e, n = s.bbox
    snap = snapshot_dir(s) / f"{name}.gpkg"
    try:
        gdf = ox.features_from_bbox(bbox=(w, so, e, n), tags=tags)
        gdf = gdf.reset_index()
        snap.parent.mkdir(parents=True, exist_ok=True)
        keep = [c for c in gdf.columns if c in ("element", "id", "geometry", "building", "name", "amenity",
                                                    "healthcare", "social_facility", "healthcare:speciality")]
        gdf[keep].to_file(snap, driver="GPKG")
        return gdf
    except Exception as ex:  # noqa: BLE001
        import geopandas as gpd
        if "No matching features" in str(ex) or type(ex).__name__ == "InsufficientResponseError":
            log(f"no {name} in AOI")
            return gpd.GeoDataFrame(columns=["element", "id", "geometry"], geometry="geometry", crs=WGS84)
        if snap.exists():
            log(f"OSM {name} fetch failed ({ex}); using snapshot")
            return gpd.read_file(snap)
        raise


def facility_kind(row) -> str | None:
    am, hc = first(row.get("amenity")), first(row.get("healthcare"))
    sf, spec = first(row.get("social_facility")), first(row.get("healthcare:speciality"))
    if am == "hospital" or hc == "hospital":
        return "hospital"
    if hc == "dialysis" or (isinstance(spec, str) and "dialysis" in spec):
        return "dialysis"
    if hc == "nursing_home" or (am == "social_facility" and sf == "nursing_home"):
        return "nursing_home"
    if am in ("clinic", "doctors") or hc == "clinic":
        return "clinic"
    if am in ("fire_station", "police", "school"):
        return am
    return None


def nearest_nodes(xy_nodes: np.ndarray, node_ids: list[int], pts: np.ndarray) -> list[int]:
    out = []
    for x, y in pts:
        d = (xy_nodes[:, 0] - x) ** 2 + (xy_nodes[:, 1] - y) ** 2
        out.append(int(node_ids[int(np.argmin(d))]))
    return out


def prepare(s: Settings, log=print) -> dict:
    utm = utm_crs_for(s.bbox)
    G, meta = load_graph(s, log)
    s.derived.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(G, s.derived / "graph.graphml")
    Gp = ox.project_graph(G, to_crs=utm)
    rc = s["roads"]
    t = now_iso()

    seg_feats, edge_feats, seen = [], {}, set()
    for u, v, k, d in Gp.edges(keys=True, data=True):
        eid = edge_id(u, v, k)
        if eid in seen:
            continue
        seen.add(eid)
        geom = d.get("geometry") or LineString([(Gp.nodes[u]["x"], Gp.nodes[u]["y"]), (Gp.nodes[v]["x"], Gp.nodes[v]["y"])])
        pieces = split_line(geom, rc["max_segment_m"], rc["split_if_longer_than_m"])
        seg_ids = []
        for i, piece in enumerate(pieces):
            sid = f"{eid}_{i}"
            seg_ids.append(sid)
            seg_feats.append(feature(reproject_geom(piece, utm, WGS84), {
                "id": sid, "edge_id": eid, "name": first(d.get("name")), "highway": first(d.get("highway")),
                "length_m": round(piece.length, 1), "status": "pending", "status_source": "none",
                "confidence": None, "label": None, "chip_url": None, "prompt_version": None,
                "context": None, "updated_at": t,
            }))
        edge_feats[eid] = feature(reproject_geom(geom, utm, WGS84), {
            "id": eid, "name": first(d.get("name")), "length_m": round(geom.length, 1),
            "status": "pending", "segment_ids": seg_ids,
        })

    node_ids = list(Gp.nodes)
    xy = np.array([[Gp.nodes[i]["x"], Gp.nodes[i]["y"]] for i in node_ids])

    # buildings
    bld = _features(s, {"building": True}, "buildings", log)
    b_feats = []
    if len(bld):
        bld = bld[bld.geometry.type.isin(["Polygon", "MultiPolygon"])].to_crs(utm)
        cents = np.array([[g.centroid.x, g.centroid.y] for g in bld.geometry])
        nn = nearest_nodes(xy, node_ids, cents) if len(cents) else []
        for (_, r), node in zip(bld.iterrows(), nn):
            btag = first(r.get("building"))
            b_feats.append(feature(reproject_geom(r.geometry, utm, WGS84), {
                "id": f"osm:{r.get('element', 'way')}/{r.get('id')}", "building": btag,
                "is_residential": btag in RESIDENTIAL, "node_id": node, "tract_geoid": None,
                "est_pop": None, "zone_id": None, "damage": None,
            }))

    # facilities
    fac = _features(s, FACILITY_TAGS, "facilities", log)
    f_feats = []
    if len(fac):
        fac = fac.to_crs(utm)
        for _, r in fac.iterrows():
            kind = facility_kind(r)
            if not kind:
                continue
            p = r.geometry.representative_point()
            f_feats.append({"id": f"osm:{r.get('element', 'node')}/{r.get('id')}", "name": first(r.get("name")) or kind,
                            "kind": kind, "source": "osm", "capacity": None, "_xy": (p.x, p.y)})
    shelters_csv = s.root / "inputs" / "shelters.csv"
    to_wgs = None
    if shelters_csv.exists():
        from floodlens.geo import transformer
        to_utm = transformer(WGS84, utm)
        with open(shelters_csv) as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                x, y = to_utm.transform(float(row["lon"]), float(row["lat"]))
                f_feats.append({"id": f"manual:{i}", "name": row["name"], "kind": "shelter", "source": "shelters.csv",
                                "capacity": int(row["capacity"]) if row.get("capacity") else None, "_xy": (x, y)})
    if not any(f["kind"] == "shelter" for f in f_feats):
        n_school = 0
        for f in list(f_feats):
            if f["kind"] == "school":
                f_feats.append({**f, "id": f["id"] + "#shelter", "kind": "shelter", "source": "assumed_school"})
                n_school += 1
        log(f"no shelters.csv; treating {n_school} schools as assumed shelters")
    if f_feats:
        nn = nearest_nodes(xy, node_ids, np.array([f["_xy"] for f in f_feats]))
        out_f = []
        for f, node in zip(f_feats, nn):
            x, y = f.pop("_xy")
            out_f.append(feature(reproject_geom(Point(x, y), utm, WGS84),
                                 {**f, "node_id": node, "reachable": None, "reachable_from_outside": None, "site_status": None}))
        f_feats = out_f

    write_geojson(s.derived / "road_segments.geojson", seg_feats)
    write_geojson(s.derived / "road_edges.geojson", edge_feats.values())
    write_geojson(s.derived / "buildings.geojson", b_feats)
    write_geojson(s.derived / "facilities.geojson", f_feats)
    summary = {"segments": len(seg_feats), "edges": len(edge_feats), "nodes": len(node_ids),
               "buildings": len(b_feats), "facilities": len(f_feats), "osm": meta["status"]}
    log(f"prepared: {summary}")
    return summary
