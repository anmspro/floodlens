# FloodLens — Implementation Plan

## Team amendments (agreed 13 Nov 2026, these override the sections below)

1. **Runs on laptops.** The GB10 was not reachable, so everything runs locally. The VLM is the organizer's hosted endpoint (`qwen3-vl-8b-instruct`, the only model our key can call). PyPI is blocked on the event Wi-Fi, so the environment comes from conda-forge (`environment.yml`). No Node build step: the frontend is plain JS + vendored MapLibre in `web/`.
2. **Imagery.** The organizer PNG tiles (WGS84 bounds, Web Mercator grid) are written to GeoTIFF in EPSG:3857 and mosaicked. Any GeoTIFF source also works (`imagery.source: geotiff`). No TiTiler: tiles are drawn as MapLibre image sources.
3. **Before and after pairs for roads from Tier 1.** Water already present in 2016 is permanent, not flood.
4. **Replay wording.** We judge the road "when Image 2 was taken", not "right now".
5. **Historical context for Harvey** (per-image gauge, rain and FEMA columns, archived NWS warnings). Live feeds only for a San Antonio mode.
6. **Evaluation before any number:** hand labels with agreement, FEMA-zone and colour baselines, 2016/2016 control, shuffled-pair control.
7. **Scope:** Tiers 0-3, 3b, 3c. Tiers 4-8 only if ahead.
8. **Area:** start with the 5 x 5 tile block in north Addicks (`config.yaml: aoi.bbox`).
9. **Package layout:** `floodlens/` at the repo root (run `python -m floodlens <command>`), not `backend/floodlens`.

Every CLI command logs to `ops/RUNLOG.md`. A failing command writes `ops/errors/<time>_<command>.md`; commit and push it so someone else can fix it.

Audience: a coding agent building this project. Read the whole document before writing code. When this plan and your own preferences disagree, follow the plan. When the plan is silent, pick the simplest option and record the choice in `docs/DECISIONS.md`.

## 0. What we are building

A post-flood triage dashboard for one area of interest (AOI). It:

1. Labels every drivable road segment as open / blocked / uncertain by asking a vision-language model (VLM) about small image chips cut from post-flood imagery.
2. Uses the labels to compute cut-off neighborhoods, safe routes, and facility reachability.
3. Pulls public context (weather alerts, stream gauges, elevation, census vulnerability) through MCP servers that keep working when the live APIs fail.
4. Optionally adds building damage, crowd-sourced photo verification, multilingual alerts, a resident text interface, contamination warnings, and a shelter-capacity view.

Everything is shown on one MapLibre dashboard with toggleable layers, a ranked action panel, and an evidence panel that shows the image chip and the VLM's reason for any clicked feature.

### Tiers

| Tier | Content | Required? |
|---|---|---|
| 0 | Foundations: repo, config, VLM client, data prep, base dashboard | Yes |
| 1 | Road passability labeling + road layer + evidence panel + review queue | Yes |
| 2 | Network analysis: cut-off zones, route out, facility reachability | Yes |
| 3 | MCP public-data layer with fallback, VLM context enrichment, vulnerability-weighted ranking, failure demo | Yes |
| 3b | Demo-grade interface (judged criterion "Interface and Usability"), see section 7b | Yes |
| 3c | Chat model in the main path: cited dispatch brief + Spanish alert (moved forward from Tier 6) | Yes |
| 4 | Building flooding from before/after imagery + **economic impact estimate** (homes, farmland), see section 8 | Yes (team request) |
| 4b | **Relief site recommender**: dry open areas checked by the VLM, filtered for safety and access, chosen to cover the most vulnerable need, see section 8b | Yes (team request) |
| 5 | Crowd-sourced photo verification with privacy handling | If time |
| 6 | Multilingual alerts + resident text interface | If time |
| 7 | Contamination (hazmat / wastewater) warnings | If time |
| 8 | Shelter capacity gap + exportable situation report | If time |

Rules:
- Finish a tier completely (acceptance checks pass, demo works) before starting the next.
- Each tier must leave the app in a demoable state. Never leave the main branch broken.
- Commit at the end of each tier with message `tier N: <summary>`.
- Do not change a data schema in section 3 without updating this file in the same commit.

## 1. Decisions the humans make before coding starts

The agent must not guess these. If a value is missing from `config.yaml` / `.env`, stop and ask.

| Item | Where | Example |
|---|---|---|
| AOI bounding box (WGS84) | `config.yaml: aoi.bbox` | `[-82.60, 35.55, -82.50, 35.62]` (west, south, east, north) |
| Event name and date | `config.yaml: event` | `"Example Flood, Town X", 2024-09-28` |
| Post-event imagery GeoTIFF(s) | `config.yaml: imagery.post` | `data/raw/imagery/post/*.tif` |
| Pre-event imagery GeoTIFF(s) (Tier 4) | `config.yaml: imagery.pre` | `data/raw/imagery/pre/*.tif` |
| VLM endpoint, model name, API key | `.env` | see section 2.3 |
| Shelter list | `data/manual/shelters.csv` | name, lat, lon, capacity, source |

Imagery guidance for the humans: post-event imagery needs roughly 0.5 m/pixel or better for roads to be judged. Sentinel-2 (10 m) is not usable for this. Good candidates: NOAA emergency response aerial imagery (post) and NAIP (pre), or an open-data commercial release that covers both dates. Keep the AOI to a town or a few neighborhoods (target: 300–1,500 road segments).

## 2. Stack and environment

### 2.1 Languages and libraries

Backend (Python 3.11, managed with `uv`):
- `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`
- `osmnx`, `networkx`, `geopandas`, `shapely>=2`, `pyproj`
- `rasterio`, `rio-cogeo`, `titiler.core` (mounted inside FastAPI for imagery tiles)
- `pillow`, `numpy`, `opencv-python-headless` (Tier 5)
- `httpx` (all outbound HTTP, async)
- `openai` (client for the OpenAI-compatible VLM endpoint)
- `mcp` (official Model Context Protocol Python SDK; Tier 3)
- `diskcache` (response caches)
- `pyyaml`, `python-dotenv`, `typer` (CLI), `pytest`, `pytest-asyncio`

Frontend:
- Vite + TypeScript, no UI framework.
- `maplibre-gl` (latest major), `@maplibre/maplibre-gl-compare` (Tier 4).
- Basemap: OpenFreeMap style URL or OSM raster tiles. Put the URL in `frontend/src/config.ts`.

