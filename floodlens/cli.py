"""FloodLens CLI. Run from the repo root:  python -m floodlens <command> [--area NAME|all]

Every command appends a line to ops/RUNLOG.md. A failing command also writes
ops/errors/<time>_<command>.md with the traceback, so it can be committed and
pushed for someone else to fix.
"""
from __future__ import annotations

import functools
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

import typer

from floodlens.config import ROOT, get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
OPS = ROOT / "ops"
AREA = typer.Option(None, "--area", "-a", help="area id from config.yaml, or 'all' (default: default_area)")


def _git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "no-git"


def _redact(text: str) -> str:
    key = os.environ.get("VLM_API_KEY", "")
    return text.replace(key, "***") if key and key not in ("EMPTY", "") else text


def logged(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        name = fn.__name__.replace("_", "-")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        t0 = time.time()
        OPS.mkdir(exist_ok=True)
        status, err_file = "ok", ""
        try:
            return fn(*a, **kw)
        except SystemExit as e:
            if e.code not in (0, None):
                status = "fail"
                err_file = _write_error(name, stamp, f"SystemExit: {e.code}")
            raise
        except BaseException:  # noqa: BLE001
            status = "fail"
            err_file = _write_error(name, stamp, traceback.format_exc())
            raise
        finally:
            area = kw.get("area") or ""
            line = f"| {stamp} | {name} {area} | {status} | {time.time() - t0:.1f}s | {_git_rev()} | {platform.node()} | {err_file} |\n"
            log = OPS / "RUNLOG.md"
            if not log.exists():
                log.write_text("| time (UTC) | command | status | duration | commit | host | error file |\n|---|---|---|---|---|---|---|\n")
            with open(log, "a") as fh:
                fh.write(line)
    return wrapper


def _write_error(name: str, stamp: str, tb: str) -> str:
    d = OPS / "errors"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stamp}_{name}.md"
    path.write_text(_redact(
        f"# {name} failed\n\n- time: {stamp}\n- command: `{' '.join(sys.argv)}`\n- commit: {_git_rev()}\n"
        f"- host: {platform.node()} ({platform.system()} {platform.machine()}), python {platform.python_version()}\n\n"
        f"```\n{tb[-12000:]}\n```\n"))
    print(f"\n!! error report written to {path.relative_to(ROOT)}  (commit and push it so it can be fixed)", file=sys.stderr)
    return str(path.relative_to(ROOT))


def areas(area: str | None):
    base = get_settings()
    ids = base.area_ids if area == "all" else [area or base.raw.get("default_area")]
    return [base.for_area(a) for a in ids]


@app.command("fetch-data")
@logged
def fetch_data(area: str = AREA):
    """Download image_metadata.csv and the before/after images of the area(s) from Hugging Face."""
    from floodlens import data
    for s in areas(area):
        data.fetch(s)


@app.command("check-data")
@logged
def check_data():
    """Check metadata (1,204 x 273, 575 reciprocal pairs) and decode every downloaded image."""
    from floodlens import data
    out = data.check(get_settings())
    if not out["ok"]:
        raise SystemExit("data check failed, see output above")


@app.command()
@logged
def imagery(area: str = AREA):
    """Write one GeoTIFF (EPSG:3857) per photo and cut web map tiles."""
    import pandas as pd
    from floodlens import imagery as im
    for s in areas(area):
        tiles = pd.read_csv(s.derived / "tiles.csv", keep_default_na=False)
        im.build_geotiffs(s, tiles)
        im.build_web_tiles(s, tiles)


@app.command()
@logged
def prepare(area: str = AREA):
    """Fetch OSM roads, buildings and facilities inside the photographed area; cut roads into <=100 m pieces."""
    from floodlens import osm
    for s in areas(area):
        osm.prepare(s)


@app.command()
@logged
def chips(area: str = AREA, force: bool = False):
    """Cut before/after chips for every road piece."""
    from floodlens import roads
    for s in areas(area):
        roads.make_chips(s, force=force)


@app.command()
@logged
def label(area: str = AREA, force: bool = False, limit: int = typer.Option(None, help="label at most N pieces")):
    """Ask the VLM jury about every pending road piece (cached)."""
    from floodlens import roads
    for s in areas(area):
        print(roads.label_roads(s, force=force, limit=limit))


@app.command("vlm-check")
@logged
def vlm_check(area: str = AREA, n: int = 20):
    """One image question on the first tile, then seconds per road on N chips."""
    from floodlens import roads
    for s in areas(area):
        print(json.dumps(roads.vlm_check(s, n=n), indent=1))


@app.command()
@logged
def analyze(area: str = AREA):
    """Edge status, cut-off neighbourhoods (strict and permissive) and facility reachability."""
    from floodlens.analysis import Network
    for s in areas(area):
        print(s.area_id, Network(s).run())


@app.command()
@logged
def evaluate(area: str = AREA, n: int = 60):
    """Baselines, controls and (when hand labels exist) accuracy. Writes docs/EVALUATION.md."""
    from floodlens import evaluate as ev
    ev.run(areas(area), n=n)


