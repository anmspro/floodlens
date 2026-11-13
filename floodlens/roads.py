"""Road chips (before + after, segment drawn in magenta) and VLM labelling with a two-question jury."""
from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw
from shapely.geometry import box

from floodlens.config import Settings
from floodlens.geo import WEBMERC, WGS84, geom_of, now_iso, read_geojson, reproject_geom, utm_crs_for, write_geojson
from floodlens.imagery import TileIndex
from floodlens.vlm.client import VLM

HIGHWAY_ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
                 "living_street", "service"]
MAGENTA = (255, 0, 255, 153)  # 60% opacity
JURY_VERSION = "jury_v1"


def seg_path(s: Settings) -> Path:
    return s.derived / "road_segments.geojson"


def chip_dir(s: Settings) -> Path:
    return s.chips / "roads"


def chip_files(s: Settings, sid: str) -> dict[str, Path]:
    d = chip_dir(s)
    return {k: d / f"{sid}_{k}.jpg" for k in ("pre", "post", "pre_raw", "post_raw")}


def chip_url(s: Settings, sid: str, kind: str = "post") -> str:
    return f"/chips/{s.area_id}/roads/{sid}_{kind}.jpg"


def _square_utm(line_utm, buffer_m: float, min_side: float):
    minx, miny, maxx, maxy = line_utm.buffer(buffer_m).bounds
    side = max(maxx - minx, maxy - miny, min_side)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    return box(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def _draw(rgb, line_3857, bounds, px: int) -> Image.Image:
    x0, y0, x1, y1 = bounds
    base = Image.fromarray(rgb).convert("RGBA")
    over = Image.new("RGBA", base.size, (0, 0, 0, 0))
    pts = [((x - x0) / (x1 - x0) * px, (y1 - y) / (y1 - y0) * px) for x, y in line_3857.coords]
    ImageDraw.Draw(over).line(pts, fill=MAGENTA, width=max(3, px // 150), joint="curve")
    return Image.alpha_composite(base, over).convert("RGB")


def make_chips(s: Settings, force: bool = False, log=print) -> dict:
    feats = read_geojson(seg_path(s))
    if not feats:
        raise SystemExit(f"No road_segments.geojson for {s.area_id}. Run `python -m floodlens prepare` first.")
    tiles = pd.read_csv(s.derived / "tiles.csv", keep_default_na=False)
    rc = s["roads"]
    px, min_valid = int(rc["chip_px"]), float(rc.get("min_valid_fraction", 0.5))
    utm = utm_crs_for(s.bbox)
    idx = {"post": TileIndex(s, tiles, "post")}
    if s.paired:
        idx["pre"] = TileIndex(s, tiles, "pre")
    chip_dir(s).mkdir(parents=True, exist_ok=True)
    counts = {"made": 0, "skipped": 0, "no_imagery": 0}
    try:
        for i, f in enumerate(feats, 1):
            p = f["properties"]
            files = chip_files(s, p["id"])
            if not force and files["post"].exists() and p.get("chip_url"):
                counts["skipped"] += 1
                continue
            line_utm = reproject_geom(geom_of(f), WGS84, utm)
            bounds = reproject_geom(_square_utm(line_utm, rc["chip_buffer_m"], rc["chip_min_side_m"]), utm, WEBMERC).bounds
            line_3857 = reproject_geom(geom_of(f), WGS84, WEBMERC)
            mid = line_3857.interpolate(0.5, normalized=True)
            rgbs = {}
            ok = True
            for epoch, ti in idx.items():
                rgb, valid = ti.chip(*bounds, px)
                rgbs[epoch] = rgb
                ok &= valid >= min_valid
            if not ok:
                if p["status_source"] != "human":
                    inside = idx["post"].tile_at(mid.x, mid.y)[0] is not None
                    p.update(status="no_imagery" if inside else "outside", chip_url=None)
                counts["no_imagery"] += 1
                continue
            for epoch, rgb in rgbs.items():
                Image.fromarray(rgb).save(files[f"{epoch}_raw"], "JPEG", quality=88)
                _draw(rgb, line_3857, bounds, px).save(files[epoch], "JPEG", quality=88)
            tile_id, tile_date = idx["post"].tile_at(mid.x, mid.y)
            p.update(chip_url=chip_url(s, p["id"]), tile_id=tile_id, after_date=tile_date, has_pre=s.paired)
            if p["status"] == "no_imagery":
                p["status"] = "pending"
            counts["made"] += 1
            if i % 1000 == 0:
                log(f"  chips {i}/{len(feats)}")
    finally:
        for ti in idx.values():
            ti.close()
    write_geojson(seg_path(s), feats)
    log(f"[{s.area_id}] chips: {counts}")
    return counts


def decide(pair: dict | None, surface: dict) -> dict:
    """Jury rule, safety first: any juror seeing water makes the road blocked.
    Open needs both jurors to agree the road is dry (pair areas) or a clear dry view (after-only)."""
    sf, vis = surface.get("surface"), surface.get("road_visible")
    pair_water = pair is not None and pair.get("answer") == "yes"
    if pair_water or sf == "water":
        confs = [c for c in ((pair or {}).get("confidence") if pair_water else None,
                             surface.get("confidence") if sf == "water" else None) if c is not None]
        who = " and ".join(w for w, ok in (("pair question", pair_water), ("photo question", sf == "water")) if ok)
        return {"passable": "no", "confidence": max(confs) if confs else 0.6, "rule": f"water seen by {who}"}
    pair_dry = pair is None or (pair.get("answer") == "no" and pair.get("water_on_road") in ("none", "unknown"))
    if pair_dry and sf == "dry_pavement" and vis != "no":
        confs = [c for c in ((pair or {}).get("confidence"), surface.get("confidence")) if c is not None]
        return {"passable": "yes", "confidence": min(confs) if confs else 0.6, "rule": "both questions see a dry road"}
    if sf == "mud_or_debris":
        return {"passable": "uncertain", "confidence": surface.get("confidence", 0.5), "rule": "mud or debris on road, check by eye"}
    if sf in ("trees", "shadow") or vis == "no":
        return {"passable": "uncertain", "confidence": 0.3, "rule": f"road hidden by {sf or 'the view'}"}
    return {"passable": "uncertain", "confidence": 0.4, "rule": "the two questions disagree"}


def status_of(decision: dict, th: dict) -> str:
    p, c = decision["passable"], float(decision.get("confidence") or 0)
    if p == "no" and c >= th.get("blocked_min_conf", 0.6):
        return "blocked"
    if p == "yes" and c >= th.get("open_min_conf", 0.6):
        return "open"
    return "uncertain"


async def judge(vlm: VLM, s: Settings, files: dict[str, Path], after_date: str, swap: bool = False,
                pre_override: bytes | None = None) -> dict:
    """Run the jury on one road chip. swap/pre_override are for the controls in evaluate.py."""
    post = files["post"].read_bytes()
    surface_t = vlm.label([post], "surface_v1", after_date=after_date)
    if s.paired:
        pre = pre_override if pre_override is not None else files["pre"].read_bytes()
        imgs = [post, pre] if swap else [pre, post]
        pair_t = vlm.label(imgs, "roads_v2", before_date=s.event.get("before_label") or "before", after_date=after_date)
        (pair, _), (surface, _) = await asyncio.gather(pair_t, surface_t)
    else:
        pair, (surface, _) = None, await surface_t
    d = decide(pair, surface)
    reason = (surface.get("reason") or "") if d["passable"] != "no" or surface.get("surface") == "water" else (pair or {}).get("reason", "")
    return {"pair": pair, "surface": surface, **d, "reason": reason or (pair or {}).get("reason", ""),
            "answer": (pair or {}).get("answer"), "water_on_road": (pair or {}).get("water_on_road"),
            "visibility": surface.get("road_visible")}


def _order_key(f):
    hw = f["properties"].get("highway") or ""
    return (HIGHWAY_ORDER.index(hw) if hw in HIGHWAY_ORDER else len(HIGHWAY_ORDER), f["properties"]["id"])


async def _label_all(s: Settings, todo: list[dict], feats: list[dict], log) -> dict:
    vlm = VLM(s)
    th = s.get("thresholds.road", {})
    done, t0, counts = 0, time.time(), {"open": 0, "blocked": 0, "uncertain": 0}
    default_date = (s.area_info.get("after_dates") or ["unknown date"])[-1]

    async def one(f):
        p = f["properties"]
        return f, await judge(vlm, s, chip_files(s, p["id"]), p.get("after_date") or default_date)

    tasks = [asyncio.create_task(one(f)) for f in todo]
    for fut in asyncio.as_completed(tasks):
        f, label = await fut
        p = f["properties"]
        st = status_of(label, th)
        p.update(status=st, status_source="vlm", confidence=label.get("confidence"), label=label,
                 prompt_version=JURY_VERSION if s.paired else "surface_v1", updated_at=now_iso(),
                 model=s.vlm_model + (" (mock)" if s.vlm_mock else ""))
        counts[st] += 1
        done += 1
        if done % 200 == 0 or done == len(todo):
            write_geojson(seg_path(s), feats)
            log(f"  [{s.area_id}] {done}/{len(todo)}  {(time.time() - t0) / done:.2f}s/road  {counts}")
    return counts


def label_roads(s: Settings, force: bool = False, limit: int | None = None, log=print) -> dict:
    if s.area.get("mode") == "scenario":
        log(f"[{s.area_id}] scenario area: no photos to label")
        return {}
    feats = read_geojson(seg_path(s))
    todo = []
    for f in sorted(feats, key=_order_key):
        p = f["properties"]
        if p["status_source"] == "human" or not p.get("chip_url"):
            continue
        if p["status"] == "pending" or (force and p["status_source"] == "vlm"):
            todo.append(f)
    if limit:
        todo = todo[:limit]
    log(f"[{s.area_id}] labelling {len(todo)} road pieces with {s.vlm_model}{' (MOCK)' if s.vlm_mock else ''}")
    if not todo:
        return {}
    return asyncio.run(_label_all(s, todo, feats, log))


def vlm_check(s: Settings, n: int = 20, log=print) -> dict:
    """One free-form question on a full tile, then timing of the jury on n road chips."""
    vlm = VLM(s)
    tiles = pd.read_csv(s.derived / "tiles.csv", keep_default_na=False)
    r = tiles.iloc[0]

    def jpg(path: Path) -> bytes:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((768, 768))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=88)
            return b.getvalue()

    imgs = ([jpg(s.dataset_dir / r.pre_path)] if r.pre_path else []) + [jpg(s.dataset_dir / r.post_path)]
    q = ("Image 1 is before the flood, image 2 is after. " if len(imgs) == 2 else "") + \
        "Is floodwater covering roads in the last image? Answer in one sentence."
    t = time.time()
    ans = asyncio.run(vlm.ask(imgs, q))
    log(f"[{r.post_id}] {time.time() - t:.1f}s  {ans}")
    out = {"tile": r.post_id, "answer": ans}
    feats = [f for f in read_geojson(seg_path(s)) if f["properties"].get("chip_url")][:n]
    if feats:
        async def run():
            ts = []
            for f in feats:
                t1 = time.time()
                await judge(vlm, s, chip_files(s, f["properties"]["id"]), f["properties"].get("after_date") or "")
                ts.append(time.time() - t1)
            return ts
        ts = asyncio.run(run())
        out["sec_per_road"] = sum(ts) / len(ts)
        log(f"{len(ts)} roads, {out['sec_per_road']:.2f}s per road (cache hits count as 0)")
    return out