### 2.2 Repository layout

```
floodlens/
  FLOODLENS_PLAN.md
  config.yaml
  .env.example
  pyproject.toml
  docs/DECISIONS.md
  data/
    raw/imagery/{pre,post}/        # human-provided GeoTIFFs
    raw/snapshots/                 # offline copies of public data (Tier 3)
    manual/shelters.csv
    derived/                       # pipeline outputs (GeoJSON, parquet)
    chips/{roads,buildings,photos}/
    cache/                         # diskcache dirs, VLM response cache
  backend/
    floodlens/
      __init__.py
      config.py                    # loads config.yaml + .env
      cli.py                       # typer CLI, all pipeline steps
      geo.py                       # CRS helpers, AOI, UTM selection
      imagery.py                   # COG conversion, chip extraction
      vlm/
        client.py                  # async VLM client, retries, cache
        prompts.py                 # versioned prompts + JSON schemas
        mock.py                    # deterministic fake labels
      sources/                     # data access with envelope + fallback
        envelope.py
        osm.py
        weather_water.py           # Tier 3
        elevation.py               # Tier 3
        vulnerability.py           # Tier 3
        flood_zones.py             # Tier 3 (optional)
        hazmat.py                  # Tier 7
        languages.py               # Tier 6
      pipeline/
        prepare.py                 # OSM fetch, segment split, facilities
        roads.py                   # chips + labeling for roads
        buildings.py               # Tier 4
        photos.py                  # Tier 5
      analysis/
        graph.py                   # build graph from labels
        cutoff.py
        routing.py
        facilities.py
        priority.py                # Tier 3 vulnerability ranking
        shelters.py                # Tier 8
      alerts/                      # Tier 6
      api/
        main.py                    # FastAPI app factory
        routes_layers.py
        routes_analysis.py
        routes_review.py
        routes_sources.py          # Tier 3
        routes_photos.py           # Tier 5
        routes_sms.py              # Tier 6
        routes_admin.py            # fault injection (Tier 3)
  mcp_servers/                     # Tier 3, each runnable on its own
    osm_server.py
    water_weather_server.py
    elevation_server.py
    vulnerability_server.py
    common.py
  frontend/
    index.html
    src/
      main.ts
      config.ts
      api.ts
      layers/*.ts                  # one file per layer
      panels/{layers,action,evidence,status,review,sms}.ts
      styles.css
  tests/
```

### 2.3 Environment variables (`.env`)

```
VLM_BASE_URL=http://<gb10-host>:8000/v1
VLM_MODEL=<exact model id served by vLLM>
VLM_API_KEY=EMPTY
VLM_CONCURRENCY=8
VLM_TIMEOUT_S=120
VLM_MOCK=0                 # 1 = use backend/floodlens/vlm/mock.py, no network
NWS_USER_AGENT=floodlens-hackathon (team-contact@example.com)
CENSUS_API_KEY=            # optional
USE_MCP=0                  # Tier 3 switches this to 1
```

The VLM is a vLLM server exposing the OpenAI-compatible chat completions API. Assume it was started with image input enabled and, for Tier 4, with at least 2 images allowed per prompt (`--limit-mm-per-prompt` set accordingly). The agent does not manage the GB10.

### 2.4 Coordinate systems

- All stored GeoJSON is EPSG:4326 (lon, lat).
- All distance and area math happens in the local UTM zone chosen from the AOI centroid (`geo.utm_crs_for(bbox)`). Never compute distances in degrees.
- Imagery is converted to Cloud-Optimized GeoTIFF (COG) once in Tier 0 and read with rasterio windows.

### 2.5 Processes during development and demo

| Process | Command | Port |
|---|---|---|
| Backend API (+ imagery tiles) | `uv run uvicorn floodlens.api.main:app --port 8080` | 8080 |
| Frontend | `npm run dev` in `frontend/` | 5173 |
| MCP servers (Tier 3) | `uv run python mcp_servers/<name>.py` | 9101–9104 |
| VLM (external) | on GB10 | 8000 |

## 3. Data contracts

All derived layers live in `data/derived/` as GeoJSON FeatureCollections. Every feature has a stable string `id`. The frontend only reads these through the API.

### 3.1 `road_segments.geojson` (LineString)

| Property | Type | Notes |
|---|---|---|
| id | str | `{u}_{v}_{key}_{i}` from the OSMnx edge plus piece index |
| edge_id | str | `{u}_{v}_{key}` |
| name | str \| null | OSM `name`, first value if list |
| highway | str | OSM `highway` |
| length_m | float | |
| status | enum | `open`, `blocked`, `uncertain`, `no_imagery`, `pending` |
| status_source | enum | `vlm`, `photo`, `human`, `none` |
| confidence | float \| null | 0–1 |
| label | object \| null | full VLM JSON (section 5.2) |
| chip_url | str \| null | `/chips/roads/<id>.png` |
| prompt_version | str \| null | e.g. `roads_v1` |
| context | object \| null | Tier 3 context passed to the VLM |
| updated_at | ISO8601 str | |

### 3.2 `road_edges.geojson` (LineString, one per OSMnx edge)

`id` (= edge_id), `name`, `length_m`, `status` (aggregated, section 6.4), `segment_ids` (list).

### 3.3 `facilities.geojson` (Point)

| Property | Type | Notes |
|---|---|---|
| id | str | `osm:<type>/<id>` or `manual:<n>` |
| name | str | |
| kind | enum | `hospital`, `clinic`, `nursing_home`, `dialysis`, `fire_station`, `police`, `school`, `shelter` |
| source | str | `osm`, `shelters.csv`, `hifld_archive` |
| capacity | int \| null | shelters only |
| node_id | int | nearest graph node |
| reachable | bool \| null | Tier 2 |
| reachable_from_outside | bool \| null | Tier 2 |
| site_status | object \| null | Tier 4 VLM label for the site |

### 3.4 `buildings.geojson` (Polygon)

`id` (`osm:way/<id>`), `building` (OSM tag), `is_residential` (bool, section 6.6), `node_id`, `tract_geoid` (Tier 3), `est_pop` (float), `zone_id` (str \| null), `damage` (object \| null, Tier 4).

### 3.5 `cutoff_zones.geojson` (Polygon/MultiPolygon)

