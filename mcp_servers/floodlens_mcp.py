"""FloodLens MCP server (plan Tier 3, section 7.3).

Exposes the FloodLens data and the public-data layer as MCP tools, so any MCP client (an
LLM agent, Claude Desktop, another team's app) can ask which roads are underwater, who is
cut off and how to get out. Every context tool returns the same envelope as the web app
({data, meta: {source, status, fetched_at, age_s, error}}) and falls back to snapshots.

Run:  python mcp_servers/floodlens_mcp.py                 (stdio)
      python mcp_servers/floodlens_mcp.py --http 9101     (streamable HTTP on 127.0.0.1:9101/mcp)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402  (mcp >= 2.0)

from floodlens.config import get_settings  # noqa: E402
from floodlens.geo import read_geojson  # noqa: E402

BASE = get_settings()
server = MCPServer(name="floodlens", title="FloodLens flood road triage",
                   instructions="Tools return flood road status, cut-off neighbourhoods and safe routes judged from "
                                "before/after aerial photos. Context tools carry a status (live, cached, snapshot, unavailable).")


def _area(area: str):
    if area not in BASE.area_ids:
        raise ValueError(f"unknown area {area}; choose from {BASE.area_ids}")
    return BASE.for_area(area)


@server.tool(description="List the areas FloodLens has processed, with headline numbers.")
def list_areas() -> list[dict]:
    out = []
    for a in BASE.area_ids:
        s = BASE.for_area(a)
        p = s.derived / "summary.json"
        out.append({"id": a, "name": s.area["name"], "event": s.event["name"],
                    "summary": json.loads(p.read_text()) if p.exists() else None})
    return out


@server.tool(description="Cut-off neighbourhoods in an area, highest need first. mode: permissive (best estimate) or strict (confirmed dry only).")
def cutoff_zones(area: str, mode: str = "permissive", top: int = 10) -> list[dict]:
    s = _area(area)
    zs = read_geojson(s.derived / f"cutoff_zones_{mode}.geojson")[:top]
    keep = ("id", "label", "est_pop", "building_count", "priority_rank", "people_rank", "vulnerability",
            "care_facilities", "nearest_blocked_segment_ids", "label_point")
    return [{k: z["properties"].get(k) for k in keep} for z in zs]


@server.tool(description="Road pieces near a point with their status (open, blocked, uncertain) and the model's reason.")
def roads_near(area: str, lat: float, lon: float, radius_m: float = 150) -> list[dict]:
    from shapely.geometry import Point
    from floodlens.geo import WGS84, geom_of, reproject_geom, utm_crs_for
    s = _area(area)
    utm = utm_crs_for(s.bbox)
    p = reproject_geom(Point(lon, lat), WGS84, utm)
    out = []
    for f in read_geojson(s.derived / "road_segments.geojson"):
        d = reproject_geom(geom_of(f), WGS84, utm).distance(p)
        if d <= radius_m:
            q = f["properties"]
            out.append({"id": q["id"], "name": q.get("name"), "status": q["status"], "distance_m": round(d),
                        "reason": (q.get("label") or {}).get("reason"), "decided_by": q.get("status_source")})
    return sorted(out, key=lambda r: r["distance_m"])[:25]


@server.tool(description="Safest route from a point to a shelter, hospital or the edge of the photographed area, avoiding flooded roads.")
def safe_route(area: str, lat: float, lon: float, mode: str = "permissive") -> dict:
    from floodlens.analysis import Network
    r = Network(_area(area)).route(lat, lon, mode)
    if r.get("route"):
        r["route"] = {"type": "LineString", "n_points": len(r["route"]["geometry"]["coordinates"])}
    if r.get("naive"):
        r["naive"] = {"length_m": r["naive"]["properties"]["length_m"], "flooded_roads_crossed": len(r["naive"]["properties"]["blocked_edge_ids"])}
    return r


@server.tool(description="Context for a road piece: NWS warnings active that day, tract vulnerability (SVI), relative elevation. Each item has a status.")
def road_context(area: str, segment_id: str) -> dict:
    from floodlens import sources
    s = _area(area)
    f = next((f for f in read_geojson(s.derived / "road_segments.geojson") if f["properties"]["id"] == segment_id), None)
    if f is None:
        raise ValueError("unknown segment")
    out = sources.context_for_segment(s, f)
    for v in out.values():
        if v["meta"].get("status") == "snapshot" and isinstance(v.get("data"), Path):
            v["data"] = str(v["data"])
    return out


@server.tool(description="Live NWS alerts at the area centre (live -> cached -> snapshot -> unavailable).")
def live_alerts(area: str) -> dict:
    from floodlens import sources
    return sources.nws_live(_area(area))


@server.tool(description="Health of every data source for an area.")
def health(area: str) -> list[dict]:
    from floodlens import sources
    return sources.status(_area(area))


@server.tool(description="Fault injection for demos: source in svi|nws_history|elevation|nws_live|all, mode in none|down|slow|corrupt.")
def set_fault(source: str, mode: str) -> dict:
    from floodlens import sources
    return sources.set_fault(source, mode)


@server.tool(description="Economic impact estimate for an area (homes, insured claims check, farmland).")
def impact(area: str) -> dict | None:
    p = _area(area).derived / "impact.json"
    return json.loads(p.read_text()) if p.exists() else None


@server.tool(description="Recommended relief sites for an area with their checks and people served.")
def relief_sites(area: str, status: str = "recommended") -> list[dict]:
    s = _area(area)
    keep = ("id", "name", "kind", "status", "use", "area_m2", "people_within_walk", "new_people_served", "checks", "rank")
    return [{k: f["properties"].get(k) for k in keep} for f in read_geojson(s.derived / "relief_sites.geojson")
            if status == "all" or f["properties"]["status"] == status]


if __name__ == "__main__":
    if "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http") + 1])
        server.run("streamable-http", host="127.0.0.1", port=port)
    else:
        server.run("stdio")
