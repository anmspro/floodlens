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


OVERPASS = ["https://overpass-api.de/api", "https://maps.mail.ru/osm/tools/overpass/api",
            "https://overpass.kumi.systems/api"]


def _osmnx_settings(s: Settings) -> None:
    ox.settings.cache_folder = str(s.cache / "osmnx")
    ox.settings.use_cache = True
    ox.settings.requests_timeout = 300
    ox.settings.http_user_agent = "floodlens-hackathon (OSMnx)"


def with_overpass_retry(fn, *a, log=print, **kw):
    """Try each Overpass mirror twice before giving up (the public servers rate-limit)."""
    import time
    last = None
    for attempt in range(2):
        for url in OVERPASS:
            ox.settings.overpass_url = url
            try:
                return fn(*a, **kw)
            except Exception as e:  # noqa: BLE001
                if "No matching features" in str(e) or type(e).__name__ == "InsufficientResponseError":
                    raise
                last = e
                log(f"  overpass {url} failed: {type(e).__name__}; trying next")
                time.sleep(5 * (attempt + 1))
    raise last


def snapshot_dir(s: Settings) -> Path:
    return s.snapshots / s.area_id / "osm"


def coverage(s: Settings, buffer_m: float = 0.0):
    """Union of photo tile footprints (EPSG:4326), optionally buffered in metres."""
    import json
    from shapely.geometry import shape
    cov = shape(json.loads((s.derived / "coverage.geojson").read_text())["features"][0]["geometry"])
    if buffer_m:
        utm = utm_crs_for(s.bbox)
        cov = reproject_geom(reproject_geom(cov, WGS84, utm).buffer(buffer_m), utm, WGS84)
    return cov


def load_graph(s: Settings, log=print):
    """Live fetch with snapshot fallback. Returns (graph, meta)."""
    _osmnx_settings(s)
    snap = snapshot_dir(s) / "graph.graphml"
    try:
        poly = coverage(s, float(s.get("osm.coverage_buffer_m", 150)))
        G = with_overpass_retry(ox.graph_from_polygon, poly, network_type="drive", simplify=True,
                                truncate_by_edge=True, log=log)
        snap.parent.mkdir(parents=True, exist_ok=True)
        ox.save_graphml(G, snap)
        return G, {"source": "osm", "status": "live", "fetched_at": now_iso(), "error": None}
    except Exception as ex:  # noqa: BLE001
        if snap.exists():
            log(f"OSM live fetch failed ({ex}); using snapshot {snap}")
            return ox.load_graphml(snap), {"source": "osm", "status": "snapshot", "fetched_at": None, "error": str(ex)}
        raise


def _features(s: Settings, tags: dict, name: str, log=print):
    _osmnx_settings(s)
    snap = snapshot_dir(s) / f"{name}.gpkg"
    try:
        gdf = with_overpass_retry(ox.features_from_polygon, coverage(s, float(s.get("osm.coverage_buffer_m", 150))),
                                  tags=tags, log=log)
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
    # exits: where a road leaves the photographed area (beyond it we know nothing)
    from shapely.prepared import prep
    cov = coverage(s)
    cov_p = prep(cov)
    exits = [int(n) for n, d in G.nodes(data=True) if not cov_p.contains(Point(d["x"], d["y"]))]
    import json
    (s.derived / "exits.json").write_text(json.dumps(exits))
    cov_utm = prep(reproject_geom(cov, WGS84, utm))

    # buildings
    bld = _features(s, {"building": True}, "buildings", log)
    b_feats = []
    if len(bld):
        bld = bld[bld.geometry.type.isin(["Polygon", "MultiPolygon"])].to_crs(utm)
        bld = bld[[cov_utm.contains(g.centroid) for g in bld.geometry]]
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
    summary = {"area": s.area_id, "segments": len(seg_feats), "edges": len(edge_feats), "nodes": len(node_ids), "exits": len(exits),
               "buildings": len(b_feats), "facilities": len(f_feats), "osm": meta["status"]}
    log(f"prepared: {summary}")
    return summary


EXTRA_TAGS = {
    "amenity": ["parking", "school", "college", "community_centre", "place_of_worship", "university"],
    "leisure": ["pitch", "park", "sports_centre", "recreation_ground", "playground"],
    "landuse": ["recreation_ground", "farmland", "meadow", "orchard", "vineyard", "farmyard"],
    "shop": ["supermarket", "convenience", "grocery", "greengrocer", "general"],
}


def extra_features(s: Settings, log=print):
    """One Overpass query per area for relief candidates, farmland and food stores, cached as a snapshot."""
    import geopandas as gpd
    snap = snapshot_dir(s) / "extras.gpkg"
    if snap.exists():
        return gpd.read_file(snap)
    _osmnx_settings(s)
    poly = coverage(s, 1500)
    try:
        g = with_overpass_retry(ox.features_from_polygon, poly, tags=EXTRA_TAGS, log=log).reset_index()
    except Exception as e:  # noqa: BLE001
        if "No matching features" in str(e) or type(e).__name__ == "InsufficientResponseError":
            g = gpd.GeoDataFrame(columns=["element", "id", "geometry"], geometry="geometry", crs=WGS84)
        else:
            raise
    keep = [c for c in ("element", "id", "geometry", "name", "amenity", "leisure", "landuse", "shop", "surface") if c in g.columns]
    g = g[keep]
    for c in keep:
        if c != "geometry":
            g[c] = g[c].astype(str).where(g[c].notna(), None)
    snap.parent.mkdir(parents=True, exist_ok=True)
    g.to_file(snap, driver="GPKG")
    log(f"[{s.area_id}] OSM extras: {len(g)} features")
    return gpd.read_file(snap)