| Property | Type | Notes |
|---|---|---|
| id | str | `Z1`, `Z2`, … ordered by `priority_rank` |
| label | str | e.g. "near Elm St" |
| building_count | int | |
| est_pop | float | |
| vulnerability | object \| null | Tier 3: `svi`, `age65`, `noveh`, `disabl`, `limeng` |
| priority_score | float | Tier 2: `est_pop`; Tier 3: section 8.6 |
| priority_rank | int | 1 = highest |
| mode | enum | `strict` or `permissive` |
| nearest_blocked_segment_ids | list[str] | blocked segments on the zone boundary, for "open this road first" |

### 3.6 API envelope for source data (Tier 0 onward)

Every function in `backend/floodlens/sources/` and every MCP tool returns:

```json
{
  "data": { },
  "meta": {
    "source": "nws_alerts",
    "status": "live | cached | snapshot | unavailable",
    "fetched_at": "2026-11-14T15:02:11Z",
    "age_s": 0,
    "error": null
  }
}
```

## 4. Tier 0 — Foundations

Goal: running backend and frontend, data prepared for the AOI, VLM reachable (or mocked).

### 4.1 Config and CLI
1. Implement `config.py` loading `config.yaml` and `.env` into one pydantic settings object. Fail fast with a clear message if a required item from section 1 is missing.
2. Implement `cli.py` (typer) with subcommands that later tiers fill in:
   `prepare`, `cog`, `chips-roads`, `label-roads`, `analyze`, `fetch-snapshots`, `label-buildings`, `all`.
   Each subcommand is idempotent and skips work whose outputs exist, unless `--force` is given.

### 4.2 Imagery
1. `floodlens cog`: convert every input GeoTIFF to COG in `data/derived/imagery/{pre,post}/` using rio-cogeo. If several tiles exist, build a VRT mosaic per epoch (`post.vrt`, `pre.vrt`).
2. `imagery.coverage_polygon(epoch)`: polygon of valid (non-nodata) pixels, in EPSG:4326, saved as `data/derived/coverage_{epoch}.geojson`.
3. Mount `titiler.core` `TilerFactory` in FastAPI under `/tiles/{epoch}` so the frontend can show `pre` and `post` as raster layers. No separate TiTiler container.

### 4.3 OSM preparation (`floodlens prepare`)
1. Fetch the drive network with OSMnx for the AOI bbox (`network_type="drive"`, `simplify=True`, `truncate_by_edge=True`). Save as `data/derived/graph.graphml`.
2. Project to UTM. Split every edge longer than 120 m into pieces of at most 100 m (equal length). Each piece is a road segment (section 3.1) with `status="pending"`.
3. Fetch buildings (`building=*`) and facilities using these OSM tags:
   - `amenity=hospital` → hospital
   - `amenity=clinic`, `amenity=doctors` → clinic
   - `amenity=social_facility` + `social_facility=nursing_home`, or `healthcare=nursing_home` → nursing_home
   - `healthcare=dialysis` or `healthcare:speciality=dialysis` → dialysis
   - `amenity=fire_station` → fire_station
   - `amenity=police` → police
   - `amenity=school` → school
   - Do **not** use `amenity=shelter` (in OSM that is mostly bus and picnic shelters).
4. Load `data/manual/shelters.csv` into facilities with `kind=shelter`. If the file is empty, mark every school as `kind=shelter` with `source="assumed_school"` and log a warning.
5. Compute `node_id` for every building centroid and facility (nearest graph node, in UTM).
6. Write `road_segments.geojson`, `road_edges.geojson`, `buildings.geojson`, `facilities.geojson`.
7. Wrap the OSM fetch in `sources/osm.py` using the envelope (section 3.6). On failure, load the last successful result from `data/raw/snapshots/osm/`. After every successful fetch, write a snapshot there.

### 4.4 VLM client (`vlm/client.py`)
1. Async function `label_image(images: list[bytes], prompt_id: str, context: str | None) -> (dict, meta)`.
2. Send a chat completion with images as base64 `data:image/png;base64,...` in `image_url` content parts, the prompt text from `prompts.py`, `temperature=0`, `max_tokens=400`.
3. Request structured JSON with `response_format={"type": "json_schema", "json_schema": {...}}`. If the server rejects that parameter, fall back to plain text and extract the first JSON object from the reply. Always validate with the pydantic model for that prompt. On validation failure, retry once with the error appended to the prompt. If that also fails, return `status="uncertain"` with `reason="model_output_invalid"`.
4. Concurrency limit: `asyncio.Semaphore(VLM_CONCURRENCY)`. Timeout `VLM_TIMEOUT_S`. Up to 2 retries with exponential backoff on network errors.
5. Cache: key = sha256(image bytes + prompt_id + prompt_version + context + model). Store results in `data/cache/vlm/`. Cache hits never call the server.
6. `VLM_MOCK=1`: return deterministic labels from `mock.py` (hash of the image bytes mod 10: 0–5 open, 6–7 blocked, 8–9 uncertain) so frontend work can proceed without the GB10.
7. CLI `floodlens vlm-check`: sends one test image, prints latency and the parsed result. Also runs 20 chips and prints average seconds per chip. Record that number in `docs/DECISIONS.md`; it decides how many segments and buildings are labeled.

### 4.5 Backend API skeleton
- `GET /api/health` → `{ok, vlm_reachable, vlm_mock}`
- `GET /api/config` → event name, AOI bbox, available layers, tile URLs
- `GET /api/layers/{name}` → serves `data/derived/{name}.geojson` (whitelist names)
- Static mount `/chips` → `data/chips/`
- CORS enabled for `http://localhost:5173`.

### 4.6 Frontend skeleton
Layout (CSS grid):
```
┌──────────── header: event name | source status pills ────────────┐
│ left: layer toggles │        map        │ right: action panel     │
├──────────── bottom: evidence panel (collapsed until click) ──────┤
```
- Map centered on AOI bbox. Raster layer for `post` imagery (on), `pre` (off).
- Layer toggles generated from `/api/config`.
- Header status pills are placeholders until Tier 3.

### 4.7 Acceptance (Tier 0)
- `floodlens prepare && floodlens cog` succeed on the real AOI.
- `floodlens vlm-check` returns a valid parsed result (or mock result with `VLM_MOCK=1`).
- Opening the frontend shows the basemap, post imagery, and gray road segments.
- `pytest` passes (at least: config loading, segment splitting lengths ≤ 100 m, envelope fallback to snapshot when the fetch raises).

