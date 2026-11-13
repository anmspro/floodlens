# FloodLens

Which streets became islands? FloodLens compares aerial photos from before and after a flood, road by road, with a vision-language model. It marks each road open or underwater, finds the neighbourhoods with no dry road out, and draws the safest way out.

Built for the UTSA Social Impact AI Datathon (GB10 Hackathon), November 2026. The demo replays Hurricane Harvey in the Addicks and Barker area of West Houston, using NOAA aerial photos from 30 August 2017 and USDA NAIP photos from 2016.

## Setup

PyPI is blocked on the event Wi-Fi, so the environment comes from conda-forge.

```sh
conda env create -f environment.yml        # or: micromamba create -f environment.yml
conda activate floodlens
cp .env.example .env                        # then put the model server key in .env
```

## Run

All commands run from the repo root.

```sh
python -m floodlens fetch-data     # metadata + only the before/after images for the area
python -m floodlens check-data     # 1,204 rows x 273 cols, 575 reciprocal pairs, images decode
python -m floodlens imagery        # PNG tiles -> GeoTIFF (EPSG:3857) -> mosaics + map overlays
python -m floodlens prepare        # OSM roads, buildings, facilities; roads cut into <=100 m pieces
python -m floodlens chips          # before/after crop for every road piece
python -m floodlens vlm-check      # one question to the model + seconds per crop
python -m floodlens label          # the model judges every road piece (cached)
python -m floodlens analyze        # cut-off neighbourhoods, facility reachability
python -m floodlens serve          # open http://127.0.0.1:8501
```

`python -m floodlens all` runs the pipeline from `fetch-data` to `analyze`. Set `VLM_MOCK=1` in `.env` to work without the model server.

## When something fails

Every command adds a line to `ops/RUNLOG.md`. A failing command also writes `ops/errors/<time>_<command>.md` with the full traceback. Commit and push that file so a teammate can fix it.

## Plan and decisions

- `FLOODLENS_PLAN.md`: the build plan by tier, with the team's amendments at the top.
- `docs/DECISIONS.md`: what we chose and why, including what did not work.

## Data and credits

- Organizer dataset [jennifer-fowler/UTSA-flood-images](https://huggingface.co/datasets/jennifer-fowler/UTSA-flood-images) (CC-BY-NC-4.0).
- NOAA National Geodetic Survey emergency response imagery (CC0). USDA NAIP imagery.
- Roads, buildings and facilities © OpenStreetMap contributors (ODbL).
- Model: Qwen3-VL-8B-Instruct, served by the organizer.
- Map rendering: MapLibre GL JS (BSD-3-Clause), vendored in `web/vendor/`.
