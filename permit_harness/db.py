"""MongoDB Atlas layer. Six collections, five Atlas features that matter on stage.

  sites / site_features   parcel, homes, wetlands as GeoJSON (2dsphere)  -> $geoNear, $geoIntersects
  designs                 one site plan per round, parent_design_id      -> version history
  reviews                 violations, measured values, penalty, letter   -> Atlas Search (full text) over letters
  lessons                 short rule learned                             -> Vector Search with Atlas AUTOMATED EMBEDDINGS (Voyage AI)
  runs                    status, round, penalty history                 -> the hard metric over time
  checkpoints*            LangGraph MongoDBSaver                          -> kill it, restart it, it continues
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import lru_cache

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.operations import SearchIndexModel

from . import config as C

LESSONS_VECTOR_INDEX = "lessons_vector"          # client-side embeddings (fallback)
LESSONS_AUTO_INDEX = "lessons_auto"              # Atlas automated embeddings (Voyage AI), M10+
REVIEWS_SEARCH_INDEX = "reviews_text"            # Atlas Search over rejection letters
AUTO_EMBED_MODEL = C.ATLAS_EMBED_MODEL


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
    d.site_features.create_index([("geometry", "2dsphere")])
    d.site_features.create_index([("site_id", ASCENDING), ("kind", ASCENDING)])
    ensure_search_indexes(verbose)


def _existing(coll) -> dict[str, dict]:
    try:
        return {ix["name"]: ix for ix in coll.list_search_indexes()}
    except Exception:  # noqa: BLE001  (M0 without search support etc.)
        return {}


def ensure_search_indexes(verbose: bool = True) -> None:
    d = db()
    have = _existing(d.lessons)
    if C.ATLAS_AUTO_EMBED and LESSONS_AUTO_INDEX not in have:
        try:
            d.lessons.create_search_index(model=SearchIndexModel(
                definition={"fields": [
                    {"type": "text", "path": "text", "model": AUTO_EMBED_MODEL},
                    {"type": "filter", "path": "rule"},
                    {"type": "filter", "path": "site_id"},
                ]},
                name=LESSONS_AUTO_INDEX, type="vectorSearch"))
            if verbose:
                print(f"created Atlas Vector Search index '{LESSONS_AUTO_INDEX}' with AUTOMATED EMBEDDINGS ({AUTO_EMBED_MODEL}); "
                      "Atlas embeds every lesson itself, no embedding code in this repo")
        except Exception as e:  # noqa: BLE001
            print(f"automated-embedding index not available ({str(e)[:120]}); falling back to client-side embeddings")
            C.ATLAS_AUTO_EMBED = False
    if not C.ATLAS_AUTO_EMBED and LESSONS_VECTOR_INDEX not in have:
        d.lessons.create_search_index(model=SearchIndexModel(
            definition={"fields": [
                {"type": "vector", "path": "embedding", "numDimensions": C.EMBEDDING_DIMS, "similarity": "cosine"},
                {"type": "filter", "path": "rule"},
                {"type": "filter", "path": "site_id"},
            ]},
            name=LESSONS_VECTOR_INDEX, type="vectorSearch"))
        if verbose:
            print(f"created Atlas Vector Search index '{LESSONS_VECTOR_INDEX}' (client-side embeddings)")
    if REVIEWS_SEARCH_INDEX not in _existing(d.reviews):
        try:
            d.reviews.create_search_index(model=SearchIndexModel(
                definition={"mappings": {"dynamic": False, "fields": {
                    "rejection_text": {"type": "string"},
                    "site_id": {"type": "token"},
                    "violations": {"type": "document", "fields": {"rule": {"type": "token"}, "title": {"type": "string"}}},
                }}},
                name=REVIEWS_SEARCH_INDEX, type="search"))
            if verbose:
                print(f"created Atlas Search index '{REVIEWS_SEARCH_INDEX}' over rejection letters")
        except Exception as e:  # noqa: BLE001
            print(f"Atlas Search index not created ({str(e)[:120]})")


def index_ready(coll, name: str) -> bool:
    return bool(_existing(coll).get(name, {}).get("queryable", False))


def vector_index_ready() -> bool:
    d = db()
    return index_ready(d.lessons, LESSONS_AUTO_INDEX if C.ATLAS_AUTO_EMBED else LESSONS_VECTOR_INDEX)


def wait_for_indexes(timeout_s: int = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if vector_index_ready():
            return True
        time.sleep(5)
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
    """$geoNear: the closest residential lot to a point."""
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
        "$pull": {"history": {"round": round_no}},
    })
    db().runs.update_one({"_id": run_id}, {
        "$push": {"history": {"round": round_no, "penalty": penalty, "capacity": capacity_fraction, "at": now()}}})


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
    """lessons: [{rule, text, context, embedding?}]. With automated embeddings there is no
    `embedding` field: Atlas computes it from `text` when the document lands."""
    if not lessons:
        return
    d = db()
    d.lessons.delete_many({"run_id": run_id, "round": round_no})  # idempotent on resume
    d.lessons.insert_many([{**l, "run_id": run_id, "site_id": site_id, "round": round_no, "created_at": now()} for l in lessons])


_LESSON_FIELDS = {"text": 1, "rule": 1, "site_id": 1, "run_id": 1, "round": 1, "created_at": 1, "context": 1}


def diversify(res: list[dict], k: int, per_rule: int = 2, exclude_site: str | None = None) -> list[dict]:
    """Instant-mode runs write templated lessons, so raw nearest-neighbours are near-duplicates.
    Keep the best-scoring copy of each distinct sentence, at most `per_rule` per rule."""
    seen_text, per_rule_n, out = set(), {}, []
    for r in res:
        if exclude_site and r.get("site_id") == exclude_site:
            continue
        key = " ".join((r.get("text") or "").lower().split())[:120]
        rule = r.get("rule") or "?"
        if not key or key in seen_text or per_rule_n.get(rule, 0) >= per_rule:
            continue
        seen_text.add(key)
        per_rule_n[rule] = per_rule_n.get(rule, 0) + 1
        out.append(r)
        if len(out) >= k:
            break
    return out


def similar_lessons_text(query_text: str, k: int = 6, exclude_site: str | None = None) -> list[dict]:
    """$vectorSearch with a plain-text query; Atlas embeds the query with the same Voyage model."""
    d = db()
    if C.ATLAS_AUTO_EMBED and index_ready(d.lessons, LESSONS_AUTO_INDEX):
        pipe = [{"$vectorSearch": {"index": LESSONS_AUTO_INDEX, "path": "text", "query": query_text,
                                   "numCandidates": max(200, k * 40), "limit": k * 8}},
                {"$project": {**_LESSON_FIELDS, "score": {"$meta": "vectorSearchScore"}}}]
        return diversify(list(d.lessons.aggregate(pipe)), k, exclude_site=exclude_site)
    from .llm import embed
    return similar_lessons(embed([query_text])[0], k, exclude_site)


def similar_lessons(query_embedding: list[float], k: int = 6, exclude_site: str | None = None) -> list[dict]:
    """$vectorSearch over client-side embeddings. Falls back to recency if no index is queryable yet."""
    d = db()
    if index_ready(d.lessons, LESSONS_VECTOR_INDEX):
        pipe = [{"$vectorSearch": {"index": LESSONS_VECTOR_INDEX, "path": "embedding", "queryVector": query_embedding,
                                   "numCandidates": max(200, k * 40), "limit": k * 8}},
                {"$project": {**_LESSON_FIELDS, "score": {"$meta": "vectorSearchScore"}}}]
        return diversify(list(d.lessons.aggregate(pipe)), k, exclude_site=exclude_site)
    q = {"site_id": {"$ne": exclude_site}} if exclude_site else {}
    return diversify(list(d.lessons.find(q, _LESSON_FIELDS).sort("created_at", DESCENDING).limit(k * 8)), k)


# -------------------------------------------------------- Atlas Search ----
def search_rejections(query_text: str, k: int = 3) -> list[str]:
    """Full-text search over earlier rejection letters (precedent for the reviewer)."""
    d = db()
    if not index_ready(d.reviews, REVIEWS_SEARCH_INDEX):
        return []
    pipe = [{"$search": {"index": REVIEWS_SEARCH_INDEX,
                         "text": {"query": query_text, "path": ["rejection_text", "violations.title"]}}},
            {"$match": {"passed": False}},
            {"$limit": k},
            {"$project": {"rejection_text": 1, "run_id": 1, "round": 1, "score": {"$meta": "searchScore"}}}]
    return [f"[{r['run_id']} r{r['round']}] {r['rejection_text']}" for r in d.reviews.aggregate(pipe) if r.get("rejection_text")]


def penalty_curve(run_id: str) -> list[dict]:
    r = get_run(run_id)
    return r.get("history", []) if r else []
