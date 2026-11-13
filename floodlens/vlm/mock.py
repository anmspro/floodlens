"""Deterministic fake labels so UI and analysis work without the model server."""
from __future__ import annotations

import hashlib


def mock_label(images: list[bytes]) -> dict:
    h = int(hashlib.sha256(b"".join(images)).hexdigest(), 16) % 10
    if h <= 5:
        return {"water_on_road": "none", "water_new_since_before": "no", "debris_on_road": "none",
                "visibility": "clear", "passable": "yes", "confidence": 0.8, "reason": "MOCK: road looks dry"}
    if h <= 7:
        return {"water_on_road": "full", "water_new_since_before": "yes", "debris_on_road": "unknown",
                "visibility": "clear", "passable": "no", "confidence": 0.8, "reason": "MOCK: water covers road"}
    return {"water_on_road": "unknown", "water_new_since_before": "unknown", "debris_on_road": "unknown",
            "visibility": "partly_occluded", "passable": "uncertain", "confidence": 0.4, "reason": "MOCK: unclear"}
