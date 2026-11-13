"""Async VLM client for an OpenAI-compatible endpoint, with cache, retries and validation."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path

from openai import AsyncOpenAI, BadRequestError
from pydantic import ValidationError

from floodlens.config import Settings
from floodlens.vlm import prompts
from floodlens.vlm.mock import mock_label


class VLM:
    def __init__(self, s: Settings):
        self.s = s
        self.cache_dir = s.cache / "vlm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(max(1, s.vlm_concurrency))
        self.use_schema = True
        self.client = None
        self.last_ok: float | None = None
        if not s.vlm_mock:
            if not s.vlm_base_url:
                raise SystemExit("VLM_BASE_URL is not set. Copy .env.example to .env, or set VLM_MOCK=1.")
            self.client = AsyncOpenAI(base_url=s.vlm_base_url, api_key=s.vlm_api_key, timeout=s.vlm_timeout_s)

    def _key(self, images: list[bytes], prompt_text: str) -> str:
        h = hashlib.sha256()
        for b in images:
            h.update(hashlib.sha256(b).digest())
        h.update(prompt_text.encode())
        h.update(self.s.vlm_model.encode())
        return h.hexdigest()

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = text.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError("no JSON object in reply")
        return json.loads(m.group(0))

    async def _call(self, images: list[bytes], text: str, prompt_id: str) -> str:
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
                   for b in images]
        content.append({"type": "text", "text": text})
        kw = dict(model=self.s.vlm_model, temperature=0, max_tokens=400,
                  messages=[{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": content}],
                  extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}})
        if self.use_schema:
            try:
                r = await self.client.chat.completions.create(
                    response_format={"type": "json_schema", "json_schema": prompts.json_schema(prompt_id)}, **kw)
                return r.choices[0].message.content or ""
            except BadRequestError:
                self.use_schema = False   # server does not support structured output
        r = await self.client.chat.completions.create(**kw)
        return r.choices[0].message.content or ""

    async def label(self, images: list[bytes], prompt_id: str, **fmt) -> tuple[dict, dict]:
        """Returns (label dict, meta). Never raises for model output problems."""
        text = prompts.render(prompt_id, **fmt)
        key = self._key(images, text)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text()), {"cache_hit": True, "latency_s": 0.0}
        if self.s.vlm_mock:
            return mock_label(images), {"cache_hit": False, "latency_s": 0.0, "mock": True}
        model = prompts.PROMPTS[prompt_id]["model"]
        t0 = time.time()
        async with self.sem:
            raw, err = "", None
            for attempt in range(3):
                try:
                    raw = await self._call(images, text, prompt_id)
                    break
                except Exception as e:  # noqa: BLE001  network / server errors
                    err = e
                    await asyncio.sleep(2 ** attempt)
            else:
                return ({"passable": "uncertain", "confidence": 0.0, "reason": f"model_unreachable: {type(err).__name__}"},
                        {"cache_hit": False, "latency_s": time.time() - t0, "error": str(err)})
            label = None
            for attempt in range(2):
                try:
                    label = model.model_validate(self._extract_json(raw)).model_dump()
                    break
                except (ValueError, ValidationError) as e:
                    if attempt == 0:
                        raw = await self._call(images, text + f"\n\nYour last reply was invalid ({str(e)[:200]}). Return valid JSON only.", prompt_id)
            if label is None:
                return ({"passable": "uncertain", "confidence": 0.0, "reason": "model_output_invalid", "raw": raw[:300]},
                        {"cache_hit": False, "latency_s": time.time() - t0})
        post = prompts.PROMPTS[prompt_id].get("post")
        if post:
            label = post(label)
        self.last_ok = time.time()
        path.write_text(json.dumps(label))
        return label, {"cache_hit": False, "latency_s": time.time() - t0}

    async def ask(self, images: list[bytes], question: str, max_tokens: int = 300) -> str:
        """Free-form question (vlm-check, chat)."""
        if self.s.vlm_mock:
            return "MOCK answer"
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
                   for b in images] + [{"type": "text", "text": question}]
        r = await self.client.chat.completions.create(
            model=self.s.vlm_model, temperature=0, max_tokens=max_tokens,
            messages=[{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": content}])
        self.last_ok = time.time()
        return r.choices[0].message.content or ""


def status_from_label(label: dict, th: dict) -> str:
    p, c = label.get("passable"), float(label.get("confidence") or 0)
    if p == "no" and c >= th.get("blocked_min_conf", 0.6):
        return "blocked"
    if p == "yes" and c >= th.get("open_min_conf", 0.6) and label.get("visibility") != "occluded":
        return "open"
    return "uncertain"


def jpeg_bytes(path: Path) -> bytes:
    return path.read_bytes()
