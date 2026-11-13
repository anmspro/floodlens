"""Network analysis: edge status, cut-off zones, facility reachability, safe routes."""
from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx
import osmnx as ox
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from floodlens.config import Settings
from floodlens.geo import WGS84, feature, geom_of, read_geojson, reproject_geom, utm_crs_for, write_geojson, transformer
from floodlens.osm import edge_id

UNSURE = {"uncertain", "pending", "no_imagery"}


def edge_statuses(segs: list[dict]) -> dict[str, str]:
    by_edge: dict[str, list[str]] = defaultdict(list)
    for f in segs:
        by_edge[f["properties"]["edge_id"]].append(f["properties"]["status"])
    out = {}
    for eid, sts in by_edge.items():
        if "blocked" in sts:
            out[eid] = "blocked"
        elif any(x in UNSURE for x in sts):
            out[eid] = "uncertain"
        else:
            out[eid] = "open"
    return out


class Network:
    """Loads graph + current labels once; recompute() after any label change."""

    def __init__(self, s: Settings):
        self.s = s
        self.G = ox.load_graphml(s.derived / "graph.graphml")
        self.utm = utm_crs_for(s.bbox)
        self.to_utm = transformer(WGS84, self.utm)
        self.unverified: dict[str, dict] = {}
        self.recompute_inputs()

    def recompute_inputs(self) -> None:
        s = self.s
        self.segs = read_geojson(s.derived / "road_segments.geojson")
        self.estatus = edge_statuses(self.segs)
        self.buildings = read_geojson(s.derived / "buildings.geojson")
        self.facilities = read_geojson(s.derived / "facilities.geojson")
        self.exit_nodes = self._exit_nodes()

    def _exit_nodes(self) -> set[int]:
        w, so, e, n = self.s.bbox
        buf = float(self.s.get("routing.exit_buffer_m", 50))
        x0, y0 = self.to_utm.transform(w, so)
        x1, y1 = self.to_utm.transform(e, n)
        out = set()
        for nid, d in self.G.nodes(data=True):
            x, y = self.to_utm.transform(d["x"], d["y"])
            if min(x - x0, x1 - x, y - y0, y1 - y) <= buf:
                out.add(nid)
        return out

    def target_nodes(self, kinds=("hospital", "shelter", "exit")) -> dict[int, dict]:
        t: dict[int, dict] = {}
        if "exit" in kinds:
            for nid in self.exit_nodes:
                t[nid] = {"kind": "exit", "name": "edge of mapped area"}
        for f in self.facilities:
            p = f["properties"]
            if p["kind"] in kinds:
                t[p["node_id"]] = {"kind": p["kind"], "name": p["name"], "id": p["id"]}
        return t

    def allowed(self, eid: str, mode: str) -> bool:
        st = self.estatus.get(eid, "uncertain")
        if st == "blocked":
            return False
        if mode == "strict":
            return st == "open"
        return True

    def undirected(self, mode: str) -> nx.Graph:
        H = nx.Graph()
        H.add_nodes_from(self.G.nodes)
        for u, v, k in self.G.edges(keys=True):
            if self.allowed(edge_id(u, v, k), mode):
                H.add_edge(u, v)
        return H

    def directed(self, mode: str) -> nx.DiGraph:
        pen = float(self.s.get("routing.uncertain_penalty", 4.0))
        D = nx.DiGraph()
        for u, v, k, d in self.G.edges(keys=True, data=True):
            eid = edge_id(u, v, k)
            if not self.allowed(eid, mode):
                continue
            w = float(d.get("length", 1.0)) * (1.0 if self.estatus.get(eid) == "open" else pen)
            if not D.has_edge(u, v) or D[u][v]["w"] > w:
                D.add_edge(u, v, w=w, key=k, eid=eid)
        return D

    # ---------- cut-off zones ----------
    def cutoff(self, mode: str) -> list[dict]:
        H = self.undirected(mode)
        targets = set(self.target_nodes())
        b_by_node: dict[int, list[dict]] = defaultdict(list)
        for b in self.buildings:
            b_by_node[b["properties"]["node_id"]].append(b)
        names_by_node: dict[int, Counter] = defaultdict(Counter)
        blocked_by_node: dict[int, list[dict]] = defaultdict(list)
        seg_by_edge: dict[str, list[dict]] = defaultdict(list)
        for f in self.segs:
            seg_by_edge[f["properties"]["edge_id"]].append(f)
        for u, v, k, d in self.G.edges(keys=True, data=True):
            nm = d.get("name")
            nm = nm[0] if isinstance(nm, list) else nm
            for n in (u, v):
                if nm:
                    names_by_node[n][nm] += 1
            for f in seg_by_edge.get(edge_id(u, v, k), []):
                if f["properties"]["status"] == "blocked":
                    blocked_by_node[u].append(f)
                    blocked_by_node[v].append(f)
        ppb = float(self.s.get("population.persons_per_building", 2.5))
        min_b = int(self.s.get("cutoff.min_buildings", 3))
        zones, unverified = [], []
        for comp in nx.connected_components(H):
            if comp & targets:
                continue
            blds = [b for n in comp for b in b_by_node.get(n, [])]
            if len(blds) < min_b:
                continue
            polys = [reproject_geom(geom_of(b), WGS84, self.utm).buffer(35) for b in blds]
            poly = reproject_geom(unary_union(polys).buffer(-20).simplify(4), self.utm, WGS84)
            names = sum((names_by_node[n] for n in comp), Counter())
            if names:
                label = f"near {names.most_common(1)[0][0]}"
            else:
                c = poly.representative_point()
                label = f"zone near {c.y:.4f}, {c.x:.4f}"
            blocked = {f["properties"]["id"]: f for n in comp for f in blocked_by_node.get(n, [])}
            if not blocked:
                # Walled in only by roads we have no imagery for: unknown, not "cut off by flooding".
                unverified.append(len(blds))
                continue
            nres = sum(1 for b in blds if b["properties"]["is_residential"])
            zones.append({"geom": poly, "props": {
                "label": label, "building_count": len(blds), "est_pop": round(nres * ppb, 1),
                "vulnerability": None, "mode": mode,
                "nearest_blocked_segment_ids": [k for k, _ in sorted(blocked.items(), key=lambda kv: kv[1]["properties"]["length_m"])],
                "node_ids": sorted(int(n) for n in comp), "building_ids": [b["properties"]["id"] for b in blds],
                "label_point": [round(poly.representative_point().x, 6), round(poly.representative_point().y, 6)],
            }})
        zones.sort(key=lambda z: -z["props"]["est_pop"])
        self.unverified[mode] = {"zones": len(unverified), "buildings": sum(unverified)}
        out = []
        for i, z in enumerate(zones, 1):
            p = {"id": f"Z{i}", "priority_score": z["props"]["est_pop"], "priority_rank": i, **z["props"]}
            out.append(feature(z["geom"], p))
        return out

    def run(self) -> dict:
        s = self.s
        self.recompute_inputs()
        zones = {m: self.cutoff(m) for m in ("strict", "permissive")}
        for m, z in zones.items():
            write_geojson(s.derived / f"cutoff_zones_{m}.geojson", z)
        # facility reachability (strict)
        H = self.undirected("strict")
        comp_of = {}
        for i, comp in enumerate(nx.connected_components(H)):
            for n in comp:
                comp_of[n] = i
        exit_comps = {comp_of[n] for n in self.exit_nodes if n in comp_of}
        safe_comps = exit_comps | {comp_of[f["properties"]["node_id"]] for f in self.facilities
                                   if f["properties"]["kind"] in ("hospital", "shelter")}
        for f in self.facilities:
            p = f["properties"]
            c = comp_of.get(p["node_id"])
            p["reachable_from_outside"] = c in exit_comps
            p["reachable"] = c in exit_comps if p["kind"] in ("hospital", "shelter") else c in safe_comps
        write_geojson(s.derived / "facilities.geojson", self.facilities)
        zone_of = {bid: z["properties"]["id"] for z in zones["strict"] for bid in z["properties"]["building_ids"]}
        for b in self.buildings:
            b["properties"]["zone_id"] = zone_of.get(b["properties"]["id"])
        write_geojson(s.derived / "buildings.geojson", self.buildings)
        edges = read_geojson(s.derived / "road_edges.geojson")
        for e in edges:
            e["properties"]["status"] = self.estatus.get(e["properties"]["id"], "uncertain")
        write_geojson(s.derived / "road_edges.geojson", edges)
        counts = Counter(f["properties"]["status"] for f in self.segs)
        summary = {"segments": dict(counts), "zones_strict": len(zones["strict"]), "zones_permissive": len(zones["permissive"]),
                   "unverified": self.unverified,
                   "people_cut_off": {m: round(sum(z["properties"]["est_pop"] for z in zones[m])) for m in zones},
                   "facilities_cut_off": [f["properties"]["name"] for f in self.facilities if f["properties"]["reachable"] is False]}
        (s.derived / "summary.json").write_text(__import__("json").dumps(summary))
        return summary

    # ---------- routing ----------
    def route(self, lat: float, lon: float, mode: str = "permissive",
              target_kinds=("shelter", "hospital", "exit")) -> dict:
        D = self.directed(mode)
        targets = {n: t for n, t in self.target_nodes(target_kinds).items() if n in D}
        if not D.number_of_nodes() or not targets:
            return {"route": None, "reason": "no reachable target"}
        x, y = self.to_utm.transform(lon, lat)
        best, bd = None, float("inf")
        for n in D.nodes:
            nx_, ny_ = self.to_utm.transform(self.G.nodes[n]["x"], self.G.nodes[n]["y"])
            d = (nx_ - x) ** 2 + (ny_ - y) ** 2
            if d < bd:
                best, bd = n, d
        try:
            dist, path = nx.multi_source_dijkstra(D.reverse(copy=False), set(targets), target=best, weight="w")
        except nx.NetworkXNoPath:
            zone = next((z["properties"]["id"] for z in read_geojson(self.s.derived / f"cutoff_zones_{mode}.geojson")
                         if best in z["properties"]["node_ids"]), None)
            return {"route": None, "reason": "no reachable target", "zone_id": zone}
        path = list(reversed(path))
        coords, steps, uncertain, length = [], [], [], 0.0
        for u, v in zip(path[:-1], path[1:]):
            ed = D[u][v]
            data = self.G.get_edge_data(u, v)[ed["key"]]
            geom = data.get("geometry") or LineString([(self.G.nodes[u]["x"], self.G.nodes[u]["y"]),
                                                      (self.G.nodes[v]["x"], self.G.nodes[v]["y"])])
            pts = list(geom.coords)
            if Point(pts[0]).distance(Point(self.G.nodes[u]["x"], self.G.nodes[u]["y"])) > \
               Point(pts[-1]).distance(Point(self.G.nodes[u]["x"], self.G.nodes[u]["y"])):
                pts.reverse()
            coords.extend(pts if not coords else pts[1:])
            L = float(data.get("length", 0))
            length += L
            nm = data.get("name")
            nm = (nm[0] if isinstance(nm, list) else nm) or "unnamed road"
            if steps and steps[-1]["name"] == nm:
                steps[-1]["m"] += L
            else:
                steps.append({"name": nm, "m": L})
            if self.estatus.get(ed["eid"]) != "open":
                uncertain.append(ed["eid"])
        tgt = targets[path[-1]]
        naive = self._naive(best, path[-1])
        text = [f"{'Start on' if i == 0 else 'Continue on'} {st['name']} for {round(st['m'])} m" for i, st in enumerate(steps)]
        text.append(f"Arrive: {tgt['name']} ({tgt['kind']})")
        return {"route": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": {}},
                "target": tgt, "length_m": round(length), "uncertain_edge_ids": uncertain, "steps": text, "mode": mode, "naive": naive}

    def _naive(self, start: int, target: int) -> dict | None:
        """Shortest route to the same destination ignoring the flood: what a normal map app would suggest."""
        try:
            path = nx.shortest_path(self.G, start, target, weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        coords, blocked, length = [], [], 0.0
        for u, v in zip(path[:-1], path[1:]):
            k, data = min(self.G.get_edge_data(u, v).items(), key=lambda kv: kv[1].get("length", 0))
            geom = data.get("geometry") or LineString([(self.G.nodes[u]["x"], self.G.nodes[u]["y"]),
                                                      (self.G.nodes[v]["x"], self.G.nodes[v]["y"])])
            pts = list(geom.coords)
            if Point(pts[0]).distance(Point(self.G.nodes[u]["x"], self.G.nodes[u]["y"])) > \
               Point(pts[-1]).distance(Point(self.G.nodes[u]["x"], self.G.nodes[u]["y"])):
                pts.reverse()
            coords.extend(pts if not coords else pts[1:])
            length += float(data.get("length", 0))
            eid = edge_id(u, v, k)
            if self.estatus.get(eid) == "blocked":
                blocked.append(eid)
        return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"length_m": round(length), "blocked_edge_ids": blocked}}
