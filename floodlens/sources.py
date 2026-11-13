"""Public context data with a status envelope and snapshot fallback (plan 3.6, 7.1-7.2, 7a).

Every getter returns {"data": ..., "meta": {source, status, fetched_at, age_s, error}}.
status is one of live | cached | snapshot | unavailable. Live sources try the network
(5 s timeout), then a short-lived cache, then the snapshot file on disk. Fault injection
(`set_fault`) makes a source fail on purpose so the demo can show the fallback working.

Sources
- svi          CDC/ATSDR SVI 2022 (Texas tracts) joined to Census TIGER 2022 tract shapes
- nws_history  NWS warnings issued during the event window (Iowa Environmental Mesonet archive)
- elevation    USGS 3DEP elevation for the area (GeoTIFF), relative road elevation
- nws_live     api.weather.gov active alerts at the area centre (live demo of the envelope)
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from floodlens.config import Settings, get_settings
from floodlens.geo import WGS84, geom_of, now_iso, reproject_geom, utm_crs_for

UA = {"User-Agent": "floodlens-hackathon (contact via project repo)"}
SVI_URL = "https://svi.cdc.gov/Documents/Data/2022/csv/states/Texas.csv"
TRACTS_URL = "https://www2.census.gov/geo/tiger/TIGER2022/TRACT/tl_2022_48_tract.zip"
IEM_URL = ("https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py?year1={y1}&month1={m1}&day1={d1}"
           "&hour1=0&minute1=0&year2={y2}&month2={m2}&day2={d2}&hour2=0&minute2=0&{wfos}&limitps=yes&phenomena={ph}&significance=W")
DEM_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
           "?bbox={w},{s},{e},{n}&bboxSR=4326&imageSR=4326&size={px},{py}&format=tiff&pixelType=F32"
           "&interpolation=RSP_BilinearInterpolation&f=image")
NWS_LIVE_URL = "https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}"
SOURCES = ("svi", "nws_history", "elevation", "nws_live")


# ------------------------------------------------------------------ fault injection + state

def _state_path() -> Path:
    return get_settings().cache / "sources_state.json"


def _state() -> dict:
    p = _state_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st))


def set_fault(source: str, mode: str) -> dict:
    if source not in SOURCES and source != "all":
        raise ValueError(f"unknown source {source}")
    st = _state()
    for src in (SOURCES if source == "all" else (source,)):
        st.setdefault("faults", {})[src] = mode
    _save_state(st)
    return st["faults"]


def fault(source: str) -> str:
    return _state().get("faults", {}).get(source, "none")


def _http(url: str, timeout: float = 5.0, source: str = "", binary: bool = True):
    f = fault(source)
    if f == "down":
        raise ConnectionError(f"{source}: injected fault 'down'")
    if f == "slow":
        time.sleep(timeout + 0.5)
        raise TimeoutError(f"{source}: injected fault 'slow' (timed out)")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    if f == "corrupt":
        body = b"\x00corrupt"
    return body if binary else body.decode("utf-8", "replace")


def _envelope(source: str, data, status: str, fetched_at: str | None, error: str | None = None) -> dict:
    age = None
    if fetched_at:
        try:
            t = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            age = int((datetime.now(timezone.utc) - t).total_seconds())
        except ValueError:
            pass
    st = _state()
    st.setdefault("last", {})[source] = {"status": status, "fetched_at": fetched_at, "error": error, "at": now_iso()}
    _save_state(st)
    return {"data": data, "meta": {"source": source, "status": status, "fetched_at": fetched_at, "age_s": age, "error": error}}


def snap_dir(s: Settings) -> Path:
    return s.snapshots / s.area_id / "context"


def _global_dir(s: Settings) -> Path:
    d = s.snapshots / "global"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_once(s: Settings, url: str, name: str, timeout: float = 300) -> Path:
    p = _global_dir(s) / name
    if not p.exists():
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(_http(url, timeout=timeout))
        tmp.replace(p)
    return p


# ------------------------------------------------------------------ fetchers (write snapshots)

def fetch_svi(s: Settings, log=print) -> Path:
    import geopandas as gpd
    import pandas as pd
    from floodlens.osm import coverage
    csv_p = _download_once(s, SVI_URL, "svi_2022_texas.csv")
    zip_p = _download_once(s, TRACTS_URL, "tl_2022_48_tract.zip")
    svi = pd.read_csv(csv_p, dtype={"FIPS": str})
    keep = ["FIPS", "LOCATION", "E_TOTPOP", "EP_AGE65", "EP_NOVEH", "EP_DISABL", "EP_LIMENG", "EP_POV150",
            "E_AGE65", "E_NOVEH", "E_DISABL", "E_LIMENG", "RPL_THEMES"]
    svi = svi[keep].replace(-999, pd.NA)
    tracts = gpd.read_file(f"zip://{zip_p}")[["GEOID", "geometry"]].to_crs(WGS84)
    cov = coverage(s, 300)
    t = tracts[tracts.intersects(cov)].merge(svi, left_on="GEOID", right_on="FIPS", how="left")
    out = snap_dir(s)
    out.mkdir(parents=True, exist_ok=True)
    t.drop(columns=["FIPS"]).to_file(out / "tracts_svi.geojson", driver="GeoJSON")
    (out / "tracts_svi.meta.json").write_text(json.dumps({"fetched_at": now_iso(), "source": "CDC/ATSDR SVI 2022 + TIGER 2022"}))
    log(f"[{s.area_id}] SVI: {len(t)} tracts")
    return out / "tracts_svi.geojson"


def fetch_nws_history(s: Settings, log=print) -> Path:
    import geopandas as gpd
    from floodlens.osm import coverage
    ev = s.event
    a, b = ev["window"]
    (y1, m1, d1), (y2, m2, d2) = (map(int, a.split("-")), map(int, b.split("-")))
    wfos = "&".join(f"wfo[]={w}" for w in ev["nws_offices"])
    import pandas as pd
    parts = []
    for ph in ("FF", "FL", "FA"):
        name = f"iem_wwa_{'-'.join(ev['nws_offices'])}_{ph}W_{a}_{b}.zip"
        try:
            zp = _download_once(s, IEM_URL.format(y1=y1, m1=m1, d1=d1, y2=y2, m2=m2, d2=d2, wfos=wfos, ph=ph), name, timeout=120)
        except Exception as e:  # noqa: BLE001
            log(f"[{s.area_id}] NWS {ph} warnings not downloaded ({type(e).__name__}); continuing without them")
            continue
        try:
            parts.append(gpd.read_file(f"zip://{zp}").to_crs(WGS84))
        except Exception:  # noqa: BLE001  empty archive for this phenomenon
            pass
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=WGS84) if parts else gpd.GeoDataFrame(columns=["PHENOM", "geometry"], geometry="geometry", crs=WGS84)
    cov = coverage(s)
    flood = g[g["PHENOM"].isin(["FF", "FL", "FA"]) & g.intersects(cov)]
    cols = [c for c in ("WFO", "ISSUED", "EXPIRED", "PHENOM", "SIG", "WFO", "ETN", "STATUS", "geometry") if c in flood.columns]
    flood = flood.loc[:, ~flood.columns.duplicated()][list(dict.fromkeys(cols))]
    out = snap_dir(s)
    out.mkdir(parents=True, exist_ok=True)
    flood.to_file(out / "nws_warnings.geojson", driver="GeoJSON")
    (out / "nws_warnings.meta.json").write_text(json.dumps({"fetched_at": now_iso(), "source": "IEM NWS warning archive"}))
    log(f"[{s.area_id}] NWS flood warnings in window: {len(flood)}")
    return out / "nws_warnings.geojson"


def fetch_elevation(s: Settings, log=print) -> Path:
    w, so, e, n = s.bbox
    utm = utm_crs_for(s.bbox)
    from shapely.geometry import box
    b = reproject_geom(box(w, so, e, n), WGS84, utm).bounds
    px, py = min(2000, max(64, int((b[2] - b[0]) / 10))), min(2000, max(64, int((b[3] - b[1]) / 10)))
    out = snap_dir(s)
    out.mkdir(parents=True, exist_ok=True)
    body = _http(DEM_URL.format(w=w, s=so, e=e, n=n, px=px, py=py), timeout=120, source="elevation")
    if body[:2] not in (b"II", b"MM"):
        raise ValueError(f"3DEP did not return a GeoTIFF: {body[:120]!r}")
    (out / "dem.tif").write_bytes(body)
    (out / "dem.meta.json").write_text(json.dumps({"fetched_at": now_iso(), "source": "USGS 3DEP", "px": [px, py]}))
    log(f"[{s.area_id}] DEM {px}x{py}")
    return out / "dem.tif"


def fetch_all(s: Settings, log=print) -> dict:
    out = {}
    for name, fn in (("svi", fetch_svi), ("nws_history", fetch_nws_history), ("elevation", fetch_elevation)):
        try:
            out[name] = str(fn(s, log))
        except Exception as e:  # noqa: BLE001
            out[name] = f"FAILED: {type(e).__name__}: {e}"
            log(f"[{s.area_id}] {name} failed: {e}")
    try:
        out["nws_live"] = nws_live(s)["meta"]["status"]
    except Exception as e:  # noqa: BLE001
        out["nws_live"] = f"FAILED: {e}"
    # copy context layers for the map
    for src, dst in (("tracts_svi.geojson", "context_tracts.geojson"), ("nws_warnings.geojson", "context_warnings.geojson")):
        p = snap_dir(s) / src
        if p.exists():
            (s.derived / dst).write_text(p.read_text())
    return out


# ------------------------------------------------------------------ getters (envelope)

def _snapshot(s: Settings, name: str, source: str) -> dict:
    p = snap_dir(s) / name
    meta_p = p.with_name(p.name.split(".")[0] + ".meta.json")
    if not p.exists():
        return _envelope(source, None, "unavailable", None, "no snapshot; run fetch-context")
    fetched = json.loads(meta_p.read_text()).get("fetched_at") if meta_p.exists() else None
    if fault(source) == "corrupt":
        return _envelope(source, None, "unavailable", fetched, "snapshot failed validation (injected)")
    return _envelope(source, p, "snapshot", fetched)


_cache: dict[str, tuple[float, dict]] = {}


def nws_live(s: Settings, ttl: float = 60) -> dict:
    """Active NWS alerts at the area centre right now. Live -> cached -> snapshot -> unavailable."""
    w, so, e, n = s.bbox
    key = f"nws_live:{s.area_id}"
    snap = snap_dir(s) / "nws_live.json"
    try:
        body = _http(NWS_LIVE_URL.format(lat=(so + n) / 2, lon=(w + e) / 2), timeout=5, source="nws_live", binary=False)
        js = json.loads(body)
        alerts = [{"event": f["properties"]["event"], "headline": f["properties"].get("headline"),
                   "expires": f["properties"].get("expires")} for f in js.get("features", [])]
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps({"fetched_at": now_iso(), "alerts": alerts}))
        env = _envelope("nws_live", alerts, "live", now_iso())
        _cache[key] = (time.time(), env)
        return env
    except Exception as ex:  # noqa: BLE001
        if key in _cache and time.time() - _cache[key][0] < ttl:
            env = dict(_cache[key][1])
            return _envelope("nws_live", env["data"], "cached", env["meta"]["fetched_at"], str(ex))
        if snap.exists():
            js = json.loads(snap.read_text())
            return _envelope("nws_live", js["alerts"], "snapshot", js["fetched_at"], str(ex))
        return _envelope("nws_live", None, "unavailable", None, str(ex))


_dem_cache: dict[str, object] = {}


def _relative_elevation(s: Settings, lon: float, lat: float, path: Path) -> dict | None:
    import numpy as np
    import rasterio
    if s.area_id not in _dem_cache:
        ds = rasterio.open(path)
        _dem_cache[s.area_id] = (ds, ds.read(1))
    ds, arr = _dem_cache[s.area_id]
    r, c = ds.index(lon, lat)
    if not (0 <= r < arr.shape[0] and 0 <= c < arr.shape[1]):
        return None
    k = 30  # ~300 m at 10 m cells
    win = arr[max(0, r - k): r + k + 1, max(0, c - k): c + k + 1]
    win = win[np.isfinite(win) & (win > -1000)]
    v = float(arr[r, c])
    if not win.size or not np.isfinite(v):
        return None
    return {"elevation_m": round(v, 1), "relative_m": round(v - float(np.median(win)), 1)}


def context_for_segment(s: Settings, f: dict) -> dict:
    """Context items for one road piece, each with its own envelope status."""
    from shapely.geometry import shape
    mid = geom_of(f).interpolate(0.5, normalized=True)
    out: dict = {}
    # warnings active on the photo date over this road
    env = _snapshot(s, "nws_warnings.geojson", "nws_history")
    items = None
    if env["data"]:
        date = (f["properties"].get("after_date") or "")[:10]
        items = []
        for w in json.loads(Path(env["data"]).read_text())["features"]:
            p = w["properties"]
            if shape(w["geometry"]).contains(mid) and (not date or (p.get("ISSUED", "")[:8] <= date.replace("-", "") <= p.get("EXPIRED", "")[:8])):
                items.append({"type": {"FF": "Flash Flood", "FL": "Flood", "FA": "Areal Flood"}.get(p.get("PHENOM"), p.get("PHENOM")),
                              "sig": p.get("SIG"), "issued": p.get("ISSUED"), "expires": p.get("EXPIRED"), "office": p.get("WFO")})
    out["nws_warnings"] = {"data": items, "meta": env["meta"]}
    env = _snapshot(s, "tracts_svi.geojson", "svi")
    tract = None
    if env["data"]:
        for t in json.loads(Path(env["data"]).read_text())["features"]:
            if shape(t["geometry"]).contains(mid):
                p = t["properties"]
                tract = {k: p.get(k) for k in ("GEOID", "LOCATION", "E_TOTPOP", "EP_AGE65", "EP_NOVEH", "EP_DISABL", "EP_LIMENG", "RPL_THEMES")}
                break
    out["svi"] = {"data": tract, "meta": env["meta"]}
    env = _snapshot(s, "dem.tif", "elevation")
    out["elevation"] = {"data": _relative_elevation(s, mid.x, mid.y, Path(env["data"])) if env["data"] else None, "meta": env["meta"]}
    return out


def status(s: Settings) -> list[dict]:
    out = []
    for name, fname in (("svi", "tracts_svi.geojson"), ("nws_history", "nws_warnings.geojson"), ("elevation", "dem.tif")):
        env = _snapshot(s, fname, name)
        out.append({"name": name, **{k: env["meta"][k] for k in ("status", "age_s", "error")}, "fault": fault(name)})
    live = nws_live(s)
    out.append({"name": "nws_live", **{k: live["meta"][k] for k in ("status", "age_s", "error")}, "fault": fault("nws_live")})
    return out
