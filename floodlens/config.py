"""Load config.yaml and .env into one settings object."""
from __future__ import annotations

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

    # common folders
    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def derived(self) -> Path:
        return self.data / "derived"

    @property
    def chips(self) -> Path:
        return self.data / "chips"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def dataset_dir(self) -> Path:
        return self.path(self["dataset"]["dir"])

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        w, s, e, n = (float(v) for v in self["aoi"]["bbox"])
        if not (w < e and s < n):
            raise ValueError(f"aoi.bbox must be [west, south, east, north], got {self['aoi']['bbox']}")
        return w, s, e, n


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(os.environ.get("FLOODLENS_CONFIG", ROOT / "config.yaml"))
    if not cfg_path.exists():
        raise SystemExit(f"Missing config file: {cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text())
    for key in ("event", "dataset", "aoi", "imagery", "roads", "vlm"):
        if key not in raw:
            raise SystemExit(f"config.yaml is missing the '{key}' section")
    s = Settings(raw=raw)
    s.vlm_mock = os.environ.get("VLM_MOCK", "0").strip() in ("1", "true", "yes")
    s.vlm_base_url = os.environ.get("VLM_BASE_URL", "").strip()
    s.vlm_model = os.environ.get("VLM_MODEL", raw["vlm"].get("model", "")).strip()
    s.vlm_api_key = os.environ.get("VLM_API_KEY", "EMPTY").strip()
    s.vlm_concurrency = int(os.environ.get("VLM_CONCURRENCY", "4"))
    s.vlm_timeout_s = float(os.environ.get("VLM_TIMEOUT_S", "120"))
    if not s.vlm_mock and not s.vlm_base_url:
        # Not fatal here: commands that need the VLM check again and explain.
        pass
    return s
