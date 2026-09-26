"""Site model: one real parcel plus everything around it that the rules care about."""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Polygon, MultiPolygon, shape

from . import config as C
from .geo import geojson_to_utm, to_local, largest_polygon, bounds_summary, utm_epsg_for, DEFAULT_EPSG


@dataclass
class Neighbor:
    pin: str
    prop_class: str  # NJ MOD-IV: '2' residential, '4A' commercial, '4B' industrial, '1' vacant ...
    geom: Polygon | MultiPolygon  # local metres
    address: str = ""

    @property
    def is_residential(self) -> bool:
        return self.prop_class.startswith("2") or self.prop_class in {"3A", "3B", "4C"}

    @property
    def is_commercial(self) -> bool:
        return self.prop_class.startswith("4") and self.prop_class != "4C"


@dataclass
class Wetland:
    label: str
    resource_class: str  # exceptional | intermediate | ordinary
    geom: Polygon | MultiPolygon  # local metres
    buffer_m: float = 0.0  # set from the state profile


@dataclass
class Site:
    site_id: str
    name: str
    pin: str
    municipality: str
    acres: float
    origin_utm: tuple[float, float]
    parcel: Polygon  # local metres
    neighbors: list[Neighbor] = field(default_factory=list)
    wetlands: list[Wetland] = field(default_factory=list)
    highlands_preservation: bool = False
    state: str = "NJ"
    epsg: int = DEFAULT_EPSG

    @property
    def profile(self) -> dict:
        return C.profile(self.state)

    # ---- derived ----------------------------------------------------------
    @property
    def homes(self) -> list[Neighbor]:
        return [n for n in self.neighbors if n.is_residential]

    @property
    def commercial(self) -> list[Neighbor]:
        return [n for n in self.neighbors if n.is_commercial]

    def buildable(self) -> Polygon | MultiPolygon:
        """Parcel minus wetlands and their transition-area buffers."""
        b = self.parcel
        for w in self.wetlands:
            b = b.difference(w.geom.buffer(w.buffer_m))
        return b

    def summary_for_llm(self) -> dict:
        """Compact description the designer can reason about. Local metres, origin at parcel centroid."""
        parcel_b = bounds_summary([self.parcel])
        homes = self.homes
        # Where are the homes, by compass sector, and how close?
        sectors: dict[str, dict] = {}
        for h in homes:
            cx, cy = h.geom.centroid.x, h.geom.centroid.y
            sec = _sector(cx, cy)
            d = self.parcel.distance(h.geom)
            s = sectors.setdefault(sec, {"count": 0, "nearest_m": 1e9})
            s["count"] += 1
            s["nearest_m"] = round(min(s["nearest_m"], d), 1)
        wet = []
        for w in self.wetlands:
            wb = bounds_summary([w.geom])
            wet.append({"label": w.label, "class": w.resource_class, "buffer_m": round(w.buffer_m, 1),
                        "sector": _sector(w.geom.centroid.x, w.geom.centroid.y), "bounds": wb})
        buildable = self.buildable()
        return {
            "site_id": self.site_id, "name": self.name, "pin": self.pin, "municipality": self.municipality,
            "acres": round(self.acres, 1),
            "parcel_bounds_m": parcel_b,
            "parcel_polygon_m": [[round(x, 1), round(y, 1)] for x, y in self.parcel.exterior.coords][:40],
            "buildable_area_acres": round(buildable.area / C.ACRE_M2, 1),
            "homes_by_sector": sectors,
            "commercial_neighbors": len(self.commercial),
            "wetlands": wet,
            "highlands_preservation": self.highlands_preservation,
            "state": self.state, "rulebook": self.profile["name"],
        }


