"""A hand-made parcel in the same bundle format scripts/fetch_site.py writes.
Used by the tests and by `--offline` demos when the real GIS data is not loaded."""
from __future__ import annotations

from shapely.geometry import Polygon, mapping

from .geo import from_local, utm_to_geojson, geojson_to_utm


def bundle(site_id: str = "synthetic_wayne", lon: float = -74.25, lat: float = 40.93,
           homes_side: str = "E", wetland_side: str = "N") -> dict:
    origin_pt = geojson_to_utm({"type": "Point", "coordinates": [lon, lat]})
    origin = (origin_pt.x, origin_pt.y)

    def gj(poly_local: Polygon) -> dict:
        return utm_to_geojson(from_local(poly_local, origin))

    half = 190.0  # ~36 acres
    parcel = Polygon([(-half, -half), (half, -half), (half, half), (-half, half)])
    sx, sy = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}[homes_side]
    homes = []
    for i in range(8):
        along = -140 + i * 40
        cx = sx * (half + 40) + (along if sx == 0 else 0)
        cy = sy * (half + 40) + (along if sy == 0 else 0)
        homes.append({"type": "Feature", "properties": {"PAMS_PIN": f"H{i + 1}", "PROP_CLASS": "2", "PROP_LOC": f"{i + 1} Maple Ln"},
                      "geometry": gj(Polygon([(cx - 15, cy - 15), (cx + 15, cy - 15), (cx + 15, cy + 15), (cx - 15, cy + 15)]))})
    homes.append({"type": "Feature", "properties": {"PAMS_PIN": "C1", "PROP_CLASS": "4B", "PROP_LOC": "1 Industrial Way"},
                  "geometry": gj(Polygon([(-half - 80, -60), (-half - 20, -60), (-half - 20, 60), (-half - 80, 60)]))})
    wx, wy = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}[wetland_side]
    if wx == 0:
        wet = Polygon([(-half, wy * 120), (half, wy * 120), (half, wy * half), (-half, wy * half)])
    else:
        wet = Polygon([(wx * 120, -half), (wx * half, -half), (wx * half, half), (wx * 120, half)])
    return {
        "site_id": site_id, "name": "Synthetic 36-acre parcel, Wayne NJ",
        "parcel": {"type": "Feature", "properties": {"PAMS_PIN": "SYN_1", "MUN_NAME": "WAYNE TWP", "COUNTY": "PASSAIC",
                                                     "PROP_CLASS": "1", "CALC_ACRE": round(parcel.area / 4046.86, 1)},
                   "geometry": gj(parcel)},
        "neighbors": {"type": "FeatureCollection", "features": homes},
        "wetlands": {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"LABEL20": "DECIDUOUS WOODED WETLANDS", "ACRES": round(wet.area / 4046.86, 1)},
             "geometry": gj(wet)}]},
        "highlands_preservation": False,
        "sources": {"note": "synthetic"},
    }
