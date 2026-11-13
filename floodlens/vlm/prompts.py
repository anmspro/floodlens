"""Versioned prompts and their JSON schemas. Change a prompt -> bump its version."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SYSTEM = "Use visible image evidence. Be concise and state uncertainty."


class RoadLabel(BaseModel):
    water_on_road: Literal["none", "partial", "full", "unknown"]
    water_new_since_before: Literal["yes", "no", "unknown"]
    debris_on_road: Literal["none", "some", "blocking", "unknown"]
    visibility: Literal["clear", "partly_occluded", "occluded"]
    passable: Literal["yes", "no", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


ROADS_V1 = """You are checking one road segment in two aerial photos of the same place in West Houston, Texas.
Image 1 (BEFORE) was taken in {before_date} in normal conditions.
Image 2 (AFTER) was taken on {after_date}, during Hurricane Harvey flooding.
The magenta line marks the same road segment in both images. Judge ONLY that segment, in Image 2.

Decide whether a normal passenger car could have driven along it when Image 2 was taken.
- Water that is also present in Image 1 (ponds, channels, reservoirs) is permanent, not flood.
- Floodwater can be brown, grey or green and can hide curbs, lane lines and the road edge.
- Report debris or damaged pavement if you see it.
- If trees, shadow or clouds hide the road surface, set visibility accordingly and use passable = "uncertain".
{context}
Return JSON only, with exactly these keys:
{{"water_on_road": "none|partial|full|unknown", "water_new_since_before": "yes|no|unknown",
 "debris_on_road": "none|some|blocking|unknown", "visibility": "clear|partly_occluded|occluded",
 "passable": "yes|no|uncertain", "confidence": 0.0-1.0, "reason": "one short sentence, max 200 characters"}}"""


class RoadLabelV2(BaseModel):
    answer: Literal["yes", "no", "unsure"]          # is the road under new water in image 2?
    water_on_road: Literal["none", "partial", "full", "unknown"]
    visibility: Literal["clear", "partly_occluded", "occluded"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


# roads_v1 primed the 8B model ("during Hurricane Harvey flooding" + a list of flood cues):
# it answered "full water, not passable, 0.95" for all 370 segments, dry ones included.
# roads_v2 asks one neutral question first. See docs/DECISIONS.md.
ROADS_V2 = """Image 1 is BEFORE ({before_date}), image 2 is AFTER ({after_date}), the same place seen from the air.
Look only at the road under the magenta line.
Is the road surface in image 2 covered by water that is not there in image 1?
If trees, shadow or the image quality hide the road surface, answer "unsure".
{context}
Return JSON only: {{"answer": "yes|no|unsure", "water_on_road": "none|partial|full|unknown", "visibility": "clear|partly_occluded|occluded", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


def _v2_post(label: dict) -> dict:
    """Map the v2 answer onto the plan's passable field (safety first: any new water = not passable)."""
    a = label.get("answer")
    label["passable"] = {"yes": "no", "no": "yes"}.get(a, "uncertain")
    if a == "no" and label.get("water_on_road") in ("partial", "full"):
        label["passable"] = "uncertain"           # self-contradiction -> ask a human
    return label


class SurfaceLabel(BaseModel):
    surface: Literal["dry_pavement", "water", "mud_or_debris", "trees", "shadow", "other"]
    road_visible: Literal["yes", "partly", "no"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


# After-only question. Used as the second juror in pair areas (catches flooded roads the
# pair question misses) and alone where no before photo exists (Hill Country 2025).
SURFACE_V1 = """This is an aerial photo taken on {after_date}.
Look only at the road under the magenta line.
What covers the road surface there?
{context}
Return JSON only: {{"surface": "dry_pavement|water|mud_or_debris|trees|shadow|other", "road_visible": "yes|partly|no", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


class BuildingLabel(BaseModel):
    water_reach: Literal["none", "around", "at_walls", "deep", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


BUILDING_V1 = """Image 1 is BEFORE ({before_date}), image 2 is AFTER ({after_date}), the same place seen from the air.
Look only at the building outlined in magenta.
In image 2, how far does water reach this building, compared with image 1?
- none: no new water near it
- around: new water in the yard, driveway or street next to it, not touching the walls
- at_walls: new water touching the walls
- deep: the building stands in deep water, well up the walls
- unknown: trees, shadow or image quality hide it
Return JSON only: {{"water_reach": "none|around|at_walls|deep|unknown", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


class BuildingLabelV2(BaseModel):
    water_percent_after: int = Field(ge=0, le=100)
    water_percent_before: int = Field(ge=0, le=100)
    view: Literal["clear", "trees", "shadow", "unclear"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


# building_v1 offered depth classes; the 8B model answered "around, not touching the walls"
# for homes standing in deep water. v2 asks only what an aerial photo can show.
BUILDING_V2 = """Image 1 is BEFORE ({before_date}), image 2 is AFTER ({after_date}), the same place seen from the air.
Look at the building outlined in magenta and the ground right around it.
About what percent of image 2 is covered by water? And of image 1?
Can you see the ground around the building, or is it hidden by trees or shadow?
Return JSON only: {{"water_percent_after": 0-100, "water_percent_before": 0-100, "view": "clear|trees|shadow|unclear", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


class BuildingAfterLabel(BaseModel):
    condition: Literal["intact", "debris_around", "damaged", "destroyed", "water_around", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


BUILDING_AFTER_V1 = """This aerial photo was taken on {after_date}, days after a river flood.
Look only at the building outlined in magenta. What is its condition?
- intact: looks normal
- debris_around: flood debris, mud or sand around it, building looks intact
- damaged: roof or walls visibly damaged, or the building is shifted
- destroyed: building gone or collapsed
- water_around: standing water around it
- unknown: hidden by trees or shadow
Return JSON only: {{"condition": "intact|debris_around|damaged|destroyed|water_around|unknown", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


class FieldLabel(BaseModel):
    answer: Literal["yes", "no", "unsure"]
    deposit: Literal["none", "sand_or_mud", "debris", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


FIELD_V1 = """{intro}
Look only at the area outlined in magenta (a field, pasture or orchard).
Is it covered by floodwater{compare}? Also say if you see a sand or mud deposit or flood debris on it.
Return JSON only: {{"answer": "yes|no|unsure", "deposit": "none|sand_or_mud|debris|unknown", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


class SiteLabel(BaseModel):
    usable: Literal["usable", "not_usable", "uncertain"]
    dry: Literal["yes", "no", "unsure"]
    mostly_empty: Literal["yes", "no", "unsure"]
    debris: Literal["none", "some", "a_lot", "unknown"]
    surface: Literal["paved", "grass", "dirt", "mixed", "unknown"]
    change: str = Field(max_length=200)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


SITE_V1 = """{intro}
Look only at the open area outlined in magenta (a parking lot, school ground, field or park).
Could trucks park there and people queue for a relief point (food, water, tents) when the last photo was taken?
It must be dry, mostly empty and free of debris.
Return JSON only: {{"usable": "usable|not_usable|uncertain", "dry": "yes|no|unsure", "mostly_empty": "yes|no|unsure",
 "debris": "none|some|a_lot|unknown", "surface": "paved|grass|dirt|mixed|unknown", "change": "what changed between the photos, or 'n/a'",
 "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


PROMPTS = {
    "building_v1": {"version": "building_v1", "template": BUILDING_V1, "model": BuildingLabel, "n_images": 2, "post": None},
    "building_v2": {"version": "building_v2", "template": BUILDING_V2, "model": BuildingLabelV2, "n_images": 2, "post": None},
    "building_after_v1": {"version": "building_after_v1", "template": BUILDING_AFTER_V1, "model": BuildingAfterLabel, "n_images": 1, "post": None},
    "field_v1": {"version": "field_v1", "template": FIELD_V1, "model": FieldLabel, "n_images": 2, "post": None},
    "site_v1": {"version": "site_v1", "template": SITE_V1, "model": SiteLabel, "n_images": 2, "post": None},
    "surface_v1": {"version": "surface_v1", "template": SURFACE_V1, "model": SurfaceLabel, "n_images": 1, "post": None},
    "roads_v1": {"version": "roads_v1", "template": ROADS_V1, "model": RoadLabel, "n_images": 2, "post": None},
    "roads_v2": {"version": "roads_v2", "template": ROADS_V2, "model": RoadLabelV2, "n_images": 2, "post": _v2_post},
}


def render(prompt_id: str, **kw) -> str:
    p = PROMPTS[prompt_id]
    ctx = kw.pop("context", None)
    kw["context"] = f"\nContext (secondary evidence, may be stale):\n{ctx}\nUse the images as primary evidence.\n" if ctx else ""
    return p["template"].format(**kw)


def json_schema(prompt_id: str) -> dict:
    model = PROMPTS[prompt_id]["model"]
    return {"name": prompt_id, "schema": model.model_json_schema(), "strict": True}
