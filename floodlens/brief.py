"""Tier 3c: the chat model writes a cited dispatch brief and a short resident alert (English or Spanish).

The model only sees structured facts. Every number it writes is checked against those
facts; a number the facts do not contain is returned struck through, so a hallucinated
figure can never look trustworthy on screen.
"""
from __future__ import annotations

import html
import json
import re

from openai import OpenAI

from floodlens.config import Settings
from floodlens.geo import read_geojson

LANG = {"en": "English", "es": "Spanish (plain Latin American Spanish, usted form)"}


def _facts(s: Settings, zone_id: str | None, mode: str) -> dict:
    zones = read_geojson(s.derived / f"cutoff_zones_{mode}.geojson")
    segs = {f["properties"]["id"]: f["properties"] for f in read_geojson(s.derived / "road_segments.geojson")}
    summ = json.loads((s.derived / "summary.json").read_text())
    info = s.area_info
    facts: dict = {"area": info["name"], "event": info["event"], "photo_dates": info["after_dates"],
                   "people_cut_off_total": summ["people_cut_off"][mode], "neighbourhoods_cut_off": len(zones),
                   "road_pieces_underwater": summ["segments"].get("blocked", 0),
                   "facilities_cut_off": summ.get("facilities_cut_off", [])}
    z = next((z for z in zones if z["properties"]["id"] == zone_id), zones[0] if zones else None)
    if z:
        p = z["properties"]
        blocked = []
        for sid in p.get("nearest_blocked_segment_ids", [])[:4]:
            sp = segs.get(sid, {})
            blocked.append({"id": sid, "road": sp.get("name") or "unnamed road", "length_m": sp.get("length_m"),
                            "evidence": (sp.get("label") or {}).get("reason"), "photo_tile": sp.get("tile_id")})
        facts["zone"] = {"id": p["id"], "rank": p["priority_rank"], "near": p["label"], "people": round(p["est_pop"]),
                         "buildings": p["building_count"], "vulnerability": p.get("vulnerability"),
                         "blocked_roads_shortest_first": blocked}
    return facts


PROMPT = """You write for a flood dispatcher. Use ONLY the facts in the JSON below. Do not add any number, place, time or claim that is not in it.
Write in {lang}.

Part 1, "BRIEF": at most 90 words. Say which neighbourhood is cut off, roughly how many people, why it ranks where it does, and which flooded road to clear or avoid first. Put the id of each road you mention in square brackets, like [id].
Part 2, "ALERT": a text message for residents of that neighbourhood, at most 300 characters, plain words, no emoji. Tell them to stay put and call for rescue if they cannot leave safely, and end with "Turn around, don't drown." (translated if not English).

Return JSON only: {{"brief": "...", "alert": "..."}}

FACTS:
{facts}"""


def _numbers(obj) -> set[str]:
    txt = json.dumps(obj)
    out = set()
    for n in re.findall(r"\d[\d,]*\.?\d*", txt):
        n = n.replace(",", "").rstrip(".")
        out.add(n)
        try:
            out.add(str(round(float(n))))
        except ValueError:
            pass
    return out


def check_numbers(text: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Escape text, wrap numbers not in `allowed` in <del>, link [segment ids]."""
    bad: list[str] = []

    def num(m):
        raw = m.group(0)
        n = raw.replace(",", "").rstrip(".")
        ok = n in allowed or (n.replace(".", "", 1).isdigit() and str(round(float(n))) in allowed)
        if not ok:
            bad.append(raw)
            return f'<del title="not in the data">{raw}</del>'
        return raw

    safe = html.escape(text)
    ids = re.findall(r"\[([0-9_]+)\]", safe)
    safe = re.sub(r"\[([0-9_]+)\]", lambda m: f"\x00{m.group(1)}\x00", safe)
    safe = re.sub(r"(?<![\w\x00])\d[\d,]*\.?\d*", num, safe)
    for sid in ids:
        safe = safe.replace(f"\x00{sid}\x00", f'<a class="lnk" data-seg="{sid}">[road]</a>')
    return safe, bad


def make_brief(s: Settings, zone_id: str | None, mode: str = "permissive", lang: str = "en") -> dict:
    facts = _facts(s, zone_id, mode)
    if s.vlm_mock:
        out = {"brief": f"MOCK brief for {facts.get('zone', {}).get('near', 'area')}.", "alert": "MOCK alert. Turn around, don't drown."}
    else:
        client = OpenAI(base_url=s.vlm_base_url, api_key=s.vlm_api_key, timeout=s.vlm_timeout_s)
        r = client.chat.completions.create(
            model=s.vlm_model, temperature=0.2, max_tokens=500,
            messages=[{"role": "system", "content": "You are careful and concise. You never invent facts."},
                      {"role": "user", "content": PROMPT.format(lang=LANG[lang], facts=json.dumps(facts, ensure_ascii=False))}])
        raw = r.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, flags=re.S)
        try:
            out = json.loads(m.group(0)) if m else {"brief": raw, "alert": ""}
        except json.JSONDecodeError:
            out = {"brief": raw, "alert": ""}
    allowed = _numbers(facts)
    brief_html, bad1 = check_numbers(out.get("brief", ""), allowed)
    alert_html, bad2 = check_numbers(out.get("alert", ""), allowed)
    return {"lang": lang, "zone_id": (facts.get("zone") or {}).get("id"), "brief_html": brief_html, "alert_html": alert_html,
            "alert_chars": len(out.get("alert", "")), "unverified_numbers": bad1 + bad2, "facts": facts,
            "model": s.vlm_model, "label": "Machine-written draft. Check before sending."}