## 5. Tier 1 — Road passability

Goal: every road segment colored open/blocked/uncertain, each with a clickable chip and reason.

### 5.1 Chip extraction (`floodlens chips-roads`)
For each segment:
1. In UTM, take the segment's bounding box, buffer by 20 m, and expand to a square. Minimum side 80 m.
2. If the square does not intersect `coverage_post`, set `status="no_imagery"` and skip.
3. Read the post imagery window, resample to 512×512 px, RGB.
4. Save two PNGs: `data/chips/roads/<id>_raw.png` (no overlay) and `<id>.png` (segment centerline drawn in magenta `#FF00FF`, 3 px wide, 60% opacity). The overlay version is sent to the VLM; the evidence panel shows both.

### 5.2 Prompt `roads_v1`
System/user text (store in `prompts.py`, versioned):

```
You are assessing an aerial image taken after a flood.
The magenta line marks one road segment. Judge ONLY that road segment.
Decide whether a normal passenger car could drive along it right now.
Report water covering the road, debris, collapsed or washed-out pavement,
and whether the view is blocked by clouds, trees, or shadows.
If you cannot see the road surface clearly, say passable = "uncertain".
Return JSON only, matching the schema.
```

Schema (pydantic model `RoadLabel`):
```json
{
  "water_on_road": "none | partial | full | unknown",
  "debris_on_road": "none | some | blocking | unknown",
  "surface_damage": "none | damaged | washed_out | unknown",
  "visibility": "clear | partly_occluded | occluded",
  "passable": "yes | no | uncertain",
  "confidence": 0.0,
  "reason": "string, max 200 characters"
}
```

### 5.3 Status mapping
- `passable == "no"` and `confidence >= 0.6` → `blocked`
- `passable == "yes"` and `confidence >= 0.6` and `visibility != "occluded"` → `open`
- anything else → `uncertain`

Thresholds live in `config.yaml: thresholds.road`.

### 5.4 Labeling run (`floodlens label-roads`)
- Order of labeling: segments inside FEMA flood zones first if available (Tier 3), otherwise by ascending elevation if a DEM is available, otherwise by `highway` class (primary/secondary/tertiary before residential). Respect `config.yaml: limits.max_road_segments` (default 1500); unlabeled segments stay `pending` and are treated as `uncertain` in analysis.
- Write results back to `road_segments.geojson` after every 50 labels so a crash loses little work.
- Print progress: done / total, seconds per chip, counts per status.

### 5.5 Human review queue
- `GET /api/review` → uncertain segments sorted by `highway` class then length (longest first).
- `POST /api/review/{segment_id}` with `{"status": "open" | "blocked"}` → sets `status_source="human"`. Human labels are never overwritten by later VLM runs.
- Frontend: review panel tab listing items with chip thumbnail and two buttons.

### 5.6 Frontend
- Road layer colors: open `#2E9E44`, blocked `#D7263D`, uncertain `#F4A261`, no_imagery/pending `#9AA0A6`. Line width 3 (5 on hover).
- Click a segment → evidence panel shows: raw chip, overlay chip, status, confidence, the label JSON fields as a small table, reason, prompt_version, status_source.
- Legend with counts per status.

### 5.7 Acceptance (Tier 1)
- At least 300 segments labeled with the real VLM.
- Spot-check 20 random labels by eye; record agreement count in `docs/DECISIONS.md`. If agreement is below 14/20, adjust the prompt (new version `roads_v2`), not the thresholds, and rerun.
- Clicking any segment shows its evidence within 1 second.
- Approving a review item updates the map without page reload.

## 6. Tier 2 — Network analysis

Goal: cut-off zones, route out, facility reachability, recomputed whenever labels change.

### 6.1 Graph build (`analysis/graph.py`)
- Load `graph.graphml`, convert to undirected for connectivity, keep the directed version for routing.
- Edge status = aggregation of its segments (6.4).
- Two graph views:
  - `strict`: only `open` edges.
  - `permissive`: `open` + `uncertain` (incl. pending/no_imagery) edges.
- Edge weight for routing: `length_m × factor`, factor 1 for open, `config.yaml: routing.uncertain_penalty` (default 4.0) for uncertain. Blocked edges are removed.

### 6.2 Targets
Target nodes = union of:
- `node_id` of every facility with kind `hospital` or `shelter`.
- AOI exit nodes: graph nodes within 50 m (UTM) of the AOI bbox boundary.

### 6.3 Cut-off zones (`analysis/cutoff.py`)
For each mode (`strict`, `permissive`):
1. Connected components of the undirected view.
2. A component is cut off if it contains no target node.
3. Buildings whose `node_id` is in a cut-off component belong to that zone. Drop components with fewer than `config.yaml: cutoff.min_buildings` (default 3) buildings.
4. Zone polygon = union of building footprints buffered by 30 m, simplified with 5 m tolerance, converted to EPSG:4326.
5. `label` = most frequent non-null street name among edges in the component, as "near <name>"; fallback "zone near <lat>, <lon>".
6. `est_pop` (Tier 2 version) = number of residential buildings × `config.yaml: population.persons_per_building` (default 2.5). Tier 3 replaces this.
7. `nearest_blocked_segment_ids` = blocked segments with one endpoint node in the component, sorted by length ascending (short blockages first, since they are quicker to clear).
8. Rank by `priority_score` (= `est_pop` in Tier 2), descending.
Write `cutoff_zones_strict.geojson` and `cutoff_zones_permissive.geojson`.

### 6.4 Edge status aggregation
- Any segment `blocked` → edge `blocked`.
- Else any segment `uncertain`/`pending`/`no_imagery` → edge `uncertain`.
- Else `open`.

### 6.5 Routing (`analysis/routing.py`)
- `POST /api/route` body `{lat, lon, mode: "strict" | "permissive", target_kinds: ["shelter","hospital","exit"]}`.
- Snap to the nearest node in the chosen view. Run multi-source Dijkstra from all targets of the requested kinds (reverse graph) to find the nearest one.
- Response: GeoJSON LineString of the route, `target` (facility or exit), `length_m`, `uncertain_segment_ids` used, and turn-by-turn text (street name changes, "continue on X for N m"). If no route exists, return `{"route": null, "reason": "no reachable target", "zone_id": ...}`.

