"""Tier 4 amendment: economic impact estimate (plan section 8a).

Homes: a fixed-seed random sample of residential buildings is checked by the VLM (how far
does new water reach the building?). Each class gets a damage ratio calibrated on real
NFIP claims for the same event (building damage / building value, by water depth). Values
come from the ACS 2016 5-year median home value of the building's census tract. Totals are
extrapolated to every residential building in the photographed area with a bootstrap 95%
interval. Farmland: OSM farmland/meadow/orchard polygons checked for floodwater or deposits.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import random
import statistics
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from floodlens.config import Settings
from floodlens.geo import WGS84, geom_of, now_iso, read_geojson, reproject_geom, utm_crs_for, write_geojson
from floodlens.polychips import PolyChipper
from floodlens.vlm.client import VLM

ACS_ZIP = "acs2016_tx_tracts.zip"
ACS_URL = "https://www2.census.gov/programs-surveys/acs/summary_file/2016/data/5_year_by_state/Texas_Tracts_Block_Groups_Only.zip"
NFIP_URL = "https://www.fema.gov/api/open/v3/NfipClaims"
# Fallback damage ratios if NFIP calibration is unavailable: rough USACE/Hazus-style one-story residential values.
FALLBACK_RATIO = {"none": 0.0, "around": 0.05, "at_walls": 0.25, "deep": 0.45}
AFTER_RATIO = {"intact": (0.0, 0.0, 0.0), "debris_around": (0.0, 0.05, 0.15), "water_around": (0.0, 0.1, 0.3),
               "damaged": (0.2, 0.4, 0.7), "destroyed": (0.8, 1.0, 1.0)}
MARKET_DISCOUNT = (0.031, 0.055)   # Freddie Mac 2020: floodplain discount in Harris County after Harvey
SAMPLE = 600
FIELD_CAP = 150


# ------------------------------------------------------------------ data: home values

def home_values(s: Settings, log=print) -> dict[str, float]:
    """tract GEOID -> ACS 2016 5-year median home value (B25077_001)."""
    out_p = s.snapshots / "global" / "acs2016_median_home_value_tx.json"
    if out_p.exists():
        return json.loads(out_p.read_text())
    zp = s.snapshots / "global" / ACS_ZIP
    if not zp.exists():
        from floodlens.sources import _http
        zp.write_bytes(_http(ACS_URL, timeout=1800))
    vals: dict[str, float] = {}
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        geo_name = next(n for n in names if n.lower().endswith("g20165tx.csv"))
        est_name = next(n for n in names if n.lower().endswith("e20165tx0106000.txt"))
        logrec = {}
        for row in csv.reader(io.TextIOWrapper(z.open(geo_name), encoding="latin-1")):
            if row[2] == "140":   # census tract summary level
                geoid = next((c.split("US", 1)[1] for c in row if c.startswith("14000US")), None)
                if geoid:
                    logrec[row[4]] = geoid
        for row in csv.reader(io.TextIOWrapper(z.open(est_name), encoding="latin-1")):
            g = logrec.get(row[5])
            if g and len(row) > 245 and row[245] not in ("", ".", "-"):
                try:
                    vals[g] = float(row[245])   # B25077_001 starts at position 246 of sequence 0106
                except ValueError:
                    pass
    out_p.write_text(json.dumps(vals))
    log(f"ACS 2016 median home value: {len(vals)} Texas tracts")
    return vals


# ------------------------------------------------------------------ data: NFIP claims

def nfip_claims(s: Settings, log=print) -> list[dict]:
    """NFIP claims for the event window in the counties of the area (snapshot per area)."""
    p = s.snapshots / s.area_id / "context" / "nfip_claims.json"
    if p.exists():
        return json.loads(p.read_text())["claims"]
    import pandas as pd
    from floodlens.sources import _http
    m = pd.read_csv(s.dataset_dir / "image_metadata.csv", dtype="string", keep_default_na=False)
    counties = sorted(set(m[m.locality.isin(s.area["localities"])].county_geoid) - {""})
    a, b = s.event["window"]
    claims = []
    fields = ("dateOfLoss,countyCode,censusGeoid,waterDepth,exteriorWaterDepth,interiorWaterDepth,buildingDamageAmount,"
              "buildingPropertyValue,amountPaidOnBuildingClaim,amountPaidOnContentsClaim,occupancyType")
    for cc in counties:
        skip = 0
        while True:
            q = {"$filter": f"countyCode eq '{cc}' and dateOfLoss ge '{a}' and dateOfLoss le '{b}'",
                 "$select": fields, "$top": "10000", "$skip": str(skip)}
            body = None
            for attempt in range(4):
                try:
                    body = json.loads(_http(NFIP_URL + "?" + urllib.parse.urlencode(q), timeout=240, binary=False))
                    break
                except Exception as e:  # noqa: BLE001  OpenFEMA returns 503 under load
                    log(f"[{s.area_id}] NFIP county {cc} page {skip // 10000}: {type(e).__name__}, retrying")
                    import time as _t
                    _t.sleep(15 * (attempt + 1))
            if body is None:
                raise RuntimeError(f"NFIP county {cc} failed after retries")
            rows = body.get("NfipClaims", [])
            claims += rows
            if len(rows) < 10000:
                break
            skip += 10000
        log(f"[{s.area_id}] NFIP claims county {cc}: {sum(1 for c in claims if c.get('countyCode') == cc)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched_at": now_iso(), "source": "OpenFEMA NfipClaims v3", "claims": claims}))
    return claims


def depth_band(inches) -> str | None:
    try:
        d = float(inches)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return "around" if d < 12 else "at_walls" if d <= 36 else "deep"


def calibrate(claims: list[dict]) -> dict:
    """Building damage ratio quartiles per exterior-depth band from real claims of this event.
    Claims exist only where damage happened, so these are conditional on damage; the class
    ranges below therefore start at zero for 'partial'."""
    by: dict[str, list[float]] = defaultdict(list)
    for c in claims:
        ext = c.get("exteriorWaterDepth")
        band = depth_band(ext if ext not in (None, 0) else c.get("waterDepth"))
        dmg, val = c.get("buildingDamageAmount"), c.get("buildingPropertyValue")
        if band and dmg and val and val > 0:
            by[band].append(min(1.0, max(0.0, dmg / val)))
    q = {}
    for band in ("around", "at_walls", "deep"):
        v = sorted(by.get(band, []))
        if len(v) >= 20:
            q[band] = {"q25": round(v[len(v) // 4], 3), "median": round(statistics.median(v), 3), "q75": round(v[3 * len(v) // 4], 3), "n": len(v)}
    calibrated = all(b in q for b in ("around", "at_walls", "deep"))
    if not calibrated:
        q = {b: {"q25": FALLBACK_RATIO[b] * 0.6, "median": FALLBACK_RATIO[b], "q75": min(1, FALLBACK_RATIO[b] * 1.5), "n": 0}
             for b in ("around", "at_walls", "deep")}
    upper = sorted(by.get("at_walls", []) + by.get("deep", []))
    ranges = {
        "none": (0.0, 0.0, 0.0),
        "partial": (0.0, q["around"]["q25"], q["around"]["median"]),
        "surrounded": (q["at_walls"]["q25"], round(statistics.median(upper), 3) if upper else q["at_walls"]["median"], q["deep"]["q75"]),
    }
    return {"bands": q, "class_ranges": {k: {"low": a, "mid": b, "high": c} for k, (a, b, c) in ranges.items()},
            "source": "NFIP claims for this event (building damage / building value)" if calibrated else "fallback table (too few claims)"}


def classify(label: dict, paired: bool) -> str:
    if not paired:
        return label.get("condition") or "unknown"
    if label.get("view") in ("trees", "shadow", "unclear") and label.get("water_percent_after", 0) < 50:
        return "hidden"
    new = (label.get("water_percent_after") or 0) - (label.get("water_percent_before") or 0)
    return "surrounded" if new >= 50 else "partial" if new >= 15 else "none"


# ------------------------------------------------------------------ VLM checks

async def _check_buildings(s: Settings, sample: list[dict], log) -> list[dict]:
    vlm = VLM(s)
    ch = PolyChipper(s, "buildings", buffer_m=15, min_side_m=40, px=320)
    try:
        jobs = []
        for b in sample:
            files, tile, date = ch.make(b["properties"]["id"].replace("/", "_").replace(":", "_"), geom_of(b))
            if files:
                jobs.append((b, files, tile, date))
    finally:
        ch.close()

    async def one(b, files, tile, date):
        if s.paired:
            lab, _ = await vlm.label([files["pre"].read_bytes(), files["post"].read_bytes()], "building_v2",
                                     before_date=s.event.get("before_label"), after_date=date)
        else:
            lab, _ = await vlm.label([files["post"].read_bytes()], "building_after_v1", after_date=date)
        cid = b["properties"]["id"].replace("/", "_").replace(":", "_")
        return {"id": b["properties"]["id"], "label": lab, "tile_id": tile, "chip": ch.url(cid),
                "chip_pre": ch.url(cid, "pre") if s.paired else None}

    out = await asyncio.gather(*(one(*j) for j in jobs))
    log(f"[{s.area_id}] buildings checked: {len(out)}")
    return list(out)


async def _check_fields(s: Settings, fields: list[dict], log) -> list[dict]:
    vlm = VLM(s)
    ch = PolyChipper(s, "fields", buffer_m=10, min_side_m=60, px=384)
    jobs = []
    try:
        for f in fields:
            files, tile, date = ch.make(f["properties"]["id"], geom_of(f))
            if files:
                jobs.append((f, files, date))
    finally:
        ch.close()

    async def one(f, files, date):
        if s.paired:
            imgs = [files["pre"].read_bytes(), files["post"].read_bytes()]
            lab, _ = await vlm.label(imgs, "field_v1", intro=f"Image 1 is BEFORE ({s.event.get('before_label')}), image 2 is AFTER ({date}), the same place from the air.",
                                     compare=" in image 2 that is not there in image 1")
        else:
            lab, _ = await vlm.label([files["post"].read_bytes(), files["post"].read_bytes()], "field_v1",
                                     intro=f"Both images are the same aerial photo taken on {date}, days after a river flood.", compare="")
        return {"id": f["properties"]["id"], "label": lab, "acres": f["properties"]["acres"], "landuse": f["properties"]["landuse"],
                "chip": ch.url(f["properties"]["id"])}

    out = await asyncio.gather(*(one(*j) for j in jobs))
    log(f"[{s.area_id}] fields checked: {len(out)}")
    return list(out)


def _farmland(s: Settings, log) -> list[dict]:
    from floodlens.osm import coverage, extra_features
    g = extra_features(s, log)
    if "landuse" not in g.columns:
        return []
    g = g[g["landuse"].isin(["farmland", "meadow", "orchard", "vineyard", "farmyard"])]
    g = g[g.geometry.type.isin(["Polygon", "MultiPolygon"])]
    utm = utm_crs_for(s.bbox)
    cov = coverage(s)
    out = []
    for _, r in g.iterrows():
        geom = r.geometry.intersection(cov)
        if geom.is_empty:
            continue
        acres = reproject_geom(geom, WGS84, utm).area / 4046.86
        if acres < 0.5:
            continue
        out.append({"type": "Feature", "geometry": geom.__geo_interface__,
                    "properties": {"id": f"osm_{r.get('element', 'way')}_{r.get('id')}", "landuse": r.get("landuse"), "acres": round(acres, 2)}})
    out.sort(key=lambda f: -f["properties"]["acres"])
    return out[:FIELD_CAP]


# ------------------------------------------------------------------ estimate

TRACTS_2016 = "https://www2.census.gov/geo/tiger/TIGER2016/TRACT/tl_2016_48_tract.zip"


def _tract_of(s: Settings, buildings: list[dict]) -> dict[str, str]:
    """Building id -> 2016-vintage tract GEOID (matches ACS 2016 values and 2017 NFIP claims)."""
    import geopandas as gpd
    from shapely.strtree import STRtree
    from floodlens.osm import coverage
    from floodlens.sources import _download_once
    zp = _download_once(s, TRACTS_2016, "tl_2016_48_tract.zip")
    tr = gpd.read_file(f"zip://{zp}")[["GEOID", "geometry"]].to_crs(WGS84)
    tr = tr[tr.intersects(coverage(s, 500))]
    geoms, ids = list(tr.geometry), list(tr.GEOID)
    tree = STRtree(geoms)
    out = {}
    for b in buildings:
        c = geom_of(b).centroid
        for i in tree.query(c):
            if geoms[int(i)].contains(c):
                out[b["properties"]["id"]] = ids[int(i)]
                break
    return out


def run(s: Settings, n: int = SAMPLE, log=print) -> dict:
    bl = [b for b in read_geojson(s.derived / "buildings.geojson") if b["properties"]["is_residential"]]
    rnd = random.Random(13)
    sample = rnd.sample(bl, min(n, len(bl)))
    checked = asyncio.run(_check_buildings(s, sample, log))
    values = home_values(s, log)
    tract_of = _tract_of(s, bl)
    try:
        claims = nfip_claims(s, log)
    except Exception as e:  # noqa: BLE001
        log(f"[{s.area_id}] NFIP claims unavailable: {e}")
        claims = []
    if s.paired:
        calib = calibrate(claims)
    else:
        calib = {"class_ranges": {k: {"low": a, "mid": b, "high": c} for k, (a, b, c) in AFTER_RATIO.items()},
                 "source": "assumed ratios for visible structural condition (no before photo, no depth)"}
    ranges = calib["class_ranges"]
    if s.paired:
        # hidden home on a road the road check marked underwater: second, independent signal
        ranges["hidden_on_flooded_road"] = {"low": 0.0, "mid": ranges["partial"]["high"], "high": ranges["surrounded"]["mid"]}
    wet_nodes = set()
    for e in read_geojson(s.derived / "road_edges.geojson"):
        if e["properties"].get("status") == "blocked":
            a, b, _ = e["properties"]["id"].split("_")
            wet_nodes |= {int(a), int(b)}
    node_of = {b["properties"]["id"]: b["properties"].get("node_id") for b in bl}
    area_vals = [values[t] for t in set(tract_of.values()) if t in values]
    fallback_val = statistics.median(area_vals) if area_vals else None
    rows = []
    for c in checked:
        cls = classify(c["label"], s.paired)
        if cls == "hidden" and node_of.get(c["id"]) in wet_nodes:
            cls = "hidden_on_flooded_road"
        val = values.get(tract_of.get(c["id"], ""), fallback_val)
        rg = ranges.get(cls)
        dm = {k: (round(val * rg[k]) if (val and rg) else None) for k in ("low", "mid", "high")}
        rows.append({**c, "class": cls, "value_usd": val, "damage_usd": dm["mid"], "damage_low": dm["low"], "damage_high": dm["high"]})
    known = [r for r in rows if r["damage_usd"] is not None]
    N = len(bl)
    classes = Counter(r["class"] for r in rows)

    def boot(vals, reps=1000):
        if not vals:
            return (None, None, None)
        means = sorted(statistics.fmean(rnd.choices(vals, k=len(vals))) for _ in range(reps))
        return (statistics.fmean(vals), means[int(0.025 * reps)], means[int(0.975 * reps)])

    mean, _, _ = boot([r["damage_usd"] for r in known])
    _, lo, _ = boot([r["damage_low"] for r in known])
    _, _, hi = boot([r["damage_high"] for r in known])
    hit = [r for r in rows if r["class"] in ("surrounded", "damaged", "destroyed")]
    likely = [r for r in rows if r["class"] == "hidden_on_flooded_road"]
    visible = [r for r in rows if r["class"] not in ("hidden", "unknown", "hidden_on_flooded_road")]
    share = len(hit) / len(visible) if visible else 0
    affected_value = sum(r["value_usd"] or 0 for r in hit) / len(visible) * N if visible else 0
    # validation against insured claims, per tract
    tract_claims = defaultdict(float)
    for c in claims:
        if c.get("censusGeoid"):
            tract_claims[str(c["censusGeoid"])[:11]] += (c.get("buildingDamageAmount") or 0)
    res_per_tract = Counter(tract_of.get(b["properties"]["id"]) for b in bl)
    by_tract = defaultdict(list)
    for r in known:
        t = tract_of.get(r["id"])
        if t:
            by_tract[t].append(r["damage_usd"])
    pairs = [(statistics.fmean(v) * res_per_tract[t], tract_claims.get(t, 0.0)) for t, v in by_tract.items() if len(v) >= 3]
    rho = _spearman(pairs) if (claims and len(pairs) >= 4) else None
    # farmland
    fields = _farmland(s, log)
    fchecked = asyncio.run(_check_fields(s, fields, log)) if fields else []
    fl_acres = sum(f["acres"] for f in fchecked if f["label"].get("answer") == "yes")
    dep_acres = sum(f["acres"] for f in fchecked if f["label"].get("deposit") in ("sand_or_mud", "debris"))
    result = {
        "area": s.area_id, "generated": now_iso(), "model": s.vlm_model,
        "homes": {"residential_buildings": N, "sampled": len(rows), "classes": dict(classes),
                  "visible_in_sample": len(visible),
                  "hidden_on_flooded_road_in_sample": len(likely),
                  "est_homes_hidden_on_flooded_road": round(len(likely) / len(rows) * N) if rows else 0,
                  "share_surrounded_or_worse": round(share, 3),
                  "est_homes_surrounded_or_worse": round(share * N),
                  "est_building_damage_usd": {"mid": round(mean * N) if mean is not None else None,
                                              "low": round(lo * N) if lo is not None else None,
                                              "high": round(hi * N) if hi is not None else None},
                  "affected_home_value_usd": round(affected_value),
                  "market_discount_usd": {"low": round(affected_value * MARKET_DISCOUNT[0]), "high": round(affected_value * MARKET_DISCOUNT[1])},
                  "home_value_source": "ACS 2016 5-year median home value by tract (B25077)",
                  "damage_ratios": calib},
        "insured_claims": {"n_claims": len(claims), "building_damage_usd": round(sum(c.get("buildingDamageAmount") or 0 for c in claims)),
                           "paid_usd": round(sum((c.get("amountPaidOnBuildingClaim") or 0) + (c.get("amountPaidOnContentsClaim") or 0) for c in claims)),
                           "scope": "all NFIP claims in the area's counties during the event window (wider than the photographed area)",
                           "tract_spearman_vs_estimate": rho, "tracts_compared": len(pairs)},
        "method": {"sample": "fixed-seed random sample of residential buildings; hidden ones (trees/shadow) are left out and the rest is scaled to all homes",
                   "classes": {"surrounded": "new water covers half or more of the crop around the home",
                               "partial": "new water covers 15-50% of the crop", "none": "less than 15%", "hidden": "ground not visible",
                               "hidden_on_flooded_road": "ground hidden by trees, but its road was judged underwater (counted with a wider range)"}},
        "farmland": {"fields_checked": len(fchecked), "acres_checked": round(sum(f["acres"] for f in fchecked), 1),
                     "acres_flooded": round(fl_acres, 1), "acres_with_deposit_or_debris": round(dep_acres, 1)},
    }
    s.derived.mkdir(parents=True, exist_ok=True)
    (s.derived / "impact.json").write_text(json.dumps(result, indent=1))
    by_id = {r["id"]: r for r in rows}
    feats = []
    for b in sample:
        r = by_id.get(b["properties"]["id"])
        if r:
            feats.append({"type": "Feature", "geometry": b["geometry"], "properties": {
                "id": r["id"], "class": r["class"], "value_usd": r["value_usd"], "damage_usd": r["damage_usd"],
                "damage_low": r["damage_low"], "damage_high": r["damage_high"],
                "water_after": r["label"].get("water_percent_after"), "water_before": r["label"].get("water_percent_before"),
                "reason": r["label"].get("reason"), "chip": r["chip"], "chip_pre": r["chip_pre"]}})
    write_geojson(s.derived / "impact_buildings.geojson", feats)
    write_geojson(s.derived / "impact_fields.geojson", [
        {"type": "Feature", "geometry": next(f["geometry"] for f in fields if f["properties"]["id"] == c["id"]),
         "properties": {**{k: c[k] for k in ("id", "acres", "landuse", "chip")}, **c["label"]}} for c in fchecked])
    log(f"[{s.area_id}] impact: {json.dumps(result['homes']['est_building_damage_usd'])}, farmland flooded {fl_acres:.0f} ac")
    return result


def _spearman(pairs):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    d2 = sum((x - y) ** 2 for x, y in zip(a, b))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 3)
