"""Fetch the organizer dataset (metadata + only the AOI images) and check it."""
from __future__ import annotations

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


def aoi_pairs(s: Settings, m: pd.DataFrame | None = None) -> pd.DataFrame:
    """Response (after) rows whose footprint intersects the AOI, joined to their before image."""
    m = load_metadata(s) if m is None else m
    w, so, e, n = s.bbox
    after = m[(m["image_role"] == "response") & m["paired_image_id"].ne("")]
    eps = 1e-6  # tiles that only share an edge with the AOI are not included
    hit = after[(after.west_f < e - eps) & (after.east_f > w + eps) & (after.south_f < n - eps) & (after.north_f > so + eps)]
    before = m.set_index("image_id")
    rows = []
    for _, r in hit.iterrows():
        b = before.loc[r["paired_image_id"]]
        rows.append({
            "pair_key": r["pair_key"],
            "post_id": r["image_id"], "post_path": r["image_path"], "post_date": r["acquisition_date"],
            "pre_id": r["paired_image_id"], "pre_path": b["image_path"], "pre_date": b["acquisition_date"],
            "west": r.west_f, "south": r.south_f, "east": r.east_f, "north": r.north_f,
            "locality": r["locality"], "spatial_block": r["spatial_block_005deg"],
        })
    return pd.DataFrame(rows)


def fetch(s: Settings, log=print) -> pd.DataFrame:
    m = load_metadata(s)
    pairs = aoi_pairs(s, m)
    if pairs.empty:
        raise SystemExit("No organizer tiles intersect aoi.bbox. Check config.yaml.")
    paths = list(pairs.post_path) + list(pairs.pre_path)
    log(f"AOI has {len(pairs)} before/after pairs ({len(paths)} images)")
    for i, rel in enumerate(paths, 1):
        dest = s.dataset_dir / rel
        if dest.exists() and dest.stat().st_size > 0:
            continue
        _download(f"{s['dataset']['hf_base_url']}/{rel}", dest)
        log(f"  [{i}/{len(paths)}] {rel}")
    s.derived.mkdir(parents=True, exist_ok=True)
    pairs.to_csv(s.derived / "aoi_pairs.csv", index=False)
    return pairs


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
