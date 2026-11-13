"""Fetch the organizer dataset (metadata + only the AOI images) and check it."""
from __future__ import annotations

import json
import math
import time
import urllib.request
from pathlib import Path

import pandas as pd
from PIL import Image

from floodlens.config import Settings

META = "image_metadata.csv"


def _download(url: str, dest: Path, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "floodlens-hackathon"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(dest)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed after {retries} tries: {url}: {last}")


def load_metadata(s: Settings) -> pd.DataFrame:
    path = s.dataset_dir / META
    if not path.exists():
        _download(f"{s['dataset']['hf_base_url']}/{META}", path)
    m = pd.read_csv(path, dtype="string", keep_default_na=False)
    m.attrs["n_source_cols"] = m.shape[1]
    for c in ("west", "south", "east", "north", "latitude", "longitude"):
        m[c + "_f"] = pd.to_numeric(m[c], errors="coerce")
    return m


def tile_xy(west: float, north: float, z: int) -> tuple[int, int]:
    x = int(round((west + 180) / 360 * 2 ** z))
    lat = math.radians(north)
    y = int(round((1 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi) / 2 * 2 ** z))
    return x, y


def area_tiles(s: Settings, m: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per after (response) image in the area, joined to its before image when one exists."""
    m = load_metadata(s) if m is None else m
    locs = set(s.area["localities"])
    after = m[(m["image_role"] == "response") & m["locality"].isin(locs) & m["west_f"].notna()]
    by_id = m.set_index("image_id")
    rows = []
    for _, r in after.iterrows():
        z = int(round(math.log2(360 / (r.east_f - r.west_f))))
        tx, ty = tile_xy(r.west_f, r.north_f, z)
        pre = r["paired_image_id"]
        b = by_id.loc[pre] if pre and pre in by_id.index else None
        rows.append({
            "post_id": r["image_id"], "post_path": r["image_path"], "post_date": r["acquisition_date"],
            "pre_id": pre if b is not None else "", "pre_path": b["image_path"] if b is not None else "",
            "pre_date": b["acquisition_date"] if b is not None else "",
            "west": r.west_f, "south": r.south_f, "east": r.east_f, "north": r.north_f,
            "z": z, "tx": tx, "ty": ty, "locality": r["locality"],
            "spatial_block": r["spatial_block_005deg"], "pair_key": r["pair_key"],
        })
    return pd.DataFrame(rows)


def fetch(s: Settings, log=print) -> pd.DataFrame:
    tiles = area_tiles(s)
    if tiles.empty:
        raise SystemExit(f"No organizer tiles for area {s.area_id}. Check its localities in config.yaml.")
    paths = [p for p in list(tiles.post_path) + list(tiles.pre_path) if p]
    log(f"[{s.area_id}] {len(tiles)} tiles, {len(paths)} images")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = [rel for rel in paths if not ((s.dataset_dir / rel).exists() and (s.dataset_dir / rel).stat().st_size > 0)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_download, f"{s['dataset']['hf_base_url']}/{rel}", s.dataset_dir / rel) for rel in todo]
        for i, f in enumerate(as_completed(futs), 1):
            f.result()
            if i % 50 == 0 or i == len(todo):
                log(f"  {i}/{len(todo)} downloaded")
    s.derived.mkdir(parents=True, exist_ok=True)
    tiles.to_csv(s.derived / "tiles.csv", index=False)
    write_area_info(s, tiles)
    return tiles


def write_area_info(s: Settings, tiles: pd.DataFrame) -> dict:
    from shapely.geometry import box, mapping
    from shapely.ops import unary_union
    cov = unary_union([box(r.west, r.south, r.east, r.north) for r in tiles.itertuples()])
    info = {
        "id": s.area_id, "name": s.area["name"], "event": s.event["name"], "mode": s.area.get("mode", "pair"),
        "bbox": [round(v, 7) for v in cov.bounds], "n_tiles": int(len(tiles)),
        "zooms": sorted(int(z) for z in tiles.z.unique()),
        "after_dates": sorted(tiles.post_date.unique().tolist()),
        "before_dates": sorted(d for d in tiles.pre_date.unique().tolist() if d),
        "localities": sorted(tiles.locality.unique().tolist()),
    }
    (s.derived / "area.json").write_text(json.dumps(info, indent=1))
    (s.derived / "coverage.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": mapping(cov)}]}))
    return info


def check(s: Settings, log=print) -> dict:
    """Checks from the brief. Full-dataset checks run on metadata, image checks on what is downloaded."""
    m = load_metadata(s)
    out: dict = {"rows": len(m), "cols": m.attrs.get("n_source_cols", m.shape[1])}
    ids = set(m.image_id)
    pairs = m[m.paired_image_id.ne("")]
    out["paired_rows"] = len(pairs)
    out["pairs_reciprocal"] = bool(all(
        m.loc[m.image_id == p, "paired_image_id"].iloc[0] == i for i, p in zip(pairs.image_id, pairs.paired_image_id) if p in ids
    ))
    out["prefix_counts"] = m.image_id.str[:2].value_counts().to_dict()
    present, bad = 0, []
    for rel in m.image_path:
        p = s.dataset_dir / rel
        if p.exists():
            present += 1
            try:
                with Image.open(p) as im:
                    im.verify()
            except Exception as e:  # noqa: BLE001
                bad.append(f"{rel}: {e}")
    out["images_present"] = present
    out["images_bad"] = bad
    exp = {"rows": 1204, "cols": 273, "paired_rows": 1150}
    out["ok"] = all(out[k] == v for k, v in exp.items()) and out["pairs_reciprocal"] and not bad
    for k, v in out.items():
        log(f"  {k}: {v}")
    return out