### 6.6 Residential buildings
`is_residential` = OSM `building` in {`house`, `residential`, `apartments`, `detached`, `semidetached_house`, `terrace`, `bungalow`, `dormitory`, `yes`}. `yes` counts as residential unless the building contains or is within 10 m of a non-residential facility point.

### 6.7 Facility reachability (`analysis/facilities.py`)
For each facility, in `strict` mode:
- `reachable_from_outside` = its node is in a component containing at least one exit node.
- `reachable` = same as above for shelters/hospitals; for other kinds, true if the component contains any hospital or shelter or exit.

### 6.8 Recompute
- `floodlens analyze` runs 6.1–6.7.
- `POST /api/analyze` runs the same in-process (must finish in under 5 s for 1,500 segments) and is called automatically after any label change (review, photo).
- The frontend refetches the analysis layers after any label-changing action.

### 6.9 Frontend
- Layer: cut-off zones (red fill 25% opacity, red outline). Toggle strict/permissive in the layer panel.
- Layer: facilities with icons per kind; unreachable ones get a red ring.
- Action panel, top to bottom:
  1. Cut-off zones: rank, label, est. people, building count, "show" button (fits map), "clear first" = first item of `nearest_blocked_segment_ids`.
  2. Facilities: unreachable hospitals, nursing homes, dialysis centers, shelters.
  3. "Route me out" button: next map click sets the start point; draws route in blue `#1E64C8`, shows turn list in the evidence panel.

### 6.10 Acceptance (Tier 2)
- Unit tests with a hand-built 8-node graph cover: a fully connected graph has no zones; removing one bridge edge creates exactly one zone; permissive mode with that edge uncertain has no zones.
- On the real AOI: marking a single segment blocked through the review panel changes the zone list without reload.
- Route never uses a blocked edge (assert in code and in a test).

## 7. Tier 3 — MCP public data, context, vulnerability, failure demo

Goal: public data flows through MCP servers that report freshness and survive outages; the VLM gets context; zones are ranked by vulnerability; the demo can show an outage live.

### 7.1 Snapshots first (`floodlens fetch-snapshots`)
Download once, before the event demo, into `data/raw/snapshots/`:
| Snapshot | Source | Notes |
|---|---|---|
| `svi/` | CDC/ATSDR SVI, tract level, state file with geometry (or CSV joined to Census TIGER tracts on FIPS) | Archived mirrors exist if the CDC site is down |
| `dem/aoi_dem.tif` | USGS 3DEP for the AOI bbox, 1/3 arc-second or finer | Sample locally; no per-point API calls in bulk |
| `nws_alerts.json` | api.weather.gov active alerts for AOI centroid | Requires User-Agent header |
| `gauges.json` | USGS stream gauges within 20 km of AOI with latest stage, plus NWS flood categories where available | Use the current USGS Water Data API; if the legacy NWIS endpoints still work, either is fine |
| `flood_zones.geojson` | FEMA National Flood Hazard Layer for AOI (optional) | |
| `osm/` | from Tier 0 | |

Verify column names against the SVI data dictionary before coding. Expected (2020/2022 SVI): `FIPS`, `E_TOTPOP`, `E_AGE65`, `E_NOVEH`, `E_DISABL`, `E_LIMENG`, `RPL_THEMES`. Treat `-999` as missing.

### 7.2 Source modules
Implement in `backend/floodlens/sources/`, each returning the envelope:
- `weather_water.get_alerts(lat, lon)`, `weather_water.nearby_gauges(lat, lon, radius_km)`
- `elevation.point(lat, lon)`, `elevation.relative(lat, lon, radius_m=300)` = point elevation minus median elevation within radius, from the local DEM
- `vulnerability.tract_for(lat, lon)`, `vulnerability.tracts_in(bbox)`
- `flood_zones.zone_for(lat, lon)` (optional)

Fallback order for live sources: live call (5 s timeout) → `diskcache` entry (TTL: alerts 60 s, gauges 10 min) → snapshot file → `status="unavailable"`. Local-file sources (DEM, SVI) always report `snapshot` with the file's age.

### 7.3 MCP servers (`mcp_servers/`)
- Use the official `mcp` Python SDK (`FastMCP`), streamable HTTP transport, one process per server:
  - `osm_server.py` (9101): `get_roads(bbox)`, `get_facilities(bbox)`, `health()`
  - `water_weather_server.py` (9102): `get_alerts(lat, lon)`, `nearby_gauges(lat, lon, radius_km)`, `health()`
  - `elevation_server.py` (9103): `elevation(lat, lon)`, `relative_elevation(lat, lon, radius_m)`, `health()`
  - `vulnerability_server.py` (9104): `tract_for(lat, lon)`, `tracts_in(bbox)`, `health()`
- Each tool is a thin wrapper around the matching `sources/` function and returns the envelope as JSON.
- `health()` returns `{source, status, last_success_at, last_error}` per underlying source.
- Each server has a tool `set_fault(mode: "none" | "down" | "slow" | "corrupt")`. `down` makes live calls raise, `slow` adds a 10 s delay (triggers timeout), `corrupt` returns malformed live data that must fail validation. In all three cases the server must fall back per 7.2.
- Check the current SDK README for exact import paths; record them in `docs/DECISIONS.md`.

### 7.4 Backend as MCP client
- `USE_MCP=1`: backend calls MCP tools through `mcp` client sessions (one per server, created at startup, reconnect on failure). `USE_MCP=0`: backend imports `sources/` directly. Both paths return identical envelopes; add a test asserting that.
- If an MCP server process itself is down, the backend falls back to calling `sources/` directly with `force_snapshot=True` and marks status `snapshot` with `error="mcp_server_unreachable"`.

### 7.5 Source status API and UI
- `GET /api/sources` → list of `{name, status, age_s, error}` for: osm, nws_alerts, usgs_gauges, elevation, svi, vlm.
- `vlm` status: `live` if the last call succeeded within 60 s, `cached` if only cache hits are possible, `unavailable` otherwise. When the VLM is down, new labeling jobs queue (status `pending`) and existing labels keep working.
- Header pills: green live, amber cached/snapshot (with age, e.g. "snapshot · 3h"), red unavailable. Poll every 5 s.
- `POST /api/admin/fault` body `{server, mode}` → forwards to that server's `set_fault`. Admin panel hidden behind `?admin=1`.

