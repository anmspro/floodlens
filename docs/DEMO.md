# Demo script (4 minutes)

## Before you start

- Run `python -m floodlens offline-check --area all`. Every area should say OK.
- Start the app with `python -m floodlens serve` and open `http://127.0.0.1:8501/#west_houston`.
- Close the first-visit guide once so it does not cover the map.
- Keep a second tab open on the GitHub repo.

## 0:00 The problem (30 seconds)

Most flood deaths in Texas happen on roads. People drive into water they cannot see from the car. Flood maps show which areas are wet. They do not tell you which streets still lead out.

FloodLens answers one question. Which streets became islands?

## 0:30 One area, one answer (60 seconds)

Show West Houston on 30 August 2017, the day after the Addicks and Barker reservoir releases.

- Point at the top row. This many people have no dry road out. This many road pieces are underwater.
- Click **Compare 2016 ⇄ 2017** and drag the slider. Same streets, one year apart.
- Click a red road. The panel shows the two crops, what the model saw in each and the rule that decided. Drag the small slider on the crop.
- Say: the model checks every road twice. Once against the before photo, once on the after photo alone. Any water means the road is closed.

## 1:30 Who needs help first (45 seconds)

- Open the first cut-off neighbourhood. Read its population and its census vulnerability.
- Click **Write in English**, then **Escribir en español**. Say: the model only sees our numbers. Any number it makes up is struck out.
- Click the first flooded road under "clear the shortest one first".

## 2:15 Get me out (30 seconds)

- Click **Route me out**, then a home near a cut-off area.
- The blue line is the safe route. The dashed line is what a normal map app would suggest. Read how many flooded roads that route crosses.

## 2:45 Relief and cost (30 seconds)

- **Relief** tab. These are dry parking lots and fields the model checked, chosen to reach the most vulnerable people within a 1.5 km walk.
- **Impact** tab. Estimated damage to homes with a range. The damage ratios come from real flood insurance claims for this storm. Point at the county insurance gap.

## 3:15 It works without the internet (25 seconds)

- Click **Offline drill**. The source badges turn to "snapshot". Everything on the map still works.
- Say: in a real flood the network fails first. FloodLens keeps working from what it already has.

## 3:40 Other places (20 seconds)

- Switch the area to **Texas Hill Country** (July 2025). There are no before photos here, so it uses the after photo alone.
- Switch to **San Antonio**. No photos at all. If every low-water crossing floods, these neighbourhoods can only leave across one.

## Close

Close on one sentence. FloodLens turns aerial photos into a list of who is cut off, how to reach them and where to set up help, and it keeps working when the network goes down.

## Honest answers to likely questions

- **How accurate is it?** On a blind spot check of 35 West Houston roads, the two-question check found 15 of 16 flooded roads. 4 of the 19 roads it closed were dry. A FEMA flood-zone rule found 4 of 18. These labels came from an AI check, not people, so treat them as a sanity check.
- **What does it get wrong?** Water may have drained before the photo was taken. Trees hide streets. The model sometimes ignores which photo is "before". That is why the second question looks at the after photo alone.
- **Why this model?** It is the vision model the event provided. The pipeline takes any OpenAI-compatible endpoint, so a larger model drops in through `.env`.
- **Is the damage figure real money?** It is an estimate with a range. It is good for comparing areas, not for settling claims.
