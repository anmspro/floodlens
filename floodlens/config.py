"""Load config.yaml and .env into one settings object. `for_area()` scopes paths to one area."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path = ROOT
    vlm_base_url: str = ""
    vlm_model: str = ""
    vlm_api_key: str = ""
    vlm_concurrency: int = 4
    vlm_timeout_s: float = 120
    vlm_mock: bool = False
    area_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    # ---- areas ----
    @property
    def area_ids(self) -> list[str]:
        return list(self.raw["areas"])

    def for_area(self, area_id: str | None = None) -> "Settings":
        area_id = area_id or self.raw.get("default_area") or self.area_ids[0]
        if area_id not in self.raw["areas"]:
            raise SystemExit(f"Unknown area '{area_id}'. Choose from: {', '.join(self.area_ids)}")
        s = copy.copy(self)
        s.area_id = area_id
        return s

    @property
    def area(self) -> dict:
        if not self.area_id:
            raise RuntimeError("call for_area() first")
        return {"id": self.area_id, **self.raw["areas"][self.area_id]}

    @property
    def event(self) -> dict:
        return self.raw["events"][self.area["event"]]

    @property
    def paired(self) -> bool:
        return self.area.get("mode", "pair") == "pair"

    # ---- folders ----
    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def derived(self) -> Path:
        return self.data / "derived" / self.area_id if self.area_id else self.data / "derived"

    @property
    def chips(self) -> Path:
        return self.data / "chips" / self.area_id if self.area_id else self.data / "chips"

    @property
    def tiles(self) -> Path:
        return self.data / "tiles" / self.area_id

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def snapshots(self) -> Path:
        return self.data / "raw" / "snapshots"

    @property
    def dataset_dir(self) -> Path:
        return self.path(self["dataset"]["dir"])

    @property
    def area_info(self) -> dict:
        p = self.derived / "area.json"
        if not p.exists():
            raise SystemExit(f"Area '{self.area_id}' is not prepared. Run `python -m floodlens fetch-data --area {self.area_id}`.")
        return json.loads(p.read_text())

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return tuple(self.area_info["bbox"])  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(os.environ.get("FLOODLENS_CONFIG", ROOT / "config.yaml"))
    if not cfg_path.exists():
        raise SystemExit(f"Missing config file: {cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text())
    for key in ("dataset", "areas", "events", "roads", "vlm"):
        if key not in raw:
            raise SystemExit(f"config.yaml is missing the '{key}' section")
    s = Settings(raw=raw)
    s.vlm_mock = os.environ.get("VLM_MOCK", "0").strip() in ("1", "true", "yes")
    s.vlm_base_url = os.environ.get("VLM_BASE_URL", "").strip()
    s.vlm_model = os.environ.get("VLM_MODEL", raw["vlm"].get("model", "")).strip()
    s.vlm_api_key = os.environ.get("VLM_API_KEY", "EMPTY").strip()
    s.vlm_concurrency = int(os.environ.get("VLM_CONCURRENCY", "4"))
    s.vlm_timeout_s = float(os.environ.get("VLM_TIMEOUT_S", "120"))
    return s
