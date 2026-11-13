# Decisions

Newest first. Each entry says what we chose and why.

## 2026-11-13 (afternoon)

- **All six areas.** Areas are defined by `locality` in `config.yaml`. Map imagery is served as an XYZ tile pyramid cut from the organizer tiles (each image is exactly one zoom-16/17 web-map tile), so the browser loads only what is on screen.
- **Roads only inside the photographed area.** OSM is fetched for the tile footprints plus 150 m. Exits are the nodes outside the photos, so missing tiles no longer create fake islands.
- **Two-question jury replaces the single question.** Pair question (roads_v2) plus an after-photo surface question (surface_v1). Any "water" = blocked; open needs both to see a dry road. Reason: one Highway 6 piece in brown water was called dry by the pair question alone. Hill Country has no before photo, so it uses surface_v1 alone.
- **building_v1 failed, building_v2 asks only what a photo can show.** v1 offered depth classes and the 8B model said "water around, not touching the walls" for Bevil Oaks homes standing in deep water. v2 asks for percent of the crop covered by water before and after, plus whether trees hide the ground. Control (2016 vs 2016): 0% water.
- **Damage ratios from real claims, as ranges.** OpenFEMA NFIP v3 claims for the event give building damage / building value by exterior water depth (Bevil Oaks county: 7,190 claims; medians 36%, 45%, 69% for under 1 ft, 1-3 ft, over 3 ft). Claims exist only where damage happened, so "partial" water ranges from 0. Homes hidden by trees are left out unless their road was judged underwater (second, independent signal, wider range).
- **Tract vintages.** ACS 2016 values and 2017 NFIP claims use 2010-era tract IDs; SVI 2022 uses 2020-era tracts. Each is joined on its own TIGER vintage.
- **Census API needs a key now.** We use the ACS summary file download instead (no key).
- **Overpass is the bottleneck.** The public servers allow about two queries at once per user. Relief candidates, farmland and food stores now come from one extra query per area, saved as a snapshot, with retries across three mirrors.
- **NWS archive.** The IEM endpoint returns one phenomenon per request; we fetch flash flood and flood warnings separately (areal flood timed out and is skipped when unavailable).
- **MCP SDK 2.x.** `FastMCP` is now `MCPServer`. The web backend calls `floodlens.sources` directly; the MCP server wraps the same functions, so both return identical envelopes.
- **Offline demo.** Briefs are saved per zone and language and served when the model is unreachable. `offline-check` lists anything missing. We do not bulk-download OSM street tiles (their policy forbids it); the app hides the basemap when it cannot load.

## 2026-11-13 (morning)

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