### 7.6 VLM context enrichment (prompt `roads_v2`)
For each segment midpoint, build a context string from MCP results:
```
Context (secondary evidence; may be stale):
- NWS alerts: Flood Warning (status: live)
- Nearest stream gauge 3.1 km: stage 18.2 ft, flood category moderate (status: cached, 4 min old)
- Road is 1.8 m lower than its surroundings (DEM)
- FEMA flood zone: AE
Use the image as primary evidence. Use context only to resolve unclear cases.
```
- Run `roads_v2` only on segments that are `uncertain` under `roads_v1` (CLI flag `--only-uncertain`). Keep the `roads_v1` label in `label_history`.
- Store the context dict on the segment (`context` property) and show it in the evidence panel with each item's status.

### 7.7 Vulnerability-weighted priority (`analysis/priority.py`)
1. Assign each building a `tract_geoid` (point-in-polygon on centroid).
2. Per tract: `pop_in_aoi = E_TOTPOP × (tract area inside AOI / tract area)`; `per_building = pop_in_aoi / residential buildings of that tract inside AOI`. Building `est_pop = per_building` for residential buildings, 0 otherwise.
3. Per zone: `est_pop` = sum of building `est_pop`. Vulnerability fields: building-weighted mean of tract `RPL_THEMES` (`svi`), and building-share-weighted sums of `E_AGE65`, `E_NOVEH`, `E_DISABL`, `E_LIMENG` (scaled the same way as population).
4. `priority_score = est_pop × (0.5 + svi)`; `config.yaml: priority.svi_weight` controls the 0.5/1.0 split.
5. Add a +50% multiplier if the zone contains a nursing home or dialysis facility (configurable).
6. Action panel shows both "by people" rank and "by priority" rank, with a toggle. Each zone card shows: est. people, 65+, no vehicle, disability, limited English, SVI percentile.

### 7.8 Acceptance (Tier 3)
- With all MCP servers running and internet on, all pills are green or amber with age.
- Demo script works: (1) `set_fault down` on water_weather → pill turns amber "snapshot", labeling and analysis still run; (2) kill `vulnerability_server.py` → backend falls back, zone vulnerability still shows; (3) disconnect the network entirely → app keeps working from caches and snapshots.
- At least one zone where the two rankings differ, visible in the UI.
- Tests: envelope fallback chain for each fault mode; priority formula on a toy dataset.

## 7a. Data sources (feeds Tier 3, judged criterion "Creative Use of Data")

The organizer encourages extra data. Each source below must change a decision on screen or a number in the evaluation, not only decorate the map. All were checked reachable from the event Wi-Fi on 13 Nov 2026 unless marked.

| # | Source | What it adds | Where it shows | Access |
|---|---|---|---|---|
| 1 | Organizer `UTSA-flood-images` (HF) | Before/after tiles + per-tile FEMA zone, rain, USGS gauge, terrain, county ACS | Core imagery, evidence panel context card, FEMA-only baseline | in use |
| 2 | NOAA ERI Harvey GeoTIFFs (`s3://noaa-eri-pds/2017_Hurricane_Harvey/20170830_RGB/`) | Fills the 9 AOI tiles missing from the organizer set; any AOI in the flight path | Imagery (`imagery.source: geotiff`) | public HTTPS, ~15-30 MB per tile |
| 3 | USDA NAIP 2016 (Microsoft Planetary Computer COGs) | Matching "before" imagery for #2 | Imagery | anonymous SAS token, windowed reads |
| 4 | CDC/ATSDR SVI 2022 (Texas tract CSV) + Census TIGER 2022 tracts | Tract-level vulnerability (65+, no vehicle, disability, limited English) instead of county ACS | Zone ranking "by priority" | HTTPS CSV + shapefile zip |
| 5 | USGS 3DEP elevation (ImageServer export, GeoTIFF) | Relative road elevation: low roads first, context for uncertain roads | Labelling order, roads_v3 context | HTTPS |
| 6 | IEM archive of NWS warnings (HGX, 25 Aug-5 Sep 2017) | Was a flood warning active over this road on the image date? | Evidence context card, timeline | HTTPS |
| 7 | FloodSentinel (organizer list, HF `Ymx1025/floodsentinel`) | 15-min gauge series + NWS warnings for Harris County during Harvey | Gauge sparkline in context card, sensor-vs-image amber flag | HF |
| 8 | Flood-zone homes by county 2026 (organizer list, HF) | Homes in SFHA and NFIP take-up for Harris County | One-line context in the dispatch brief | HF, 84 KB |
| 9 | FloodNet (Harvey drone images, road flooded/not labels) | Second accuracy number for the road prompt on a different sensor | Evaluation slide only | GitHub/Dropbox, licence conflict noted |
| 10 | CoSA low-water crossings + SmartSA flood sensors (organizer list) | San Antonio live mode: same graph logic on real crossings | San Antonio view (Tier 3 end) | ArcGIS REST + CKAN |
| 11 | FEMA NFHL flood zone polygons | Road-level FEMA baseline (per tile today) | Baseline, map layer | timed out on 13 Nov, retry or skip |

Rules: every source goes through `sources/` with the envelope from 3.6 and a snapshot in `data/raw/snapshots/`, so the failure demo works for all of them. Credit every source on the map and in the README.

## 7b. Tier 3b — Demo-grade interface (required)

Goal: a judge understands the problem and the answer within 10 seconds of seeing the screen, and can complete the whole story with clicks only. "Interface and Usability" is one of five equally weighted judging criteria, so this tier gets the same rigour as the model work.

### 7b.1 Quality bar (applies to every tier from now on)
- First screen shows the answer, not the controls: a headline row with **people cut off**, **neighbourhoods cut off**, **roads blocked / open**, **facilities cut off**.
- Every number on screen is clickable and leads to its evidence (zone list, road chips, label JSON).
- One primary action per panel. The safe route is one button plus one map click.
- Plain words. "Best estimate" and "Confirmed only" instead of permissive/strict. Uncertainty is always visible, never hidden.
- Works on a 1366x768 projector and on a phone (single column below 900 px).
- No console errors. Every async action shows a loading state; every empty state says what to do next.
- Colour is never the only signal: status words appear next to colours, and the palette stays legible for colour-blind viewers.

