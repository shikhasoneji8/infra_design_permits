# Permit Harness: a data center that redesigns itself until New Jersey says yes

Data centers take years to permit because every redesign starts from scratch.
This harness never forgets a rejection.

A designer agent places a 40 MW data center on a **real New Jersey parcel** (live NJOGIS
parcel data, NJDEP 2020 wetlands). A strict reviewer measures the plan with **code**
(noise propagation, wetland transition areas, setbacks, air and water thresholds) and
rejects it, citing the rule. Every rejection becomes a lesson in **MongoDB Atlas**.
The loop runs until the penalty score hits zero, survives being killed mid-run, and the
second parcel passes faster because the lessons transfer.

Built for the MongoDB x Cerebral Valley Harness Engineering hackathon, Statement Two
(long-horizon engineering). MongoDB Atlas is the agent's memory, not a log sink.

## The loop

```
site data (Atlas, GeoJSON) -> designer agent -> geometry + physics (Python) -> reviewer agent
        ^                                                                          |
        |                       lessons (Atlas Vector Search)  <-------------------+
        +-------------------- checkpoint (Atlas `runs`) ----------------------------+
```

The math is code, the judgment is AI. Distances, dBA, acres, tons of NOx and gallons per
day are computed in plain Python from constants with sources (`permit_harness/config.py`),
so the score is honest. The agents decide what to move and why.

## Rulebook (nine rules, seven are real NJ law)

| # | Rule | Limit | Source |
|---|------|-------|--------|
| R1 | Night noise at a home's lot line | 50 dBA | N.J.A.C. 7:29-1.2 |
| R2 | Day noise at homes / any time at commercial | 65 dBA | N.J.A.C. 7:29-1.2 |
| R3 | Wetland transition areas | 150 ft exceptional, 50 ft intermediate | N.J.A.C. 7:7A-3.3 |
| R4 | Air general permit cap for gensets | 100 MMBtu/hr combined | NJDEP GP-005A |
| R5 | NOx potential-to-emit | 25 tons/yr (Title V) | NJDEP |
| R6 | Water withdrawal | 100,000 gal/day | NJDEP Water Allocation |
| R7 | Stormwater major development | 0.25 acre new impervious | N.J.A.C. 7:8-1.2 |
| R8 | Residential setback (demo value) | 200 ft | local zoning |
| R9 | Generators hidden from homes (soft) | line of sight | NoVA design guidance |
| CAP | Keep >= 80% of target MW | 20 pts | so the agent cannot "win" by deleting the data center |

Hard rules 10 points, medium 5, soft 1. Zero means the permit passes.

## Atlas collections

| Collection | Holds | Atlas feature |
|---|---|---|
| `sites` | parcel, homes, wetlands as GeoJSON | 2dsphere |
| `site_features` | each neighbor / wetland as its own geo document | `$geoNear` (closest home to a generator), `$geoIntersects` |
| `designs` | full plan per round, `parent_design_id` | version history |
| `reviews` | violations, measured values, penalty | the hard metric over time |
| `lessons` | short rule learned + embedding | **Vector Search** |
| `runs` | status, round, last design id, penalty history | kill it, restart it, it continues |

## Run it

```bash
cp .env.example .env            # fill in MONGODB_URI (Atlas Hackathon Sandbox) and OPENROUTER_API_KEY
uv sync --extra dev
uv run pytest                   # rules engine + offline loop, no network needed

uv run python scripts/fetch_site.py --all      # pull the two real Wayne, NJ parcels from NJ GIS
uv run python scripts/load_sites.py            # load into Atlas, create geo + vector indexes

uv run python scripts/run.py --site wayne_west_belt --run demo1 --crash-after 3   # dies after round 3
uv run python scripts/run.py --site wayne_west_belt --run demo1 --resume          # continues at round 4
uv run python scripts/run.py --site wayne_haul_rd  --run demo2                    # site 2 uses the lessons
uv run python scripts/report.py --run demo1 --run demo2                           # HTML replay + penalty chart
```

Backup mode with no model or no network: `--no-llm` (deterministic designer) and `--offline` (in-memory store).

## Sites

Both real, both in Wayne, NJ, picked because homes and wetlands are close enough that the rules bite:

* `wayne_west_belt`: PAMS PIN 1614_302_72, 36.3 acres, vacant. 120 homes within 300 m, 11 wetland polygons within 100 m.
* `wayne_haul_rd`: PAMS PIN 1614_1508_2, 30.3 acres, industrial. 206 homes within 300 m, 19 wetland polygons.
