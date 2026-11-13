# Evaluation

Generated 2026-11-13T19:58+00:00 by `python -m floodlens evaluate`.

Read the controls and baselines before the model numbers. Hand labels come from the app's review buttons (`eval/labels/`).

## bevil_oaks

Road pieces judged by the model: 101. Status: {'uncertain': 5, 'blocked': 94, 'open': 2}.

### Controls (no hand labels needed)

| Control | What should happen | Result |
|---|---|---|
| 2016 vs 2016 (40 roads) | almost no new water | pair question said new water on 0; statuses {'open': 38, 'blocked': 2} |
| Shuffled before photo | pair answers change | pair said new water on 32; same status as real run 40 of 40 |
| After then before (swapped) | flooded roads flip to no new water | pair said new water on 19; same status 40 of 40 |

No hand labels yet. Label roads in the app (Check roads tab) to get accuracy numbers.