### 7b.2 Features
1. **Headline KPI row** above the map from `/api/summary`.
2. **Before/after swipe** on the map: a draggable divider that reveals 2016 on the left and 2017 on the right.
3. **Before/after slider in the evidence panel** for each road chip (same divider control).
4. **Zone markers**: a numbered badge on each cut-off zone matching the list rank; the list and map highlight each other on hover.
5. **Route card**: safe route vs naive shortest route drawn together (blue solid vs grey dashed), with the blocked roads the naive route would cross.
6. **First-visit guide**: a dismissible three-step card ("Red roads are underwater", "Red areas have no way out", "Click Route me out").
7. **Review mode**: keyboard shortcuts (O = open, B = blocked, → = next) so three people can label fast.
8. **Print/export view** of the ranked zone list (feeds Tier 8 situation report later).

### 7b.3 Acceptance
- A person who has not seen the app finds the top cut-off zone and draws a route out in under 60 seconds, unaided. Test with someone outside the team.
- Lighthouse accessibility score ≥ 90. No console errors.
- Screenshots at 1366x768 and 390x844 committed to `docs/screenshots/`.

## 8. Tier 4 — Building and facility damage (if time)

1. Chips: for each building (limit `limits.max_buildings`, default 1500; order: facilities first, then buildings in cut-off zones, then lowest relative elevation), square window = footprint bbox + 15 m, min side 40 m, 384×384 px, from both `pre` and `post`, footprint outline drawn in magenta on both.
2. Prompt `buildings_v1`, two images (first = before, second = after):
   ```json
   {
     "water_present_after": "none | around | partially_submerged | submerged | unknown",
     "structural_damage": "none | minor | major | destroyed | unknown",
     "change_detected": true,
     "visibility": "clear | partly_occluded | occluded",
     "confidence": 0.0,
     "reason": "max 200 chars"
   }
   ```
3. Severity score 0–4: destroyed 4, major 3, submerged 3, partially_submerged 2, minor or around 1, else 0 (take the max). Severity below confidence 0.5 shows hatched.
4. Facilities get the same label in `site_status`; facility cards show "flooded" badges.
5. Frontend: building layer colored by severity (0 none, 1 `#FFE08A`, 2 `#F4A261`, 3 `#D7263D`, 4 `#5C0A14`); before/after swipe using `maplibre-gl-compare` with pre on the left and post on the right; action panel tab "Most damaged" sorted by severity then est_pop.
6. Acceptance: 200+ buildings labeled; evidence panel shows both chips side by side.

## 8a. Tier 4 amendment — Economic impact (team request, 13 Nov)

Goal: an honest estimate of what the flood cost in each area, built from the same imagery, with every number carrying its source and a range.

1. **Building check.** For a random sample of up to 1,000 residential buildings per area (seed fixed, so the estimate is reproducible), crop before and after around the footprint and ask the VLM how far water reaches: `none | around | at_walls | deep | unknown`. Random sampling lets us extrapolate to every building in the area with a 95% interval instead of labelling all of them.
2. **Values.** Pre-flood median home value per census tract from the ACS 2016 5-year summary file (table B25077, no API key needed). Hill Country uses the latest ACS available.
3. **Damage ratio per class.** Calibrated from real claims: OpenFEMA NFIP v3 claims for the event (date of loss in the event window, same counties) give `buildingDamageAmount / buildingPropertyValue` by `waterDepth`. Map the VLM classes to depth bands (around = under 1 ft, at_walls = 1-3 ft, deep = over 3 ft) and take the median ratio per band.
4. **Market effect (separate line, cited).** Floodplain discount after Harvey of 3.1-5.5% (Freddie Mac). Shown as a range, never added to the damage total without a label.
5. **Farmland.** OSM farmland and meadow polygons inside the photographed area. The VLM checks a crop of each field (pair areas: new water; Hill Country: water, sand or mud deposit, debris). Report flooded or scoured acres. Dollar values only with a cited per-acre figure.
6. **Validation.** Compare our estimated building damage per tract with the NFIP paid claims per tract (a lower bound, since many homes are uninsured). Report the correlation.
7. **UI.** An "Impact" panel per area: homes with water at walls or deeper, estimated building damage (range), affected value, farmland acres, and the method in plain words.

## 8b. Tier 4b — Relief site recommender (teammate design, 13 Nov)

Goal: from before and after imagery, find open areas that stayed dry and recommend a temporary relief point on each (food, water, shelter or clinic tents), based on the need of nearby residents.

1. **Candidates.** OSM parking lots, school grounds, sports pitches, parks, community centres and places of worship, at least 1,500 m².
2. **Imagery check.** The VLM sees before and after crops of each candidate and answers: dry, mostly empty, free of debris, what changed. Result: usable, not usable or uncertain, with a reason. Uncertain goes to the review queue.
3. **Safety and access filters.** Drop sites in a FEMA high-risk tile, lower than their surroundings (DEM), more than 50 m from a road node, or not reachable from outside over open roads (strict graph).
4. **Need.** Residential buildings within 1.5 km network distance, weighted by tract SVI (65+, no vehicle, low income). Food need counts homes whose nearest food store is flooded or unreachable.
5. **Selection.** Greedy maximum coverage: pick the few sites that together cover the most weighted need.
6. **Use.** Paved lot: food or water. Large paved lot (over 5,000 m²): combined food and water. Large grass field at a school or community centre: tent shelter or mobile clinic.
7. **Map.** Recommended sites green, uncertain amber, rejected grey. Clicking a site shows both crops, the model's reason, the checks it passed, the recommended use and the people it serves.

## 9. Tier 5 — Crowd-sourced photo verification (if time)

1. `POST /api/photos` (multipart: image, optional lat/lon, optional taken_at).
2. Location: EXIF GPS via Pillow; else request lat/lon from the client (UI asks the user to click the map). Reject if outside AOI.
3. Privacy, before anything is stored:
   - Detect faces with OpenCV's DNN face detector (or Haar cascade if the DNN model is unavailable offline) and Gaussian-blur them.
   - Strip all EXIF metadata. Downscale to max 1024 px.
   - Store only the snapped `segment_id` and the blurred image; discard the raw coordinates after snapping.
   - Document the license-plate limitation in `docs/DECISIONS.md` (not handled unless time allows).
