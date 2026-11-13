# How to present FloodLens

Two rounds, two different jobs.

| Time | Round | What the judge wants | What you show |
|---|---|---|---|
| 3:00–3:45 | Prejudging, 1 judge | The idea and why it matters | The pitch (3 min), one screen of the app at most |
| 4:00–4:30 | Semifinal, 1 judge, 12–15 teams | A working demo | The demo path below (3 min), then questions |

Assume each semifinal slot is short. Practise the 3-minute path. Keep the 5-minute extras ready only if the judge asks for more.

## Roles

- **Speaker** tells the story and looks at the judge.
- **Driver** sits at the laptop and clicks exactly the path below. Do not explore.
- **Backup** holds this page, answers number questions and watches the time.

## Set up before 3:45 (driver)

1. `python -m floodlens serve`, then open `http://127.0.0.1:8501/#west_houston` in Chrome.
2. Close the first-visit guide. Set **Theme** to *light* (reads better on a projector). Full screen with Cmd+Ctrl+F.
3. Leave the default layers on. Collapse the evidence sheet. Make sure the **Cut off** tab is open.
4. Have a second tab open on the GitHub repo.
5. Wi-Fi can stay on. If it drops, the app still works (that becomes part of the story).

## The one sentence

**FloodLens turns aerial photos into a list of who is cut off, how to reach them, and where to set up help.**

Say it at the start and at the end.

## Prejudging (3 minutes, mostly talking)

Use `docs/PITCH.md`. The shape is:

1. **Problem (40 s).** Most flood deaths happen in cars. Flood maps show wet areas, not which streets still lead out.
2. **Idea (40 s).** Which streets became islands? The model checks every road against the before photo and on the flood photo alone. Any water closes the road.
3. **Result (50 s).** West Houston, 30 August 2017. About 11,300 people cut off in 263 neighbourhoods. A hospital, a fire station and a school cut off. About 156 million dollars of home damage, calibrated on 31,900 real insurance claims.
4. **Who it helps (30 s).** Dispatchers, residents (English and Spanish), relief teams, San Antonio planners.
5. **Close (20 s).** The one sentence.

If the judge wants to see something, show only the opening screen (headline row and map).

## Semifinal demo (3 minutes)

Each step says where to click and what to say.

### 1. Opening screen (20 s)

- **Click:** nothing.
- **Say:** "This is West Houston the day after the Addicks and Barker reservoir releases. Red dashed roads were underwater, green were dry. About 11,000 people had no dry road out. Four facilities were cut off, including a hospital."

### 2. The evidence (40 s)

- **Click:** *Compare before ⇄ after* and drag the white bar once. Click it again to turn it off.
- **Click:** any red road near the middle. The evidence sheet opens.
- **Drag:** the small slider on the crop.
- **Say:** "Every road is checked twice by the vision model. Once against the 2016 photo, once on the flood photo alone. If either sees water, the road is closed. You can see the photo and the reason for every decision, and correct it."

### 3. Who needs help first (40 s)

- **Click:** the first neighbourhood in the **Cut off** list.
- **Say:** "Neighbourhoods are ranked by people and by census vulnerability: older residents, no car, limited English."
- **Click:** *Escribir en español* (or *Write in English*).
- **Say:** "The chat model writes the dispatch brief and a text alert. It only sees our numbers. Any number it makes up is struck through."

### 4. Get me out (30 s)

- **Click:** *Route me out*, then a house next to a red neighbourhood.
- **Say:** "Blue is the safe route. The dashed line is what a normal map app would send you down, through flooded roads. Below are the nearest hospitals, fire and police stations and how far they are from this route."

### 5. Relief and cost (30 s)

- **Click:** the **Relief** tab, then the first site.
- **Say:** "The model also checks parking lots and fields. These two stayed dry, can be reached by truck, and cover the most vulnerable people within a 1.5 km walk."
- **Click:** the **Impact** tab.
- **Say:** "About 156 million dollars of damage to homes, with a range. The damage ratios come from 31,900 real Harvey flood insurance claims, and our estimate ranks the census tracts in the same order as the real claims."

### 6. Works offline (15 s)

- **Click:** *Offline drill* in the left panel.
- **Say:** "In a real flood the network fails first. Every source falls back to its saved copy and the app keeps working."

### 7. Close (15 s)

- **Click:** the area selector, choose **Bevil Oaks**.
- **Say:** "It runs on every area in the dataset, from Houston to the Hill Country." Then the one sentence.

## If the judge gives you 5 minutes

Add these after step 6, in this order:

1. **Check tab.** "A person can confirm or correct any road with one key. Corrections update the map at once and feed the accuracy numbers."
2. **Hill Country** (if it says ready). "There is no before photo here, so the model judges the flood photo alone."
3. **San Antonio** (if it says ready). "No photos at all. If every low-water crossing floods, these neighbourhoods can only leave across one."
4. **GitHub tab.** The code, the README and `docs/EVALUATION.md`.

## Questions you will get

| Question | Answer |
|---|---|
| How accurate is it? | On a blind check of 35 roads, the model found 15 of 16 flooded roads and closed 4 dry ones. A FEMA flood-zone rule found 4 of 18. That check was done by an AI assistant, not people, so treat it as a sanity check. |
| What does it get wrong? | Water can drain before the photo, trees hide streets, and the model sometimes ignores which photo is "before". That is why the second question looks at the flood photo alone, and why unclear roads are marked unsure. |
| Why should we trust the damage number? | It is a range, not an appraisal. The damage ratios come from 31,900 real claims for this storm, and the estimate ranks tracts in the same order as real claims (rank correlation 0.69). |
| How do you count people? | Census tract population, spread over home-sized buildings by footprint area. |
| What model? | Qwen3-VL-8B, served by the event. Any OpenAI-compatible endpoint works through one setting, so a larger model drops in. |
| What if the internet is down? | Everything runs from local data. Only the street basemap needs the internet, and the app hides it if it cannot load. |
| What is new here? | It reads roads, not areas. It shows evidence for every decision. It goes from photos to a rescue list, a route, relief sites and a cost estimate in one place. |
| How would this be used for real? | NOAA flies the same kind of photos within days of every major US disaster. The pipeline runs on new photos without retraining. |

## Numbers to remember

- West Houston: about **11,300 people** cut off, **263 neighbourhoods**, **1,252 of 1,683** road pieces underwater.
- Cut off: **UT MD Anderson West Houston**, **Cy-Fair fire station 5**, **Harris County Sheriff's Office**, **Bear Creek Elementary**.
- Home damage: about **$156M** (range **$21M to $253M**), calibrated on **31,900** claims, rank correlation **0.69**.
- Relief: **2 sites** cover about **2,250 people**.
- Emergency services within 5 km: **97** (12 hospitals, 25 fire stations, 6 police).
- Spot check: **15 of 16** flooded roads found (FEMA rule **4 of 18**).

## If something breaks

- **The app will not load.** Restart with `python -m floodlens serve` and reload.
- **The brief is slow.** It falls back to the saved copy. Keep talking.
- **A map tile is blank.** Keep going. The data layers are local.
- **Everything fails.** Show the README on GitHub and walk through the pipeline diagram.
