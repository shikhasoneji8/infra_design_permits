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
    # Wayne: dev site, dense homes and wetlands so every rule bites.
    {"pin": "1614_302_72", "site_id": "wayne_west_belt", "name": "West Belt, Wayne NJ (36 ac vacant)"},
    # Kenilworth: the town where CoreWeave is building on the old pharma campus amid water and noise pushback.
    {"pin": "2008_6_1.01", "site_id": "kenilworth_monroe", "name": "251 Monroe Ave, Kenilworth NJ (36 ac industrial, 328 homes within 300 m)"},
    # Vineland: DataOne (2.6M sq ft) approved despite noise and water outrage; a 39-acre industrial lot with 388 homes nearby.
    {"pin": "0614_2326_1.01", "site_id": "vineland_crystal", "name": "563 Crystal Ave, Vineland NJ (39 ac industrial, 388 homes within 300 m)"},
]
PARCEL_FIELDS = "PAMS_PIN,PROP_LOC,OWNER_NAME,CALC_ACRE,PROP_CLASS,LAND_DESC,BLDG_DESC,MUN_NAME,COUNTY"


def q(url: str, **params) -> dict:
    params.setdefault("f", "geojson")
    r = requests.post(url, data=params, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:300]}")
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
    # Spatial queries use a simplified outline (some parcels have thousands of vertices and the
    # services reject oversized POST bodies with HTTP 400). The full-detail geometry is what we store.
    # Esri rings must be CLOCKWISE; GeoJSON exteriors are counter-clockwise. Sending a CCW
    # ring gets a bare {"error": {"code": 400}} from the parcel service.
    from shapely.geometry import shape
    from shapely.geometry.polygon import orient
    simple = shape(geom).simplify(0.00003, preserve_topology=True)
    if simple.geom_type == "MultiPolygon":
        simple = max(simple.geoms, key=lambda p: p.area)
    simple = orient(simple, sign=-1.0)  # clockwise exterior
    rings = [list(map(list, simple.exterior.coords))]
    esri_geom = {"rings": rings, "spatialReference": {"wkid": 4326}}
    minx, miny, maxx, maxy = shape(geom).bounds
    envelope = {"xmin": minx, "ymin": miny, "xmax": maxx, "ymax": maxy, "spatialReference": {"wkid": 4326}}
    print(f"{pf['properties'].get('CALC_ACRE')} ac, class {pf['properties'].get('PROP_CLASS')}, "
          f"{sum(len(r) for r in (geom['coordinates'] if geom['type'] == 'Polygon' else [r for p in geom['coordinates'] for r in p]))} vertices")

    def spatial(url: str, radius_m: int, **extra) -> dict:
        common = dict(where="1=1", inSR="4326", spatialRel="esriSpatialRelIntersects", distance=str(radius_m),
                      units="esriSRUnit_Meter", returnGeometry="true", outSR="4326", maxAllowableOffset="0.00002", **extra)
        try:
            return q(url, geometry=json.dumps(esri_geom), geometryType="esriGeometryPolygon", **common)
        except Exception as e:  # noqa: BLE001
            print(f"(polygon query failed: {str(e)[:80]}; retrying with bounding box)", end=" ", flush=True)
            return q(url, geometry=json.dumps(envelope), geometryType="esriGeometryEnvelope", **common)

    print(f"[{site_id}] neighbors within {neighbor_radius_m} m ...", end=" ", flush=True)
    neighbors = spatial(PARCELS, neighbor_radius_m, outFields="PAMS_PIN,PROP_LOC,PROP_CLASS,CALC_ACRE", resultRecordCount="1000")
    neighbors["features"] = [f for f in neighbors["features"] if f["properties"].get("PAMS_PIN") != pin]
    homes = sum(1 for f in neighbors["features"] if str(f["properties"].get("PROP_CLASS", "")).startswith("2"))
    print(f"{len(neighbors['features'])} parcels, {homes} residential")

    print(f"[{site_id}] wetlands within {wetland_radius_m} m ...", end=" ", flush=True)
    wetlands = spatial(WETLANDS, wetland_radius_m, outFields="LABEL20,TYPE20,ACRES")
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
