"""Tier 4b: relief site recommender (plan section 8b, teammate design).

Candidates: OSM parking lots, school grounds, pitches, parks, community centres, places of
worship (>= 1,500 m2) inside the photographed area. The VLM checks before/after crops
(dry, mostly empty, no debris). Safety/access filters: relative elevation (DEM), road access,
truck reachability over the strict graph. Need: residential buildings within 1.5 km walking
distance over roads that are not flooded, weighted by tract vulnerability and by lack of a
reachable food store. Greedy maximum coverage picks the sites.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import networkx as nx

from floodlens.config import Settings
from floodlens.geo import WGS84, geom_of, now_iso, read_geojson, reproject_geom, utm_crs_for, write_geojson
from floodlens.osm import edge_id
from floodlens.polychips import PolyChipper
from floodlens.vlm.client import VLM

TAGS = {"amenity": ["parking", "school", "college", "community_centre", "place_of_worship", "university"],
        "leisure": ["pitch", "park", "sports_centre", "recreation_ground", "playground"],
        "landuse": ["recreation_ground"]}
FOOD_TAGS = {"shop": ["supermarket", "convenience", "grocery", "greengrocer", "general"]}
MIN_M2, MAX_M2, WALK_M, MAX_SITES, CAND_CAP = 1500, 200_000, 1500, 5, 120


def _kind(r) -> str:
    for k in ("amenity", "leisure", "landuse"):
        v = r.get(k)
        if isinstance(v, str) and v:
            return v
    return "open area"


def _candidates(s: Settings, log) -> list[dict]:
    from floodlens.osm import coverage, extra_features
    g = extra_features(s, log)
    cov = coverage(s)
    mask = False
    for k, vals in TAGS.items():
        if k in g.columns:
            mask = mask | g[k].isin(vals)
    g = g[mask & g.geometry.type.isin(["Polygon", "MultiPolygon"]) & g.intersects(cov)]
    utm = utm_crs_for(s.bbox)
    out = []
    for _, r in g.iterrows():
        a = reproject_geom(r.geometry, WGS84, utm).area
        if a < MIN_M2 or a > MAX_M2 or not cov.contains(r.geometry.representative_point()):
            continue   # too small, a whole parkland/reservoir, or centred outside the photos
        name = r.get("name") if isinstance(r.get("name"), str) else None
        out.append({"type": "Feature", "geometry": r.geometry.__geo_interface__,
                    "properties": {"id": f"osm_{r.get('element', 'way')}_{r.get('id')}", "kind": _kind(r),
                                   "name": name, "area_m2": round(a), "surface_tag": r.get("surface") if isinstance(r.get("surface"), str) else None}})
    out.sort(key=lambda f: -f["properties"]["area_m2"])
    log(f"[{s.area_id}] relief candidates >= {MIN_M2} m2: {len(out)} (checking {min(len(out), CAND_CAP)})")
    return out[:CAND_CAP]


def _food_nodes(s: Settings, net, log) -> list[int]:
    import numpy as np
    from floodlens.osm import extra_features, nearest_nodes
    g = extra_features(s, log)
    if "shop" not in g.columns:
        return []
    g = g[g["shop"].isin(FOOD_TAGS["shop"])]
    utm = net.utm
    pts = np.array([[p.x, p.y] for p in (reproject_geom(gm.representative_point(), WGS84, utm) for gm in g.geometry)])
    ids = list(net.G.nodes)
    xy = np.array([net.to_utm.transform(net.G.nodes[i]["x"], net.G.nodes[i]["y"]) for i in ids])
    return nearest_nodes(xy, ids, pts) if len(pts) else []


async def _check(s: Settings, cands: list[dict], log) -> dict[str, dict]:
    vlm = VLM(s)
    ch = PolyChipper(s, "sites", buffer_m=15, min_side_m=60, px=384)
    jobs = []
    try:
        for f in cands:
            files, tile, date = ch.make(f["properties"]["id"], geom_of(f))
            if files:
                jobs.append((f, files, tile, date))
    finally:
        ch.close()

    async def one(f, files, tile, date):
        if s.paired:
            lab, _ = await vlm.label([files["pre"].read_bytes(), files["post"].read_bytes()], "site_v1",
                                     intro=f"Image 1 is BEFORE ({s.event.get('before_label')}), image 2 is AFTER ({date}), the same place from the air.")
        else:
            lab, _ = await vlm.label([files["post"].read_bytes(), files["post"].read_bytes()], "site_v1",
                                     intro=f"Both images are the same aerial photo taken on {date}, days after a river flood.")
        pid = f["properties"]["id"]
        return pid, {"label": lab, "tile_id": tile, "chip": ch.url(pid), "chip_pre": ch.url(pid, "pre") if s.paired else None}

    res = await asyncio.gather(*(one(*j) for j in jobs))
    return dict(res)


def run(s: Settings, log=print) -> dict:
    from floodlens.analysis import Network
    from floodlens.sources import _relative_elevation, snap_dir
    import numpy as np
    from floodlens.osm import nearest_nodes
    net = Network(s)
    cands = _candidates(s, log)
    checks = asyncio.run(_check(s, cands, log)) if cands else {}
    # graphs: trucks over the strict graph, walking over everything not flooded
    Hs = net.undirected("strict")
    comp = {n: i for i, c in enumerate(nx.connected_components(Hs)) for n in c}
    exit_comps = {comp[n] for n in net.exit_nodes if n in comp}
    W = nx.Graph()
    for u, v, k, d in net.G.edges(keys=True, data=True):
        if net.estatus.get(edge_id(u, v, k)) != "blocked":
            L = float(d.get("length", 1))
            if not W.has_edge(u, v) or W[u][v]["w"] > L:
                W.add_edge(u, v, w=L)
    ids = list(net.G.nodes)
    xy = np.array([net.to_utm.transform(net.G.nodes[i]["x"], net.G.nodes[i]["y"]) for i in ids])
    dem = snap_dir(s) / "dem.tif"
    # need per node: weighted residents, extra weight where no food store is reachable on foot within 3 km
    food = set(_food_nodes(s, net, log))
    food_reach = set()
    for fn in food:
        if fn in W:
            food_reach |= set(nx.single_source_dijkstra_path_length(W, fn, cutoff=3000, weight="w"))
    need = defaultdict(float)
    for b in net.buildings:
        bp = net.bpop.get(b["properties"]["id"])
        if not bp:
            continue
        svi = float((bp["tract"] or {}).get("RPL_THEMES") or 0.5)
        n = b["properties"]["node_id"]
        need[n] += bp["est_pop"] * (0.5 + svi) * (1.0 if n in food_reach else 1.5)
    people = defaultdict(float)
    for b in net.buildings:
        bp = net.bpop.get(b["properties"]["id"])
        if bp:
            people[b["properties"]["node_id"]] += bp["est_pop"]
    from shapely.strtree import STRtree
    seg_geoms = [reproject_geom(geom_of(sf), WGS84, net.utm) for sf in net.segs]
    seg_tree = STRtree(seg_geoms)
    feats, cover = [], {}
    for f in cands:
        p = f["properties"]
        c = checks.get(p["id"])
        g = geom_of(f)
        g_utm = reproject_geom(g, WGS84, net.utm)
        k = int(seg_tree.nearest(g_utm))
        d_road = float(seg_geoms[k].distance(g_utm))     # site edge to the nearest road
        a_, b_, _ = net.segs[k]["properties"]["edge_id"].split("_")
        cu = g_utm.centroid
        cand_nodes = [int(a_), int(b_)]
        node = min(cand_nodes, key=lambda n: (xy[ids.index(n)][0] - cu.x) ** 2 + (xy[ids.index(n)][1] - cu.y) ** 2)
        lab = (c or {}).get("label") or {}
        rel = _relative_elevation(s, g.centroid.x, g.centroid.y, dem) if dem.exists() else None
        checks_list = [
            ("Photo check", "pass" if lab.get("usable") == "usable" else "fail" if lab.get("usable") == "not_usable" else "warn",
             lab.get("reason") or ("no photo" if not c else "")),
            ("Next to a road", "pass" if d_road <= 40 else "fail", f"{round(d_road)} m from the nearest road"),
            ("Truck access over confirmed-dry roads", "pass" if comp.get(node) in exit_comps else "warn" if node in W else "fail",
             "reachable from outside" if comp.get(node) in exit_comps else "only over roads the model could not confirm"),
            ("Not lower than surroundings", ("fail" if rel["relative_m"] < -0.5 else "pass") if rel else "warn",
             f"{rel['relative_m']:+.1f} m vs 300 m around" if rel else "no elevation data"),
        ]
        status = "rejected" if any(r[1] == "fail" for r in checks_list) else "uncertain" if any(r[1] == "warn" for r in checks_list) else "ok"
        reach = nx.single_source_dijkstra_path_length(W, node, cutoff=WALK_M, weight="w") if node in W else {node: 0}
        cover[p["id"]] = set(reach)
        surf = lab.get("surface") or ("paved" if p["kind"] == "parking" else "grass" if p["kind"] in ("pitch", "park", "recreation_ground") else "unknown")
        big = p["area_m2"] >= 5000
        use = ("Food and water point" if surf == "paved" and big else "Food distribution or water station" if surf == "paved"
               else "Tent shelter or mobile clinic" if surf in ("grass", "mixed") and big and p["kind"] in ("school", "college", "community_centre", "university", "place_of_worship", "pitch", "recreation_ground")
               else "Water station" if surf in ("grass", "dirt", "mixed") else "Check on site")
        p.update(status=status, checks=[{"name": a, "result": b_, "detail": c_} for a, b_, c_ in checks_list],
                 use=use, surface=surf, node_id=int(node), label=lab, chip=(c or {}).get("chip"), chip_pre=(c or {}).get("chip_pre"),
                 people_within_walk=round(sum(people[n] for n in reach)), need_within_walk=round(sum(need[n] for n in reach), 1))
        feats.append(f)
    # greedy maximum coverage over sites that passed (ok first, uncertain only if needed)
    chosen, covered = [], set()
    pool = [f for f in feats if f["properties"]["status"] == "ok"]
    for _ in range(MAX_SITES):
        best, gain = None, 0.0
        for f in pool:
            gnodes = cover[f["properties"]["id"]] - covered
            gsum = sum(need[n] for n in gnodes)
            if gsum > gain:
                best, gain = f, gsum
        if not best or gain <= 0:
            break
        new_nodes = cover[best["properties"]["id"]] - covered
        chosen.append(best)
        covered |= new_nodes
        best["properties"]["status"] = "recommended"
        best["properties"]["rank"] = len(chosen)
        best["properties"]["new_people_served"] = round(sum(people[n] for n in new_nodes))
        pool.remove(best)
    total_need = sum(need.values())
    summary = {"area": s.area_id, "generated": now_iso(), "candidates": len(feats),
               "status": {k: sum(f["properties"]["status"] == k for f in feats) for k in ("recommended", "ok", "uncertain", "rejected")},
               "people_covered": round(sum(people[n] for n in covered)), "need_covered_share": round(sum(need[n] for n in covered) / total_need, 3) if total_need else None,
               "food_stores_found": len(food), "walk_m": WALK_M}
    write_geojson(s.derived / "relief_sites.geojson", feats)
    (s.derived / "relief.json").write_text(json.dumps(summary, indent=1))
    log(f"[{s.area_id}] relief: {summary['status']}, people covered {summary['people_covered']}")
    return summary
