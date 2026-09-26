"""Show what MongoDB Atlas is doing for the harness, live. Good for judges.

  uv run python scripts/inspect_atlas.py
  uv run python scripts/inspect_atlas.py --ask "homes to the east and a wetland to the north"
"""
from __future__ import annotations

import argparse

from rich.console import Console
from rich.table import Table

from permit_harness import config as C
from permit_harness import db

console = Console()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", default="cooling too loud at homes on the east side, generators visible from houses",
                    help="plain-text query for Vector Search over lessons")
    a = ap.parse_args()
    d = db.db()

    t = Table(title=f"Atlas database '{C.MONGODB_DB}'")
    t.add_column("collection"); t.add_column("documents", justify="right"); t.add_column("what it is")
    what = {"sites": "parcel + neighbors + wetlands (GeoJSON)", "site_features": "each home / wetland, 2dsphere",
            "designs": "one plan per round", "reviews": "penalty + violations + letter (Atlas Search)",
            "lessons": "what was learned (Vector Search, automated embeddings)", "runs": "status + penalty history",
            "checkpoints": "LangGraph state after every node", "checkpoint_writes": "LangGraph pending writes"}
    for name in ["sites", "site_features", "designs", "reviews", "lessons", "runs", "checkpoints", "checkpoint_writes"]:
        t.add_row(name, f"{d[name].estimated_document_count():,}", what.get(name, ""))
    console.print(t)

    t = Table(title="Search indexes")
    t.add_column("collection"); t.add_column("index"); t.add_column("type"); t.add_column("queryable")
    for coll in ["lessons", "reviews"]:
        try:
            for ix in d[coll].list_search_indexes():
                t.add_row(coll, ix["name"], ix.get("type", "search"), str(ix.get("queryable")))
        except Exception as e:  # noqa: BLE001
            t.add_row(coll, "-", str(e)[:40], "-")
    console.print(t)

    t = Table(title="Runs (the hard metric over time)")
    t.add_column("run"); t.add_column("site"); t.add_column("status"); t.add_column("penalty per round")
    for r in d.runs.find().sort("created_at", 1):
        t.add_row(r["_id"], r["site_id"], r["status"], " ".join(str(h["penalty"]) for h in r.get("history", [])))
    console.print(t)

    console.rule("$geoNear: closest home to each site's centroid")
    for s in d.sites.find({}, {"name": 1, "centroid": 1}):
        lon, lat = s["centroid"]["coordinates"]
        nh = db.nearest_home(s["_id"], lon, lat)
        if nh:
            console.print(f"  {s['_id']}: {nh.get('address') or nh['pin']} at {nh['distance_m']:.0f} m")

    console.rule(f"$vectorSearch over lessons for: \"{a.ask}\"")
    try:
        for l in db.similar_lessons_text(a.ask, k=5):
            console.print(f"  [{l.get('rule')}] score={l.get('score', 0):.3f} ({l.get('site_id')}): {l.get('text')}")
    except Exception as e:  # noqa: BLE001
        console.print(f"  vector search error: {e}")

    console.rule("Atlas Search over rejection letters for: \"wetland buffer\"")
    try:
        for p in db.search_rejections("wetland buffer", k=3):
            console.print("  " + p[:160].replace("\n", " ") + "...")
    except Exception as e:  # noqa: BLE001
        console.print(f"  search error: {e}")

    console.rule("LangGraph checkpoints (resume state)")
    for run in d.runs.find({}, {"_id": 1}).limit(20):
        n = d.checkpoints.count_documents({"thread_id": run["_id"]})
        console.print(f"  thread {run['_id']}: {n} checkpoints")


if __name__ == "__main__":
    main()
