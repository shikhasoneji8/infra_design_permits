"""MongoDB Atlas layer. Five collections, three Atlas features that matter on stage.

  sites    parcel + neighbors + wetlands as GeoJSON (2dsphere)   -> $geoNear, $geoIntersects
  designs  one site plan per round, parent_design_id             -> version history
  reviews  violations, measured values, penalty                  -> the hard metric over time
  lessons  short rule learned + embedding                        -> $vectorSearch
  runs     status, current round, checkpoint                     -> kill it, restart it, it continues
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import lru_cache

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.operations import SearchIndexModel

from . import config as C

LESSONS_VECTOR_INDEX = "lessons_vector"


@lru_cache(maxsize=1)
def client() -> MongoClient:
    if not C.MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set (check .env)")
    return MongoClient(C.MONGODB_URI, serverSelectionTimeoutMS=15000)


def db():
    return client()[C.MONGODB_DB]


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- setup ----
def ensure_indexes(verbose: bool = True) -> None:
    d = db()
    d.sites.create_index([("parcel", "2dsphere")])
    d.sites.create_index([("centroid", "2dsphere")])
    d.designs.create_index([("run_id", ASCENDING), ("round", ASCENDING)], unique=True)
    d.reviews.create_index([("run_id", ASCENDING), ("round", ASCENDING)], unique=True)
    d.lessons.create_index([("site_id", ASCENDING), ("rule", ASCENDING)])
    d.runs.create_index([("site_id", ASCENDING), ("status", ASCENDING)])
    # Homes and wetlands live inside the site doc but also in their own geo collection
    # so $geoNear can answer "closest home to this generator" directly.
    d.site_features.create_index([("geometry", "2dsphere")])
    d.site_features.create_index([("site_id", ASCENDING), ("kind", ASCENDING)])
    ensure_vector_index(verbose)


def ensure_vector_index(verbose: bool = True) -> None:
    d = db()
    existing = {ix["name"] for ix in d.lessons.list_search_indexes()}
    if LESSONS_VECTOR_INDEX in existing:
        return
    model = SearchIndexModel(
        definition={
            "fields": [
                {"type": "vector", "path": "embedding", "numDimensions": C.EMBEDDING_DIMS, "similarity": "cosine"},
                {"type": "filter", "path": "rule"},
                {"type": "filter", "path": "site_id"},
            ]
        },
        name=LESSONS_VECTOR_INDEX,
        type="vectorSearch",
    )
    d.lessons.create_search_index(model=model)
    if verbose:
        print(f"created Atlas Vector Search index '{LESSONS_VECTOR_INDEX}' on lessons (takes ~1 min to become queryable)")


def vector_index_ready() -> bool:
    for ix in db().lessons.list_search_indexes():
        if ix["name"] == LESSONS_VECTOR_INDEX:
            return ix.get("queryable", False)
    return False


# ---------------------------------------------------------------- sites ----
def upsert_site(doc: dict) -> None:
    d = db()
    d.sites.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    d.site_features.delete_many({"site_id": doc["_id"]})
    feats = []
    for n in doc.get("neighbors", []):
        feats.append({"site_id": doc["_id"], "kind": "neighbor", "pin": n["pin"], "prop_class": n["prop_class"],
                      "residential": str(n["prop_class"]).startswith("2"), "address": n.get("address", ""),
                      "geometry": n["geometry"]})
    for w in doc.get("wetlands", []):
        feats.append({"site_id": doc["_id"], "kind": "wetland", "label": w["label"],
                      "resource_class": w["resource_class"], "geometry": w["geometry"]})
    if feats:
        d.site_features.insert_many(feats)


def get_site(site_id: str) -> dict | None:
    return db().sites.find_one({"_id": site_id})


def nearest_home(site_id: str, lon: float, lat: float) -> dict | None:
    """$geoNear: the closest residential lot to a point (used for the on-stage callout)."""
    pipe = [
        {"$geoNear": {"near": {"type": "Point", "coordinates": [lon, lat]}, "distanceField": "distance_m",
                      "query": {"site_id": site_id, "kind": "neighbor", "residential": True}, "spherical": True}},
        {"$limit": 1},
        {"$project": {"pin": 1, "address": 1, "distance_m": 1}},
    ]
    res = list(db().site_features.aggregate(pipe))
    return res[0] if res else None


def wetlands_intersecting(site_id: str, polygon_geojson: dict) -> list[dict]:
    """$geoIntersects: which wetland polygons does this footprint touch."""
    return list(db().site_features.find(
        {"site_id": site_id, "kind": "wetland", "geometry": {"$geoIntersects": {"$geometry": polygon_geojson}}},
        {"label": 1, "resource_class": 1}))


# ------------------------------------------------------------ runs/loop ----
def create_run(run_id: str, site_id: str, max_rounds: int) -> dict:
    doc = {"_id": run_id, "site_id": site_id, "status": "running", "round": 0, "max_rounds": max_rounds,
           "last_design_id": None, "last_penalty": None, "history": [], "created_at": now(), "updated_at": now()}
    db().runs.replace_one({"_id": run_id}, doc, upsert=True)
    return doc


def get_run(run_id: str) -> dict | None:
    return db().runs.find_one({"_id": run_id})


def checkpoint(run_id: str, round_no: int, design_id: str, penalty: int, capacity_fraction: float,
               status: str = "running") -> None:
    db().runs.update_one({"_id": run_id}, {
        "$set": {"round": round_no, "last_design_id": design_id, "last_penalty": penalty, "status": status,
                 "updated_at": now()},
        "$push": {"history": {"round": round_no, "penalty": penalty, "capacity": capacity_fraction, "at": now()}},
    })


def save_design(run_id: str, site_id: str, round_no: int, plan: dict, parent_design_id: str | None,
                lessons_used: list[str]) -> str:
    design_id = f"{run_id}:r{round_no}"
    db().designs.replace_one({"_id": design_id}, {
        "_id": design_id, "run_id": run_id, "site_id": site_id, "round": round_no,
        "parent_design_id": parent_design_id, "plan": plan, "lessons_used": lessons_used, "created_at": now(),
    }, upsert=True)
    return design_id


def get_design(design_id: str) -> dict | None:
    return db().designs.find_one({"_id": design_id})


def save_review(run_id: str, site_id: str, round_no: int, design_id: str, review: dict, rejection_text: str) -> str:
    review_id = f"{run_id}:r{round_no}"
    db().reviews.replace_one({"_id": review_id}, {
        "_id": review_id, "run_id": run_id, "site_id": site_id, "round": round_no, "design_id": design_id,
        "rejection_text": rejection_text, **review, "created_at": now(),
    }, upsert=True)
    return review_id


def get_review(run_id: str, round_no: int) -> dict | None:
    return db().reviews.find_one({"_id": f"{run_id}:r{round_no}"})


# -------------------------------------------------------------- lessons ----
def save_lessons(run_id: str, site_id: str, round_no: int, lessons: list[dict]) -> None:
    """lessons: [{rule, text, embedding, context}]"""
    if not lessons:
        return
    docs = [{**l, "run_id": run_id, "site_id": site_id, "round": round_no, "created_at": now()} for l in lessons]
    db().lessons.insert_many(docs)


def similar_lessons(query_embedding: list[float], k: int = 6, exclude_site: str | None = None) -> list[dict]:
    """$vectorSearch over lessons. Falls back to recency if the index is not queryable yet."""
    d = db()
    if vector_index_ready():
        pipe = [{"$vectorSearch": {"index": LESSONS_VECTOR_INDEX, "path": "embedding", "queryVector": query_embedding,
                                   "numCandidates": max(50, k * 10), "limit": k * 2}},
                {"$project": {"text": 1, "rule": 1, "site_id": 1, "context": 1, "score": {"$meta": "vectorSearchScore"}}}]
        res = list(d.lessons.aggregate(pipe))
        if exclude_site:
            res = [r for r in res if r.get("site_id") != exclude_site]
        return res[:k]
    q = {"site_id": {"$ne": exclude_site}} if exclude_site else {}
    return list(d.lessons.find(q, {"text": 1, "rule": 1, "site_id": 1, "context": 1}).sort("created_at", DESCENDING).limit(k))


def wait_for_vector_index(timeout_s: int = 120) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if vector_index_ready():
            return True
        time.sleep(5)
    return False


def penalty_curve(run_id: str) -> list[dict]:
    r = get_run(run_id)
    return r.get("history", []) if r else []
