"""Load every data/sites/*.json bundle into Atlas (`sites` + `site_features`), create the
2dsphere and Vector Search indexes.

  uv run python scripts/load_sites.py
"""
from __future__ import annotations

import json
import pathlib

from permit_harness import db
from permit_harness.site import site_doc_from_geojson_bundle, site_from_doc

DATA = pathlib.Path(__file__).resolve().parents[1] / "data" / "sites"


def main():
    db.ensure_indexes()
    files = sorted(DATA.glob("*.json"))
    if not files:
        raise SystemExit("no bundles in data/sites; run scripts/fetch_site.py --all first")
    for p in files:
        doc = site_doc_from_geojson_bundle(json.loads(p.read_text()))
        db.upsert_site(doc)
        s = site_from_doc(doc)
        summ = s.summary_for_llm()
        print(f"loaded {doc['_id']}: {s.acres:.1f} ac, {len(s.homes)} homes, {len(s.commercial)} commercial, "
              f"{len(s.wetlands)} wetlands, buildable {summ['buildable_area_acres']} ac, homes by sector {summ['homes_by_sector']}")
        # show off $geoNear once so you know it works
        lon, lat = doc["centroid"]["coordinates"]
        nh = db.nearest_home(doc["_id"], lon, lat)
        if nh:
            print(f"   $geoNear from parcel centroid -> nearest home {nh.get('address') or nh['pin']} at {nh['distance_m']:.0f} m")
    print("vector index queryable:", db.vector_index_ready(), "(if False, wait a minute; the loop falls back to recency until then)")


if __name__ == "__main__":
    main()
