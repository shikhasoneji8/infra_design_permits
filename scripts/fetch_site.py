"""Fetch a real New Jersey parcel and everything around it from state GIS, and freeze it
as a GeoJSON bundle in data/sites/<site_id>.json.

Sources (all public, all queried live):
  Parcels and MOD-IV Composite of NJ (NJOGIS)   maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0
  Wetlands 2020 in New Jersey (NJDEP LU/LC)    services1.arcgis.com/QWdNfRs7lkPq4g4Q/.../Wetlands_2020/FeatureServer/14

Usage:
  uv run python scripts/fetch_site.py --pin 1614_302_72 --site-id wayne_west_belt --name "West Belt, Wayne NJ"
  uv run python scripts/fetch_site.py --all        # the two demo sites
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import requests

PARCELS = "https://maps.nj.gov/arcgis/rest/services/Framework/Cadastral/MapServer/0/query"
WETLANDS = "https://services1.arcgis.com/QWdNfRs7lkPq4g4Q/arcgis/rest/services/Wetlands_2020/FeatureServer/14/query"
OUT = pathlib.Path(__file__).resolve().parents[1] / "data" / "sites"

DEMO_SITES = [
    {"pin": "1614_302_72", "site_id": "wayne_west_belt", "name": "West Belt, Wayne NJ (36 ac vacant)"},
    {"pin": "1614_1508_2", "site_id": "wayne_haul_rd", "name": "55 Haul Rd, Wayne NJ (30 ac industrial)"},
]
PARCEL_FIELDS = "PAMS_PIN,PROP_LOC,OWNER_NAME,CALC_ACRE,PROP_CLASS,LAND_DESC,BLDG_DESC,MUN_NAME,COUNTY"


def q(url: str, **params) -> dict:
    params.setdefault("f", "geojson")
    r = requests.post(url, data=params, timeout=90)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{url}: {data['error']}")
    return data


def fetch(pin: str, site_id: str, name: str, neighbor_radius_m: int = 400, wetland_radius_m: int = 150) -> dict:
    print(f"[{site_id}] parcel {pin} ...", end=" ", flush=True)
    parcel = q(PARCELS, where=f"PAMS_PIN='{pin}'", outFields=PARCEL_FIELDS, returnGeometry="true", outSR="4326")
    if not parcel["features"]:
        raise SystemExit(f"no parcel with PAMS_PIN {pin}")
    pf = parcel["features"][0]
    geom = pf["geometry"]
    esri_geom = {"rings": geom["coordinates"] if geom["type"] == "Polygon" else [r for poly in geom["coordinates"] for r in poly],
                 "spatialReference": {"wkid": 4326}}
    print(f"{pf['properties'].get('CALC_ACRE')} ac, class {pf['properties'].get('PROP_CLASS')}")

    print(f"[{site_id}] neighbors within {neighbor_radius_m} m ...", end=" ", flush=True)
    neighbors = q(PARCELS, where="1=1", geometry=json.dumps(esri_geom), geometryType="esriGeometryPolygon", inSR="4326",
                  spatialRel="esriSpatialRelIntersects", distance=str(neighbor_radius_m), units="esriSRUnit_Meter",
                  outFields="PAMS_PIN,PROP_LOC,PROP_CLASS,CALC_ACRE", returnGeometry="true", outSR="4326",
                  maxAllowableOffset="0.00002", resultRecordCount="1000")
    neighbors["features"] = [f for f in neighbors["features"] if f["properties"].get("PAMS_PIN") != pin]
    homes = sum(1 for f in neighbors["features"] if str(f["properties"].get("PROP_CLASS", "")).startswith("2"))
    print(f"{len(neighbors['features'])} parcels, {homes} residential")

    print(f"[{site_id}] wetlands within {wetland_radius_m} m ...", end=" ", flush=True)
    wetlands = q(WETLANDS, where="1=1", geometry=json.dumps(esri_geom), geometryType="esriGeometryPolygon", inSR="4326",
                 spatialRel="esriSpatialRelIntersects", distance=str(wetland_radius_m), units="esriSRUnit_Meter",
                 outFields="LABEL20,TYPE20,ACRES", returnGeometry="true", outSR="4326", maxAllowableOffset="0.00002")
    print(f"{len(wetlands['features'])} polygons: {sorted({f['properties'].get('LABEL20') for f in wetlands['features']})}")

    return {
        "site_id": site_id, "name": name, "parcel": pf, "neighbors": neighbors, "wetlands": wetlands,
        "highlands_preservation": False,
        "sources": {"parcels": PARCELS, "wetlands": WETLANDS, "fetched_with": "scripts/fetch_site.py"},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pin")
    ap.add_argument("--site-id")
    ap.add_argument("--name")
    ap.add_argument("--all", action="store_true", help="fetch the two demo sites")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = DEMO_SITES if a.all else [{"pin": a.pin, "site_id": a.site_id, "name": a.name or a.site_id}]
    if not a.all and not (a.pin and a.site_id):
        ap.error("--pin and --site-id, or --all")
    for j in jobs:
        b = fetch(**j)
        p = OUT / f"{j['site_id']}.json"
        p.write_text(json.dumps(b))
        print(f"[{j['site_id']}] wrote {p} ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    sys.exit(main())
