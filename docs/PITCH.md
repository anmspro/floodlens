# FloodLens pitch (prejudging, about 3 minutes)

## The problem

More than half of flood deaths in the US happen in vehicles. People drive into water they cannot see from the car, and Texas leads the country in flood deaths. After a flood, the maps responders get show which areas are wet. They do not show which streets still lead out, or which neighbourhoods have become islands.

## What we built

FloodLens answers one question. Which streets became islands?

It takes aerial photos from before and after a flood and checks every road, one short piece at a time, with a vision-language model. Each road is asked about twice. Once against the photo from before the flood, and once on the flood photo alone. If either answer sees water, the road is closed.

From those answers FloodLens builds a map of the street network that still works. It finds every neighbourhood with no dry road out, ranks them by how many people live there and how vulnerable they are, and draws the safest way out from any home.

## What it found

In West Houston on 30 August 2017, after the Addicks and Barker reservoir releases:

- About 11,300 people in 263 neighbourhoods had no dry road out.
- 1,252 of 1,683 road pieces we could see were underwater.
- Four facilities were cut off, including a hospital, a fire station and an elementary school.
- Estimated damage to homes was about 156 million dollars. We calibrated it on 31,900 real flood insurance claims from Harvey, and our estimate ranks census tracts in the same order as the real claims (rank correlation 0.69).

On a blind spot check the model found 15 of 16 flooded roads. A rule based on FEMA flood zones found 4 of 18.

## Who it helps

- **Dispatchers** get a ranked list of cut-off neighbourhoods and the one flooded road to clear first.
- **Residents** get a safe route and a short alert in English or Spanish.
- **Relief teams** get dry parking lots and fields to set up food, water or tents, chosen to reach the most vulnerable people within walking distance.
- **Planners** in San Antonio see which neighbourhoods can only leave across a low-water crossing, before the next storm.

## Why it is different

- It reads roads, not areas.
- It shows its evidence. Every red road opens the before and after photo and the model's reason, and a person can correct it.
- It keeps working when the network fails. Everything runs from local data, and a saved copy of every brief is ready.
- It is honest about limits. Water can drain before a photo is taken, and trees hide streets, so unclear roads are marked unsure instead of guessed.

## Close

FloodLens turns aerial photos into a list of who is cut off, how to reach them and where to set up help. It works on every area in the dataset, from Houston to the Hill Country, and on San Antonio's low-water crossings.
