# FloodLens

Which streets became islands? FloodLens compares aerial photos from before and after a flood, road by road, with a vision-language model. It marks each road open or underwater, finds the neighbourhoods with no dry road out, draws the safest way out, recommends dry sites for relief points and estimates what the flood cost.

Built for the UTSA Social Impact AI Datathon (GB10 Hackathon), November 2026. It covers every area in the organizer dataset:

| Area | Event | Photos |
|---|---|---|
| West Houston: Addicks and Barker (+ Bear Creek, Eldridge) | Hurricane Harvey | 267 before/after pairs, 30 Aug 2017 |
| Richmond and Rosenberg (Brazos River) | Hurricane Harvey | 165 pairs, 31 Aug 2017 |
| Kingwood and Forest Cove (San Jacinto River) | Hurricane Harvey | 93 pairs, 31 Aug 2017 |
| Bevil Oaks (Pine Island Bayou) | Hurricane Harvey | 25 pairs, 1 Sep 2017 |
| Simonton (Brazos River) | Hurricane Harvey | 25 pairs, 3 Sep 2017 |
| Texas Hill Country (Guadalupe River) | July 2025 flood | 50 after-only photos |

## What it does

- **Road check.** Every road piece gets a before and after crop. The model answers two questions (new water on the road compared with before? what covers the road in the after photo?) and any "water" makes the road blocked. People can confirm or correct any road in the app.
- **Cut-off neighbourhoods.** Flooded roads are removed from the street network. Neighbourhoods with no road to a hospital, a shelter or the edge of the photographed area are ranked by people and by census vulnerability (CDC SVI).
- **Safe route.** From any point, the safest way out, drawn next to the usual shortest route and the flooded roads it would cross.
- **Dispatch brief.** The chat model writes a short brief and a resident text alert in English or Spanish from structured facts only. Any number not in the data is struck through.
- **Relief sites.** Dry open areas (parking lots, school grounds, fields) checked by the model, filtered for access and elevation, chosen to cover the most vulnerable people within 1.5 km.
- **Economic impact.** A random sample of homes is checked for floodwater. Damage ratios come from real NFIP claims for the same event, values from the 2016 census. Totals carry a range.
- **Public data with fallback.** Census vulnerability, NWS warnings, USGS elevation and live NWS alerts each report live, cached, snapshot or unavailable. The "Offline drill" button shows the app working without the internet.
- **MCP server.** `mcp_servers/floodlens_mcp.py` exposes the same data to any MCP client (cut-off zones, roads near a point, safe route, context, impact, relief sites).

## Setup

PyPI is blocked on the event Wi-Fi, so the environment comes from conda-forge.

```sh
conda env create -f environment.yml        # or: micromamba create -f environment.yml
conda activate floodlens
cp .env.example .env                        # then put the model server key in .env
```

## Run

All commands run from the repo root. Add `--area <id>` or `--area all` (default: west_houston).

```sh
scripts/run_all_areas.sh python    # the whole pipeline for every area (about 1-2 hours)
python -m floodlens serve          # open http://127.0.0.1:8501
```

Single steps: `fetch-data`, `check-data`, `imagery`, `prepare`, `chips`, `vlm-check`, `label`, `fetch-context`, `analyze`, `impact`, `relief`, `prefetch-briefs`, `evaluate`, `offline-check`.

## Before judging: make it work offline

Everything the app shows is stored under `data/` (git-ignored, several GB). Once the pipeline has run:

```sh
python -m floodlens prefetch-briefs --area all   # save English and Spanish briefs for the top neighbourhoods
python -m floodlens offline-check --area all     # every area must say OK
```

Then turn Wi-Fi off and click through the app. The street basemap disappears (it is the only internet layer); the aerial photos and all results stay. Briefs show the saved copy. To give a teammate a working copy, zip the `data/` folder and pass it on a drive.

## When something fails

Every command adds a line to `ops/RUNLOG.md`. A failing command also writes `ops/errors/<time>_<command>.md` with the full traceback. Commit and push that file so a teammate can fix it.

## Plan, decisions, evaluation

- `FLOODLENS_PLAN.md`: the build plan by tier, with the team's amendments at the top.
- `docs/DECISIONS.md`: what we chose and why, including what did not work.
- `docs/EVALUATION.md`: baselines, controls and accuracy (`python -m floodlens evaluate`). Hand labels from the app are saved in `eval/labels/`.

## Data and credits

- Organizer dataset [jennifer-fowler/UTSA-flood-images](https://huggingface.co/datasets/jennifer-fowler/UTSA-flood-images) (CC-BY-NC-4.0): NOAA NGS emergency imagery (CC0), USDA NAIP, per-image FEMA, weather, gauge and terrain context.
- Roads, buildings, facilities, open areas, farmland and food stores © OpenStreetMap contributors (ODbL).
- CDC/ATSDR Social Vulnerability Index 2022; US Census TIGER/Line tracts 2016 and 2022; ACS 2016 5-year summary file (median home value, B25077).
- OpenFEMA NFIP claims v3 (damage ratios by water depth, insured-loss check).
- NWS warnings archive from the Iowa Environmental Mesonet; live alerts from api.weather.gov.
- USGS 3D Elevation Program.
- Model: Qwen3-VL-8B-Instruct, served by the organizer.
- Map rendering: MapLibre GL JS (BSD-3-Clause), vendored in `web/vendor/`.
