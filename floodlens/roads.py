"""Road chips (before + after, with the segment drawn in magenta) and VLM labelling."""
from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

from PIL import Image, ImageDraw
from shapely.geometry import box

from floodlens.config import Settings
from floodlens.geo import WEBMERC, WGS84, geom_of, now_iso, read_geojson, reproject_geom, utm_crs_for, write_geojson
from floodlens.imagery import Mosaic, mosaic_path
from floodlens.vlm.client import VLM, status_from_label

HIGHWAY_ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
                 "living_street", "service"]
MAGENTA = (255, 0, 255, 153)  # 60% opacity


def seg_path(s: Settings) -> Path:
    return s.derived / "road_segments.geojson"


def chip_dir(s: Settings) -> Path:
    return s.chips / "roads"


def chip_files(s: Settings, sid: str) -> dict[str, Path]:
    d = chip_dir(s)
    return {k: d / f"{sid}_{k}.jpg" for k in ("pre", "post", "pre_raw", "post_raw")}


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
        raise SystemExit("No road_segments.geojson. Run `python -m floodlens prepare` first.")
    rc = s["roads"]
    px = int(rc["chip_px"])
    utm = utm_crs_for(s.bbox)
    mos = {e: Mosaic(mosaic_path(s, e)) for e in ("pre", "post")}
    chip_dir(s).mkdir(parents=True, exist_ok=True)
    counts = {"made": 0, "skipped": 0, "no_imagery": 0}
    try:
        for f in feats:
            p = f["properties"]
            files = chip_files(s, p["id"])
            if not force and files["post"].exists():
                counts["skipped"] += 1
                continue
            line_utm = reproject_geom(geom_of(f), WGS84, utm)
            sq = reproject_geom(_square_utm(line_utm, rc["chip_buffer_m"], rc["chip_min_side_m"]), utm, WEBMERC)
            bounds = sq.bounds
            line_3857 = reproject_geom(geom_of(f), WGS84, WEBMERC)
            post, valid_post = mos["post"].chip(*bounds, px)
            pre, valid_pre = mos["pre"].chip(*bounds, px)
            if valid_post < 0.5 or valid_pre < 0.5:
                if p["status_source"] not in ("human",):
                    p["status"] = "no_imagery"
                    p["chip_url"] = None
                counts["no_imagery"] += 1
                continue
            for name, rgb in (("pre", pre), ("post", post)):
                Image.fromarray(rgb).save(files[f"{name}_raw"], "JPEG", quality=88)
                _draw(rgb, line_3857, bounds, px).save(files[name], "JPEG", quality=88)
            p["chip_url"] = f"/chips/roads/{p['id']}_post.jpg"
            if p["status"] == "no_imagery":
                p["status"] = "pending"
            counts["made"] += 1
    finally:
        for m in mos.values():
            m.close()
    write_geojson(seg_path(s), feats)
    log(f"chips: {counts}")
    return counts


def _order_key(f):
    hw = f["properties"].get("highway") or ""
    return (HIGHWAY_ORDER.index(hw) if hw in HIGHWAY_ORDER else len(HIGHWAY_ORDER), f["properties"]["id"])


async def _label_all(s: Settings, todo: list[dict], feats: list[dict], log) -> dict:
    vlm = VLM(s)
    pid = s["vlm"]["prompt_id"]
    th = s.get("thresholds.road", {})
    ev = s["event"]
    done, t0, counts = 0, time.time(), {"open": 0, "blocked": 0, "uncertain": 0}

    async def one(f):
        files = chip_files(s, f["properties"]["id"])
        imgs = [files["pre"].read_bytes(), files["post"].read_bytes()]
        label, meta = await vlm.label(imgs, pid, before_date=ev.get("before_date", "2016"), after_date=ev["date"])
        return f, label, meta

    tasks = [asyncio.create_task(one(f)) for f in todo]
    for fut in asyncio.as_completed(tasks):
        f, label, meta = await fut
        p = f["properties"]
        st = status_from_label(label, th)
        p.update(status=st, status_source="vlm", confidence=label.get("confidence"), label=label,
                 prompt_version=pid, updated_at=now_iso(), model=s.vlm_model + (" (mock)" if s.vlm_mock else ""))
        counts[st] += 1
        done += 1
        if done % 50 == 0 or done == len(todo):
            write_geojson(seg_path(s), feats)
            rate = (time.time() - t0) / done
            log(f"  {done}/{len(todo)}  {rate:.2f}s/chip  {counts}")
    return counts


def label_roads(s: Settings, force: bool = False, limit: int | None = None, log=print) -> dict:
    feats = read_geojson(seg_path(s))
    lim = limit or int(s.get("limits.max_road_segments", 1500))
    todo = []
    for f in sorted(feats, key=_order_key):
        p = f["properties"]
        if p["status_source"] == "human" or not p.get("chip_url"):
            continue
        if p["status"] == "pending" or (force and p["status_source"] == "vlm"):
            todo.append(f)
    todo = todo[:lim]
    log(f"labelling {len(todo)} segments with {s.vlm_model}{' (MOCK)' if s.vlm_mock else ''}")
    if not todo:
        return {}
    return asyncio.run(_label_all(s, todo, feats, log))


def vlm_check(s: Settings, n: int = 20, log=print) -> dict:
    """One free-form question on a full tile pair, then timing on n road chips."""
    import pandas as pd
    vlm = VLM(s)
    pairs = pd.read_csv(s.derived / "aoi_pairs.csv")
    r = pairs.iloc[0]

    def jpg(path: Path) -> bytes:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((768, 768))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=88)
            return b.getvalue()

    imgs = [jpg(s.dataset_dir / r.pre_path), jpg(s.dataset_dir / r.post_path)]
    t = time.time()
    ans = asyncio.run(vlm.ask(imgs, "Image 1 is before Hurricane Harvey (2016), image 2 is after (2017-08-30). "
                                     "Is floodwater covering roads in image 2? Answer in one sentence."))
    log(f"[{r.post_id}] {time.time() - t:.1f}s  {ans}")
    out = {"tile": r.post_id, "answer": ans}
    feats = [f for f in read_geojson(seg_path(s)) if f["properties"].get("chip_url")][:n]
    if feats:
        async def run():
            ts = []
            for f in feats:
                files = chip_files(s, f["properties"]["id"])
                t1 = time.time()
                await vlm.label([files["pre"].read_bytes(), files["post"].read_bytes()], s["vlm"]["prompt_id"],
                                before_date=s["event"].get("before_date", "2016"), after_date=s["event"]["date"])
                ts.append(time.time() - t1)
            return ts
        ts = asyncio.run(run())
        out["sec_per_chip"] = sum(ts) / len(ts)
        log(f"{len(ts)} chips, {out['sec_per_chip']:.2f}s per chip (cache hits count as 0)")
    return out
