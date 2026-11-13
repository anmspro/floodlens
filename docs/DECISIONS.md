# Decisions

Newest first. Each entry says what we chose and why.

## 2026-11-13

- **Prompt `roads_v1` failed, replaced by `roads_v2`.** On the 370 labelled segments, v1 returned "full water, not passable, confidence 0.95" for every one, dry northern streets included. A neutral question ("Is the road surface in image 2 covered by water that is not there in image 1?") separated dry and flooded roads correctly on a 7-road spot test, and the 2016-vs-2016 control answered "no" 7 of 7 times. Cause: v1 framed the scene as "during Hurricane Harvey flooding" and listed flood cues, which primed the 8B model. Strict JSON schema mode was not the cause. v2 result on the 370 segments: 100 open, 270 blocked. No accuracy number yet (needs hand labels, Tier 1 acceptance).
- **Known false negative.** A North Highway 6 segment surrounded by brown water was labelled "no new water, clear, 0.95". Next step: a two-question jury with the same model (pair question + after-only "DRY/WATER/TREES" question) where any "water" makes the road blocked. Evaluate before switching.
- **Model confidence is not calibrated.** The 8B model returns 0.95 almost always. Do not show it as a probability in the pitch.
- **A zone counts as cut off only if a flooded road borders it.** Areas enclosed only by roads in missing-photo tiles are reported separately as "not counted" (65 such areas in strict mode).
- **Default view is "Best estimate" (permissive).** "Confirmed only" (strict) is one click away.
- **Imagery path.** Organizer PNG + WGS84 bounds -> GeoTIFF EPSG:3857 -> mosaic. No TiTiler, tiles are MapLibre image sources.
- **No Node build.** Plain JS + vendored MapLibre 4.7.1, so the app runs from `python -m floodlens serve` alone.
- **Environment from conda-forge.** pypi.org is blocked on the event Wi-Fi (TLS reset). conda-forge, Hugging Face, GitHub, npm/unpkg, OSM and weather.gov are reachable.
- **Model endpoint.** Organizer GPUStack server, `qwen3-vl-8b-instruct`. Our key gets 403 on `qwen3-vl-32b-instruct` and `qwen3.5-122b-a10b`.
- **Area of interest.** 5 x 5 zoom-16 tiles in north Addicks, 16 of 25 tiles exist in the organizer set (9 missing). Mix of dry streets and deep water on 30 Aug 2017. 690 segments, 370 with imagery.
- **`vlm-check` timing.** About 0.15-0.5 s per road chip pair with 4 concurrent requests (shared server, results vary).