def _sector(x: float, y: float) -> str:
    import math

    ang = math.degrees(math.atan2(y, x)) % 360  # 0 = east, 90 = north
    names = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    return names[int(((ang + 22.5) % 360) // 45)]


# ---- (de)serialisation to/from the Atlas `sites` document ------------------
def site_from_doc(doc: dict) -> Site:
    origin = tuple(doc["origin_utm"])
    epsg = int(doc.get("epsg", DEFAULT_EPSG))
    parcel = largest_polygon(to_local(geojson_to_utm(doc["parcel"], epsg), origin))
    neighbors = [
        Neighbor(pin=n["pin"], prop_class=str(n.get("prop_class", "")), address=n.get("address", ""),
                 geom=to_local(geojson_to_utm(n["geometry"], epsg), origin))
        for n in doc.get("neighbors", [])
    ]
    prof = C.profile(doc.get("state", "NJ"))
    wetlands = [
        Wetland(label=w["label"], resource_class=w["resource_class"],
                geom=to_local(geojson_to_utm(w["geometry"], epsg), origin),
                buffer_m=prof["buffer_by_class"].get(w["resource_class"], 0.0))
        for w in doc.get("wetlands", [])
    ]
    return Site(site_id=doc["_id"], name=doc["name"], pin=doc["pin"], municipality=doc.get("municipality", ""),
                acres=doc.get("acres", parcel.area / C.ACRE_M2), origin_utm=origin, parcel=parcel,
                neighbors=neighbors, wetlands=wetlands,
                highlands_preservation=doc.get("highlands_preservation", False), state=doc.get("state", "NJ"), epsg=epsg)


def site_doc_from_geojson_bundle(bundle: dict) -> dict:
    """Turn the frozen GeoJSON bundle written by scripts/fetch_site.py into a `sites` document."""
    c = shape(bundle["parcel"]["geometry"]).centroid
    epsg = utm_epsg_for(c.x, c.y)
    parcel_utm = largest_polygon(geojson_to_utm(bundle["parcel"]["geometry"], epsg))
    origin = (parcel_utm.centroid.x, parcel_utm.centroid.y)
    neighbors = []
    for f in bundle["neighbors"]["features"]:
        a = f["properties"]
        neighbors.append({"pin": str(a.get("PAMS_PIN") or a.get("PROP_ID") or ""), "prop_class": str(a.get("PROP_CLASS") or ""),
                          "address": a.get("PROP_LOC") or a.get("situs_address") or "", "geometry": f["geometry"]})
    wetlands = []
    for f in bundle["wetlands"]["features"]:
        pr = f["properties"]
        label = (pr.get("LABEL20") or pr.get("WETLAND_TYPE") or pr.get("Wetlands.WETLAND_TYPE") or "WETLAND").upper()
        rc = pr.get("resource_class") or C.WETLAND_CLASS_BY_LABEL.get(label, "intermediate")
        wetlands.append({"label": label, "resource_class": rc,
                         "acres": pr.get("ACRES") or pr.get("Wetlands.ACRES"), "geometry": f["geometry"]})
    p = bundle["parcel"]["properties"]
    return {
        "_id": bundle["site_id"], "name": bundle["name"], "pin": str(p.get("PAMS_PIN") or p.get("PROP_ID") or ""),
        "municipality": p.get("MUN_NAME") or p.get("situs_city") or "", "county": p.get("COUNTY") or p.get("county") or "",
        "prop_class": str(p.get("PROP_CLASS") or ""), "acres": p.get("CALC_ACRE") or p.get("tcad_acres"),
        "origin_utm": [origin[0], origin[1]], "epsg": epsg,
        "parcel": bundle["parcel"]["geometry"],  # GeoJSON, 2dsphere-indexed
        "centroid": {"type": "Point", "coordinates": list(shape(bundle["parcel"]["geometry"]).centroid.coords)[0]},
        "neighbors": neighbors, "wetlands": wetlands,
        "highlands_preservation": bundle.get("highlands_preservation", False),
        "state": bundle.get("state", "NJ"),
        "sources": bundle.get("sources", {}),
    }
