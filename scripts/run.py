"""Run the harness on a site.

  uv run python scripts/run.py --site wayne_west_belt --run demo1
  uv run python scripts/run.py --site wayne_west_belt --run demo1 --crash-after 3   # dies after round 3
  uv run python scripts/run.py --site wayne_west_belt --run demo1 --resume          # picks up at round 4
  uv run python scripts/run.py --site wayne_haul_rd  --run demo2                    # second site, uses lessons
  uv run python scripts/run.py --site synthetic --run t --offline --no-llm          # no network at all
"""
from __future__ import annotations

import argparse

from permit_harness import config as C
from permit_harness.loop import run
from permit_harness.store import MongoStore, MemoryStore


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--run", required=True, help="run id; reuse it with --resume")
    ap.add_argument("--max-rounds", type=int, default=12)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--crash-after", type=int, default=None, help="exit(3) after this round to demo resume")
    ap.add_argument("--no-llm", action="store_true", help="deterministic designer/reviewer (backup mode)")
    ap.add_argument("--offline", action="store_true", help="in-memory store + synthetic site, no Atlas")
    ap.add_argument("--sleep", type=float, default=0.0, help="pause between rounds (for the demo pacing)")
    ap.add_argument("--designer-model", default=C.DESIGNER_MODEL)
    ap.add_argument("--reviewer-model", default=C.REVIEWER_MODEL)
    a = ap.parse_args()

    if a.offline:
        from permit_harness.synthetic import bundle
        from permit_harness.site import site_doc_from_geojson_bundle
        store = MemoryStore()
        store.upsert_site(site_doc_from_geojson_bundle(bundle(site_id=a.site)))
    else:
        store = MongoStore()
        store.ensure()
    r = run(a.site, a.run, store=store, max_rounds=a.max_rounds, resume=a.resume, use_llm=not a.no_llm,
            crash_after_round=a.crash_after, sleep_s=a.sleep, designer_model=a.designer_model,
            reviewer_model=a.reviewer_model)
    print("penalty curve:", [(h["round"], h["penalty"]) for h in r["history"]])


if __name__ == "__main__":
    main()
