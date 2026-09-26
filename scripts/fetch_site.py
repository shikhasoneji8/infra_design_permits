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
# Texas (Travis County / Austin): county appraisal district parcels + federal National Wetlands Inventory.
TX_PARCELS = "https://gis.traviscountytx.gov/server1/rest/services/Boundaries_and_Jurisdictions/TCAD_public/MapServer/0/query"
NWI = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0/query"
WETLANDS = "https://services1.arcgis.com/QWdNfRs7lkPq4g4Q/arcgis/rest/services/Wetlands_2020/FeatureServer/14/query"
OUT = pathlib.Path(__file__).resolve().parents[1] / "data" / "sites"

TX_DEMO_SITES = [
    # East Austin, near Decker Lake and the Colony Park area: 27 ac tract with 165 small lots within 300 m.
    {"pin": "782904", "site_id": "austin_gilbert_rd", "name": "5412 Gilbert Rd, Austin TX (27 ac, Travis County)"},
    {"pin": "201589", "site_id": "austin_decker_lake", "name": "9801 Decker Lake Rd, Austin TX (27 ac, Travis County)"},
]
# Vetted candidates (homes and wetlands close enough that the rules bite). The app's "Add a site" list.
CATALOG = [
    {"state": "nj", "pin": "1614_302_72", "site_id": "wayne_west_belt", "name": "West Belt, Wayne NJ (36 ac vacant, 120 homes near)"},
    {"state": "nj", "pin": "2008_6_1.01", "site_id": "kenilworth_monroe", "name": "251 Monroe Ave, Kenilworth NJ (36 ac industrial, 328 homes)"},
    {"state": "nj", "pin": "0614_2326_1.01", "site_id": "vineland_crystal", "name": "563 Crystal Ave, Vineland NJ (39 ac industrial, 388 homes)"},
    {"state": "nj", "pin": "1614_1508_2", "site_id": "wayne_haul_rd", "name": "55 Haul Rd, Wayne NJ (30 ac industrial, 206 homes)"},
    {"state": "nj", "pin": "1614_604_17", "site_id": "wayne_dey_rd", "name": "150 Dey Rd, Wayne NJ (18 ac industrial, 42 homes, 10 wetlands)"},
    {"state": "nj", "pin": "1614_302_2", "site_id": "wayne_demarest", "name": "74 Demarest Dr, Wayne NJ (28 ac industrial, 81 homes, 8 wetlands)"},
    {"state": "nj", "pin": "1614_4402_11", "site_id": "wayne_colfax", "name": "835 Colfax Rd, Wayne NJ (17 ac vacant, 152 homes)"},
    {"state": "nj", "pin": "1614_1616_49", "site_id": "wayne_route23", "name": "1701 Route 23, Wayne NJ (16 ac vacant, 136 homes)"},
    {"state": "nj", "pin": "0614_3202_24.01", "site_id": "vineland_maple", "name": "2363 Maple Ave, Vineland NJ (31 ac commercial, 214 homes, 12 wetlands)"},
    {"state": "nj", "pin": "0614_1202_5", "site_id": "vineland_west_blvd", "name": "2192 N West Blvd, Vineland NJ (49 ac industrial, 46 homes)"},
    {"state": "tx", "pin": "782904", "site_id": "austin_gilbert_rd", "name": "5412 Gilbert Rd, Austin TX (27 ac, 165 lots near)"},
    {"state": "tx", "pin": "201589", "site_id": "austin_decker_lake", "name": "9801 Decker Lake Rd, Austin TX (27 ac, 127 lots near)"},
    {"state": "tx", "pin": "109934", "site_id": "austin_hamilton_pool", "name": "16400 Hamilton Pool Rd, Austin TX (26 ac, 168 lots near)"},
]
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