4. Snap to the nearest road segment within 50 m; if none, reply "no road nearby" and keep the photo out of analysis.
5. Prompt `photo_v1` with two images (street photo, satellite chip of that segment) plus the current segment label as text:
   ```json
   {
     "location_consistent": "yes | no | unclear",
     "water_on_road": "none | partial | full | unknown",
     "passable": "yes | no | uncertain",
     "confidence": 0.0,
     "reason": "max 200 chars"
   }
   ```
6. Update rule: if `location_consistent != "no"` and `confidence >= 0.7`, set segment `status` from `passable` (same mapping as 5.3), `status_source="photo"`, then `POST /api/analyze`. Otherwise add to the review queue with the photo attached.
7. Frontend: "Upload photo" button; photo markers on the map (camera icon); evidence panel shows photo, satellite chip, before/after segment status. Zones and routes update after the upload.
8. Acceptance: uploading a test photo of a flooded street near an open segment flips it to blocked and changes the zone list within 10 s.

## 10. Tier 6 — Multilingual alerts and resident text interface (if time)

### 10.1 Languages
- Census ACS 5-year table C16001 (language spoken at home) per tract, fetched via the Census API (snapshot into `data/raw/snapshots/languages.json`).
- Per zone: languages other than English whose building-weighted share is ≥ 5%, maximum 2.

### 10.2 Alerts
- For each zone, generate an alert with the same model (text-only chat call), prompt `alert_v1`. Input facts (structured, not free text): zone label, mode, recommended action (`shelter_in_place` if no route exists in `permissive` mode, else `evacuate`), route summary (first 3 street names), destination name, hazards.
- Output per language: max 320 characters, plain words, no emoji, must include the destination name and the first street of the route if evacuating.
- Every generated alert is shown with a "DRAFT – machine translated, needs review" label and a "mark reviewed" button. Nothing is sent anywhere.
- Endpoint `GET /api/alerts` → per zone: languages, texts, reviewed flag.

### 10.3 Resident text interface (simulated SMS)
- `POST /api/sms` body `{from: str, body: str}`. Accepted bodies: `lat,lon` or "HELP lat,lon". Free-text addresses are out of scope unless time allows (then geocode with Nominatim at max 1 request/second with a proper User-Agent, per its usage policy).
- Reply: plain text under 480 characters with zone status, action, route turns (max 5), destination. If the location is in a cut-off zone with no permissive route: shelter-in-place text plus the zone id so responders can find them.
- Log incoming requests (rounded to 3 decimals) as a "requests for help" layer.
- Frontend: SMS simulator panel styled as a phone chat.

### 10.4 Acceptance
- Alerts generated for every zone; at least one zone produces a non-English alert if the data supports it (otherwise record why in DECISIONS).
- SMS request from inside a zone returns shelter-in-place text; from outside returns a route.

## 11. Tier 7 — Contamination warnings (if time)

1. Sources (snapshot): EPA Toxics Release Inventory facilities for the state (CSV with lat/lon), OSM `man_made=wastewater_plant`, OSM `man_made=storage_tank` with `content` tags if present.
2. Label each site with `buildings_v1` (Tier 4 pipeline). If Tier 4 is not built, use the post image only with prompt `site_v1` (same schema minus `change_detected`).
3. Flooded sites (`water_present_after` in `around`, `partially_submerged`, `submerged`, confidence ≥ 0.6) get a 500 m warning buffer (configurable), labeled "possible contamination – avoid floodwater contact". This is a proximity warning, not a hydrological model; say so in the evidence panel.
4. Zones and routes that intersect a warning buffer get a hazard badge; the route response includes `hazards: [...]`.
5. Acceptance: layer toggles on/off; a route through a buffer shows the hazard in the turn list.

## 12. Tier 8 — Shelter capacity gap and situation report (if time)

### 12.1 Capacity gap
1. For every residential building not in a cut-off zone (strict mode), find the nearest reachable shelter by network distance (multi-source Dijkstra from shelters).
2. Demand per shelter = sum of `est_pop` assigned × `config.yaml: shelter.seek_rate` (default 0.10), with vulnerable residents (65+, no vehicle) weighted × `shelter.vulnerable_weight` (default 2.0).
3. Gap = demand − capacity. Shelters with unknown capacity show "capacity unknown".
4. Action panel tab "Shelters": table of shelter, reachable population, demand, capacity, gap; overloaded shelters in red. Map: shelter catchment areas (convex hull of assigned buildings) colored by gap.

### 12.2 Situation report
- `GET /api/sitrep` returns Markdown; `GET /api/sitrep.html` renders it for printing.
- Contents: event, time generated, data source statuses with ages, counts per road status, top 10 zones (both rankings), unreachable critical facilities, top damaged buildings (if Tier 4), shelter gaps (if 12.1), contamination warnings (if Tier 7), list of segments marked by humans vs VLM, and a limitations section (model labels can be wrong; snapshots may be stale).
- Frontend: "Export report" button opens the HTML version.

## 13. Cross-cutting requirements

- Logging: `structlog` or stdlib logging with JSON output; every VLM call logs prompt_id, latency, cache hit, status.
- Every VLM-derived value shown in the UI has a visible link to its evidence (chip + reason).
- Never block the UI on the VLM: labeling jobs run in background tasks; endpoints return `202` with a job id; `GET /api/jobs/{id}` reports progress.
- No secrets in git. `.env.example` lists every variable.
- `README.md`: setup in under 10 commands, plus the demo script below.

## 14. Demo script (target 4 minutes)

1. Open dashboard: imagery, colored roads, pills all green. (Tier 1)
2. Click a red road: chip + reason. (Tier 1)
3. Action panel: top cut-off zone, switch ranking from "people" to "priority", point out the nursing home zone moving up. (Tiers 2–3)
4. Route me out from inside a near-cut-off area. (Tier 2)
5. Admin: `set_fault down` on water_weather, then pull the network cable. Pills turn amber, run "Route me out" again, it still works. (Tier 3)
6. If built: upload a flooded street photo, watch a road turn red and a new zone appear. (Tier 5)
7. If built: show the Spanish alert for that zone and an SMS request from inside it. (Tier 6)
8. Export the situation report. (Tier 8)

Rehearse with `VLM_MOCK=0` and all caches warm. Keep a backup run with `VLM_MOCK=1` in case the GB10 is unreachable on demo day.
