"""Coordinate handling. Everything the rules engine touches is in local metres.

Sites are stored in Atlas as GeoJSON (WGS84) so geospatial queries work, and as
local-metre polygons (origin = parcel centroid, UTM 18N) so the designer and the
rules engine can reason in small, honest numbers.
"""
from __future__ import annotations

from typing import Iterable

from pyproj import Transformer
from shapely.geometry import Polygon, MultiPolygon, shape, mapping
from shapely.ops import transform, unary_union

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
_TO_WGS = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)


def geojson_to_utm(geom_geojson: dict) -> Polygon | MultiPolygon:
    g = shape(geom_geojson)
    return transform(_TO_UTM.transform, g)


def utm_to_geojson(g) -> dict:
    return mapping(transform(_TO_WGS.transform, g))


def to_local(g, origin_xy: tuple[float, float]):
    ox, oy = origin_xy
    return transform(lambda x, y, z=None: (x - ox, y - oy), g)


def from_local(g, origin_xy: tuple[float, float]):
    ox, oy = origin_xy
    return transform(lambda x, y, z=None: (x + ox, y + oy), g)


def local_to_geojson(g, origin_xy: tuple[float, float]) -> dict:
    return utm_to_geojson(from_local(g, origin_xy))


def largest_polygon(g) -> Polygon:
    if isinstance(g, Polygon):
        return g
    if isinstance(g, MultiPolygon):
        return max(g.geoms, key=lambda p: p.area)
    u = unary_union(g)
    return largest_polygon(u)


def rect(x: float, y: float, w: float, l: float, rotation_deg: float = 0.0) -> Polygon:
    """Axis-aligned rectangle centred at (x, y), width along x, length along y, then rotated."""
    from shapely.affinity import rotate

    p = Polygon([(x - w / 2, y - l / 2), (x + w / 2, y - l / 2), (x + w / 2, y + l / 2), (x - w / 2, y + l / 2)])
    return rotate(p, rotation_deg, origin=(x, y)) if rotation_deg else p


def bounds_summary(polys: Iterable) -> dict:
    u = unary_union(list(polys))
    minx, miny, maxx, maxy = u.bounds
    return {"minx": round(minx, 1), "miny": round(miny, 1), "maxx": round(maxx, 1), "maxy": round(maxy, 1)}