def fetch_tx(pin: str, site_id: str, name: str, neighbor_radius_m: int = 400, wetland_radius_m: int = 150) -> dict:
    """Travis County, Texas. TCAD parcels carry no land-use code, so residential lots are inferred:
    under 0.6 acre with a platted LOT in the legal description. Wetlands from the USFWS NWI; every
    NWI wetland is treated as an Austin Critical Environmental Feature (150 ft setback)."""
    from shapely.geometry import shape
    from shapely.geometry.polygon import orient
    print(f"[{site_id}] Travis County parcel {pin} ...", end=" ", flush=True)
    parcel = q(TX_PARCELS, where=f"PROP_ID={int(pin)}", outFields="PROP_ID,situs_address,situs_city,tcad_acres,legal_desc",
               returnGeometry="true", outSR="4326")
    if not parcel["features"]:
        raise SystemExit(f"no Travis County parcel with PROP_ID {pin}")
    pf = parcel["features"][0]
    pf["properties"]["county"] = "TRAVIS"
    geom = pf["geometry"]
    simple = orient(shape(geom).simplify(0.00003, preserve_topology=True), sign=-1.0)
    if simple.geom_type == "MultiPolygon":
        simple = max(simple.geoms, key=lambda p: p.area)
    esri_geom = {"rings": [list(map(list, simple.exterior.coords))], "spatialReference": {"wkid": 4326}}
    print(f"{pf['properties'].get('tcad_acres')} ac")

    print(f"[{site_id}] neighbors within {neighbor_radius_m} m ...", end=" ", flush=True)
    neighbors = q(TX_PARCELS, where="1=1", geometry=json.dumps(esri_geom), geometryType="esriGeometryPolygon", inSR="4326",
                  spatialRel="esriSpatialRelIntersects", distance=str(neighbor_radius_m), units="esriSRUnit_Meter",
                  outFields="PROP_ID,situs_address,tcad_acres,legal_desc", returnGeometry="true", outSR="4326",
                  maxAllowableOffset="0.00002", resultRecordCount="1500")
    feats = []
    for f in neighbors["features"]:
        a = f["properties"]
        if str(a.get("PROP_ID")) == str(pin):
            continue
        acres = float(a.get("tcad_acres") or 0)
        legal = (a.get("legal_desc") or "").upper()
        residential = acres < 0.6 and "LOT" in legal
        a["PROP_CLASS"] = "2" if residential else ("4A" if acres >= 0.6 else "1")
        feats.append(f)
    neighbors["features"] = feats
    homes = sum(1 for f in feats if f["properties"]["PROP_CLASS"] == "2")
    print(f"{len(feats)} parcels, {homes} inferred residential")

    print(f"[{site_id}] NWI wetlands within {wetland_radius_m} m ...", end=" ", flush=True)
    wet = q(NWI, where="1=1", geometry=json.dumps(esri_geom), geometryType="esriGeometryPolygon", inSR="4326",
            spatialRel="esriSpatialRelIntersects", distance=str(wetland_radius_m), units="esriSRUnit_Meter",
            outFields="*", returnGeometry="true", outSR="4326", maxAllowableOffset="0.00002")
    for f in wet["features"]:
        pr = f["properties"]
        pr["WETLAND_TYPE"] = pr.get("WETLAND_TYPE") or pr.get("Wetlands.WETLAND_TYPE") or "WETLAND"
        pr["resource_class"] = "exceptional"  # Austin CEF: 150 ft
    print(f"{len(wet['features'])} polygons: {sorted({f['properties']['WETLAND_TYPE'] for f in wet['features']})}")
    return {"site_id": site_id, "name": name, "parcel": pf, "neighbors": neighbors, "wetlands": wet,
            "highlands_preservation": False, "state": "TX",
            "sources": {"parcels": TX_PARCELS, "wetlands": NWI, "fetched_with": "scripts/fetch_site.py --state tx",
                        "note": "residential lots inferred (<0.6 ac with platted LOT); TCAD has no land-use code"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="nj", choices=["nj", "tx"])
    ap.add_argument("--pin")
    ap.add_argument("--site-id")
    ap.add_argument("--name")
    ap.add_argument("--all", action="store_true", help="fetch the demo sites for --state")
    ap.add_argument("--catalog", action="store_true", help="fetch every vetted parcel in CATALOG (NJ and TX) not already on disk")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.catalog:
        jobs = [dict(c) for c in CATALOG if not (OUT / f"{c['site_id']}.json").exists()]
        print(f"{len(jobs)} catalog parcels to fetch ({len(CATALOG) - len(jobs)} already on disk)")
    else:
        jobs = (TX_DEMO_SITES if a.state == "tx" else DEMO_SITES) if a.all else [{"pin": a.pin, "site_id": a.site_id, "name": a.name or a.site_id}]
        if not a.all and not (a.pin and a.site_id):
            ap.error("--pin and --site-id, --all, or --catalog")
    failures = []
    for j in jobs:
        state = j.pop("state", a.state)
        try:
            b = fetch_tx(**j) if state == "tx" else fetch(**j)
        except Exception as e:  # noqa: BLE001
            print(f"[{j['site_id']}] FAILED: {str(e)[:200]}")
            failures.append(j["site_id"])
            continue
        p = OUT / f"{j['site_id']}.json"
        p.write_text(json.dumps(b))
        print(f"[{j['site_id']}] wrote {p} ({p.stat().st_size // 1024} KB)")
    if failures:
        print("failed:", failures)


if __name__ == "__main__":
    sys.exit(main())