@app.command("fetch-context")
@logged
def fetch_context(area: str = AREA):
    """Download public context data (SVI, tracts, NWS warnings, FloodSentinel...) into snapshots."""
    from floodlens import sources
    for s in areas(area):
        sources.fetch_all(s)


@app.command()
@logged
def impact(area: str = AREA, n: int = 600):
    """Economic impact estimate: sampled buildings + farmland checked by the VLM (plan 8a)."""
    from floodlens import impact as im
    for s in areas(area):
        im.run(s, n=n)


@app.command()
@logged
def relief(area: str = AREA):
    """Relief site recommender (plan 8b)."""
    from floodlens import relief as rl
    for s in areas(area):
        rl.run(s)


@app.command("sa-fetch")
@logged
def sa_fetch(area: str = typer.Option("san_antonio", "--area", "-a")):
    """San Antonio: low-water crossings + SmartSA flood sensors, coverage = 1 km around crossings."""
    from floodlens import sa
    sa.fetch(get_settings().for_area(area))


@app.command("sa-scenario")
@logged
def sa_scenario(area: str = typer.Option("san_antonio", "--area", "-a"), scenario: str = "all_crossings_flooded"):
    """San Antonio: close every low-water crossing (or 'none' to reopen), then run analyze."""
    from floodlens import sa
    from floodlens.analysis import Network
    s = get_settings().for_area(area)
    sa.apply_scenario(s, scenario)
    print(Network(s).run())


@app.command("prefetch-briefs")
@logged
def prefetch_briefs(area: str = AREA, top: int = 5):
    """Write and save dispatch briefs (English + Spanish) for the top cut-off zones, for offline demos."""
    from floodlens import brief as b
    from floodlens.geo import read_geojson
    for s in areas(area):
        for mode in ("permissive", "strict"):
            zones = read_geojson(s.derived / f"cutoff_zones_{mode}.geojson")[:top]
            for z in zones:
                for lang in ("en", "es"):
                    out = b.make_brief(s, z["properties"]["id"], mode, lang)
                    print(s.area_id, mode, z["properties"]["id"], lang, "ok" if not out.get("from_cache") else "cached")


@app.command("offline-check")
@logged
def offline_check(area: str = AREA):
    """Confirm every file the demo needs is on disk (run before judging, then try the app with Wi-Fi off)."""
    need = ["area.json", "tiles.csv", "web_tiles.json", "graph.graphml", "exits.json", "road_segments.geojson",
            "road_edges.geojson", "buildings.geojson", "facilities.geojson", "cutoff_zones_permissive.geojson",
            "cutoff_zones_strict.geojson", "summary.json", "impact.json", "impact_buildings.geojson",
            "relief.json", "relief_sites.geojson", "context_tracts.geojson", "context_warnings.geojson"]
    ok_all = True
    for s in areas(area):
        scen = s.area.get("mode") == "scenario"
        need_here = [f for f in need if not (scen and (f.startswith(("impact", "relief", "web_tiles")) or f == "tiles.csv"))]
        missing = [f for f in need_here if not (s.derived / f).exists()]
        segs = (s.derived / "road_segments.geojson")
        n_chips = len(list((s.chips / "roads").glob("*_post.jpg"))) if (s.chips / "roads").exists() else 0
        n_tiles = sum(1 for _ in s.tiles.rglob("*.webp")) if s.tiles.exists() else 0
        briefs = len(list((s.derived / "briefs").glob("*.json"))) if (s.derived / "briefs").exists() else 0
        snaps = [p.name for p in (s.snapshots / s.area_id / "context").glob("*")] if (s.snapshots / s.area_id / "context").exists() else []
        pending = 0
        if segs.exists():
            import json as _j
            pending = sum(1 for f in _j.loads(segs.read_text())["features"] if f["properties"]["status"] == "pending")
        ok = not missing and (n_tiles > 0 or scen) and pending == 0
        ok_all &= ok
        print(f"[{'OK ' if ok else 'GAP'}] {s.area_id}: map tiles {n_tiles}, road crops {n_chips}, saved briefs {briefs}, "
              f"unlabelled roads {pending}, context snapshots {len(snaps)}" + (f"\n      missing: {', '.join(missing)}" if missing else ""))
    if not ok_all:
        raise SystemExit("some areas are not demo-ready (see GAP lines)")


@app.command()
@logged
def serve(port: int = 8501, host: str = "127.0.0.1"):
    """Run the web app."""
    import uvicorn
    uvicorn.run("floodlens.api:app", host=host, port=port, reload=False)


@app.command("all")
@logged
def run_all(area: str = AREA):
    """fetch-data -> imagery -> prepare -> chips -> label -> analyze"""
    from floodlens import data, imagery as im, osm, roads
    from floodlens.analysis import Network
    for s in areas(area):
        tiles = data.fetch(s)
        im.build_geotiffs(s, tiles)
        im.build_web_tiles(s, tiles)
        osm.prepare(s)
        roads.make_chips(s)
        roads.label_roads(s)
        print(s.area_id, Network(s).run())
