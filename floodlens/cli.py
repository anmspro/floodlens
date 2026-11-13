"""FloodLens CLI. Run from the repo root:  python -m floodlens <command>

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
            line = f"| {stamp} | {name} | {status} | {time.time() - t0:.1f}s | {_git_rev()} | {platform.node()} | {err_file} |\n"
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


@app.command("fetch-data")
@logged
def fetch_data():
    """Download image_metadata.csv and only the AOI before/after images from Hugging Face."""
    from floodlens import data
    data.fetch(get_settings())


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
def imagery():
    """Write GeoTIFFs (EPSG:3857), build pre/post mosaics and web overlays for the AOI."""
    import pandas as pd
    from floodlens import data, imagery as im
    s = get_settings()
    p = s.derived / "aoi_pairs.csv"
    pairs = pd.read_csv(p) if p.exists() else data.aoi_pairs(s)
    tifs = im.build_geotiffs(s, pairs)
    im.build_mosaics(s, tifs)
    im.build_web_overlays(s, pairs)


@app.command()
@logged
def prepare():
    """Fetch OSM roads, buildings and facilities for the AOI and split roads into <=100 m segments."""
    from floodlens import osm
    osm.prepare(get_settings())


@app.command()
@logged
def chips(force: bool = False):
    """Cut before/after chips for every road segment."""
    from floodlens import roads
    roads.make_chips(get_settings(), force=force)


@app.command()
@logged
def label(force: bool = False, limit: int = typer.Option(None, help="label at most N segments")):
    """Ask the VLM about every pending road segment (cached)."""
    from floodlens import roads
    print(roads.label_roads(get_settings(), force=force, limit=limit))


@app.command("vlm-check")
@logged
def vlm_check(n: int = 20):
    """One image question on the first AOI pair, then seconds per chip on N chips."""
    from floodlens import roads
    print(json.dumps(roads.vlm_check(get_settings(), n=n), indent=1))


@app.command()
@logged
def analyze():
    """Compute edge status, cut-off zones (strict and permissive) and facility reachability."""
    from floodlens.analysis import Network
    print(Network(get_settings()).run())


@app.command()
@logged
def serve(port: int = 8501, host: str = "127.0.0.1"):
    """Run the web app."""
    import uvicorn
    uvicorn.run("floodlens.api:app", host=host, port=port, reload=False)


@app.command("all")
@logged
def run_all():
    """fetch-data -> imagery -> prepare -> chips -> label -> analyze"""
    from floodlens import data, imagery as im, osm, roads
    from floodlens.analysis import Network
    s = get_settings()
    pairs = data.fetch(s)
    tifs = im.build_geotiffs(s, pairs)
    im.build_mosaics(s, tifs)
    im.build_web_overlays(s, pairs)
    osm.prepare(s)
    roads.make_chips(s)
    roads.label_roads(s)
    print(Network(s).run())
