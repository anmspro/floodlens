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


PROMPTS = {
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
