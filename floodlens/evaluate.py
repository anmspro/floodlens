"""Evaluation (plan amendment 6): hand labels, baselines, controls, metrics. Writes docs/EVALUATION.md.

Hand labels come from the review buttons in the app and are appended to eval/labels/<area>.csv
(tracked in git, small). Nothing here reports a model number without the baselines and
controls next to it.
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np
from PIL import Image

from floodlens.config import ROOT, Settings
from floodlens.geo import read_geojson
from floodlens.roads import chip_files, decide, status_of
from floodlens.vlm.client import VLM

EVAL = ROOT / "eval"


def labels_path(s: Settings):
    return EVAL / "labels" / f"{s.area_id}.csv"


def record_label(s: Settings, segment_id: str, status: str, reviewer: str | None, vlm_status: str | None) -> None:
    p = labels_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time_utc", "area", "segment_id", "label", "reviewer", "model_status_at_review"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), s.area_id, segment_id, status,
                    reviewer or "anonymous", vlm_status or ""])


def load_labels(s: Settings) -> dict[str, dict[str, str]]:
    """segment_id -> {reviewer: label} (latest label per reviewer wins)."""
    p = labels_path(s)
    out: dict[str, dict[str, str]] = defaultdict(dict)
    if p.exists():
        for r in csv.DictReader(open(p)):
            out[r["segment_id"]][r["reviewer"]] = r["label"]
    return out


# ------------------------------------------------------------------ baselines

def colour_water_fraction(path) -> float:
    """Share of brown/grey water-like pixels in the centre band of a raw chip (crude on purpose)."""
    a = np.asarray(Image.open(path).convert("RGB")).astype(int)
    h, w, _ = a.shape
    c = a[h // 2 - h // 8: h // 2 + h // 8, w // 2 - w // 8: w // 2 + w // 8]
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    bright = (r + g + b) / 3
    brownish = (r - b > 12) & (g - b > 6) & (np.abs(r - g) < 35) & (bright > 70) & (bright < 190)
    return float(brownish.mean())


def baseline_colour(s: Settings, sid: str) -> str:
    f = chip_files(s, sid)
    post = colour_water_fraction(f["post_raw"])
    pre = colour_water_fraction(f["pre_raw"]) if f["pre_raw"].exists() else 0.0
    return "blocked" if post - pre > 0.35 else "open"


def tile_fema(s: Settings) -> dict[str, str]:
    import pandas as pd
    m = pd.read_csv(s.dataset_dir / "image_metadata.csv", dtype="string", keep_default_na=False)
    return dict(zip(m.image_id, m.fema_sfha_tf))


# ------------------------------------------------------------------ metrics

def confusion(pairs: list[tuple[str, str]]) -> dict:
    """pairs of (truth, prediction) with truth in {open, blocked}. Prediction may be uncertain."""
    c = Counter(pairs)
    tp, fn = c[("blocked", "blocked")], c[("blocked", "open")]
    fp, tn = c[("open", "blocked")], c[("open", "open")]
    abst = sum(v for (t, p), v in c.items() if p == "uncertain")
    n = sum(c.values())
    decided = n - abst
    return {"n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn, "abstained": abst,
            "recall_flooded": round(tp / (tp + fn), 3) if tp + fn else None,
            "recall_flooded_counting_abstain_as_miss": round(tp / (tp + fn + c[("blocked", "uncertain")]), 3) if tp + fn + c[("blocked", "uncertain")] else None,
            "precision_flooded": round(tp / (tp + fp), 3) if tp + fp else None,
            "accuracy_decided": round((tp + tn) / decided, 3) if decided else None,
            "abstention_rate": round(abst / n, 3) if n else None}


def kappa(a: list[str], b: list[str]) -> float | None:
    if not a:
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else None


def from_label(label: dict | None, which: str, th: dict) -> str:
    if not label:
        return "uncertain"
    if which == "jury":
        return status_of(label, th)
    if which == "pair_only" and label.get("pair"):
        return status_of(decide(label["pair"], {"surface": "water" if label["pair"].get("answer") == "yes" else "dry_pavement",
                                                "road_visible": "yes", "confidence": label["pair"].get("confidence", 0.6)}), th)
    if which == "photo_only" and label.get("surface"):
        return status_of(decide(None, label["surface"]), th)
    return "uncertain"


# ------------------------------------------------------------------ controls

async def _controls(s: Settings, sample: list[dict]) -> dict:
    vlm = VLM(s)
    th = s.get("thresholds.road", {})
    ev = s.event
    out = {"n": len(sample)}
    if not s.paired:
        return out
    pres = {f["properties"]["id"]: chip_files(s, f["properties"]["id"])["pre"].read_bytes() for f in sample}
    ids = list(pres)
    rnd = random.Random(7)
    shuffled = ids[:]
    rnd.shuffle(shuffled)

    async def one(f, kind):
        p = f["properties"]
        files = chip_files(s, p["id"])
        date = p.get("after_date") or ""
        if kind == "same_2016":
            pre = pres[p["id"]]
            pair, _ = await vlm.label([pre, pre], "roads_v2", before_date=ev.get("before_label"), after_date=date)
            surf, _ = await vlm.label([pre], "surface_v1", after_date=date)
        elif kind == "shuffled":
            other = shuffled[ids.index(p["id"])]
            post = files["post"].read_bytes()
            pair, _ = await vlm.label([pres[other], post], "roads_v2", before_date=ev.get("before_label"), after_date=date)
            surf, _ = await vlm.label([post], "surface_v1", after_date=date)
        else:  # swapped order
            post = files["post"].read_bytes()
            pair, _ = await vlm.label([post, pres[p["id"]]], "roads_v2", before_date=ev.get("before_label"), after_date=date)
            surf, _ = await vlm.label([post], "surface_v1", after_date=date)
        return p["id"], status_of({**decide(pair, surf)}, th), pair.get("answer")

    for kind in ("same_2016", "shuffled", "swapped"):
        res = await asyncio.gather(*(one(f, kind) for f in sample))
        orig = {f["properties"]["id"]: f["properties"]["status"] for f in sample}
        st = Counter(r[1] for r in res)
        out[kind] = {"status_counts": dict(st), "pair_said_new_water": sum(r[2] == "yes" for r in res),
                     "same_status_as_real": sum(orig[i] == st_ for i, st_, _ in res)}
    return out


# ------------------------------------------------------------------ report

def run(areas: list[Settings], n: int = 60, log=print) -> dict:
    report = {}
    lines = ["# Evaluation", "", f"Generated {datetime.now(timezone.utc).isoformat(timespec='minutes')} by `python -m floodlens evaluate`.", "",
             "Read the controls and baselines before the model numbers. Hand labels come from the app's review buttons (`eval/labels/`).", ""]
    for s in areas:
        segs = read_geojson(s.derived / "road_segments.geojson")
        if not segs:
            continue
        th = s.get("thresholds.road", {})
        byid = {f["properties"]["id"]: f["properties"] for f in segs}
        labelled = [f for f in segs if f["properties"].get("status_source") in ("vlm", "human") and f["properties"].get("chip_url")]
        dist = Counter((f["properties"].get("vlm_status") or f["properties"]["status"]) for f in labelled)
        juror = Counter()
        for f in labelled:
            lab = f["properties"].get("label") or {}
            if lab.get("pair") and lab.get("surface"):
                juror[(lab["pair"].get("answer"), lab["surface"].get("surface"))] += 1
        r: dict = {"area": s.area_id, "labelled_by_model": len(labelled), "status_distribution": dict(dist),
                   "juror_agreement": {f"{a}|{b}": v for (a, b), v in juror.most_common(8)}}
        # hand labels
        hl = load_labels(s)
        truth = {}
        for sid, by in hl.items():
            vals = [v for v in by.values() if v in ("open", "blocked")]
            if vals and len(set(vals)) == 1:
                truth[sid] = vals[0]
        reviewers = sorted({rv for by in hl.values() for rv in by})
        if len(reviewers) >= 2:
            both = [(by[reviewers[0]], by[reviewers[1]]) for by in hl.values() if reviewers[0] in by and reviewers[1] in by]
            r["inter_annotator"] = {"reviewers": reviewers[:2], "n_both": len(both),
                                    "agreement": round(sum(a == b for a, b in both) / len(both), 3) if both else None,
                                    "kappa": kappa([a for a, _ in both], [b for _, b in both])}
        if truth:
            fema = tile_fema(s)
            rows = {}
            for name, pred in (
                ("FEMA zone rule (baseline)", lambda p: "blocked" if fema.get(p.get("tile_id") or "", "") == "T" else "open"),
                ("Colour threshold (baseline)", lambda p: baseline_colour(s, p["id"])),
                ("8B pair question only", lambda p: from_label(p.get("label"), "pair_only", th)),
                ("8B photo question only", lambda p: from_label(p.get("label"), "photo_only", th)),
                ("8B jury (shipped)", lambda p: from_label(p.get("label"), "jury", th)),
            ):
                pairs = [(t, pred(byid[sid])) for sid, t in truth.items() if sid in byid and byid[sid].get("chip_url")]
                rows[name] = confusion(pairs)
            r["vs_hand_labels"] = rows
        # controls
        cands = [f for f in labelled if f["properties"].get("status_source") == "vlm"]
        random.Random(42).shuffle(cands)
        if cands and s.paired and not s.vlm_mock:
            log(f"[{s.area_id}] running controls on {min(n, len(cands))} roads")
            r["controls"] = asyncio.run(_controls(s, cands[:n]))
        report[s.area_id] = r
        lines += _md(r)
    EVAL.mkdir(exist_ok=True)
    (EVAL / "results.json").write_text(json.dumps(report, indent=1))
    (ROOT / "docs" / "EVALUATION.md").write_text("\n".join(lines) + "\n")
    log("wrote docs/EVALUATION.md and eval/results.json")
    return report


def _md(r: dict) -> list[str]:
    L = [f"## {r['area']}", "", f"Road pieces judged by the model: {r['labelled_by_model']}. Status: {r['status_distribution']}.", ""]
    if "controls" in r:
        c = r["controls"]
        L += ["### Controls (no hand labels needed)", "", "| Control | What should happen | Result |", "|---|---|---|"]
        if "same_2016" in c:
            L.append(f"| 2016 vs 2016 ({c['n']} roads) | almost no new water | pair question said new water on {c['same_2016']['pair_said_new_water']}; statuses {c['same_2016']['status_counts']} |")
            L.append(f"| Shuffled before photo | pair answers change | pair said new water on {c['shuffled']['pair_said_new_water']}; same status as real run {c['shuffled']['same_status_as_real']} of {c['n']} |")
            L.append(f"| After then before (swapped) | flooded roads flip to no new water | pair said new water on {c['swapped']['pair_said_new_water']}; same status {c['swapped']['same_status_as_real']} of {c['n']} |")
        L.append("")
    if "inter_annotator" in r:
        ia = r["inter_annotator"]
        L += [f"Inter-annotator: {ia['reviewers']} agree on {ia['agreement']} of {ia['n_both']} roads (Cohen's kappa {ia['kappa']}).", ""]
    if "vs_hand_labels" in r:
        L += ["### Against hand labels", "", "| Method | n | Recall (flooded) | Precision (flooded) | Accuracy (decided) | Abstained |", "|---|---|---|---|---|---|"]
        for k, v in r["vs_hand_labels"].items():
            L.append(f"| {k} | {v['n']} | {v['recall_flooded']} | {v['precision_flooded']} | {v['accuracy_decided']} | {v['abstention_rate']} |")
        L.append("")
    else:
        L += ["No hand labels yet. Label roads in the app (Check roads tab) to get accuracy numbers.", ""]
    return L
