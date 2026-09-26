"""Storage interface for the graph. `MongoStore` is the real thing (Atlas + LangGraph
MongoDBSaver). `MemoryStore` is a dict-backed twin (+ InMemorySaver) used by the unit tests
and by `--offline` runs, so the loop logic can be exercised anywhere."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import config as C
from . import db as D


class MongoStore:
    name = "atlas"

    def __init__(self):
        self._saver = None

    @property
    def atlas_auto_embed(self) -> bool:
        return C.ATLAS_AUTO_EMBED

    def checkpointer(self):
        """LangGraph's MongoDB checkpointer: graph state lands in `checkpoints` / `checkpoint_writes`."""
        if self._saver is None:
            from langgraph.checkpoint.mongodb import MongoDBSaver
            self._saver = MongoDBSaver(D.client(), db_name=C.MONGODB_DB)
        return self._saver

    def ensure(self):
        D.ensure_indexes()

    def get_site(self, site_id):
        return D.get_site(site_id)

    def create_run(self, run_id, site_id, max_rounds):
        return D.create_run(run_id, site_id, max_rounds)

    def get_run(self, run_id):
        return D.get_run(run_id)

    def checkpoint(self, *a, **k):
        return D.checkpoint(*a, **k)

    def save_design(self, *a, **k):
        return D.save_design(*a, **k)

    def get_design(self, design_id):
        return D.get_design(design_id)

    def save_review(self, *a, **k):
        return D.save_review(*a, **k)

    def get_review(self, run_id, round_no):
        return D.get_review(run_id, round_no)

    def save_lessons(self, *a, **k):
        return D.save_lessons(*a, **k)

    def similar_lessons(self, emb, k=6, exclude_site=None):
        return D.similar_lessons(emb, k, exclude_site)

    def similar_lessons_text(self, text, k=6, exclude_site=None):
        return D.similar_lessons_text(text, k, exclude_site)

    def search_rejections(self, text, k=3):
        try:
            return D.search_rejections(text, k)
        except Exception as e:  # noqa: BLE001
            print(f"[atlas search] {e}")
            return []

    def nearest_home(self, site_id, lon, lat):
        return D.nearest_home(site_id, lon, lat)

    def lesson_count(self):
        return D.db().lessons.count_documents({})


class MemoryStore:
    name = "memory"
    atlas_auto_embed = False

    def __init__(self):
        self.sites, self.runs, self.designs, self.reviews, self.lessons = {}, {}, {}, {}, []
        self._saver = None

    def checkpointer(self):
        if self._saver is None:
            from langgraph.checkpoint.memory import InMemorySaver
            self._saver = InMemorySaver()
        return self._saver

    def ensure(self):
        pass

    def upsert_site(self, doc):
        self.sites[doc["_id"]] = doc

    def get_site(self, site_id):
        return self.sites.get(site_id)

    def create_run(self, run_id, site_id, max_rounds):
        self.runs[run_id] = {"_id": run_id, "site_id": site_id, "status": "running", "round": 0,
                             "max_rounds": max_rounds, "last_design_id": None, "last_penalty": None, "history": []}
        return self.runs[run_id]

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def checkpoint(self, run_id, round_no, design_id, penalty, capacity_fraction, status="running"):
        r = self.runs[run_id]
        r.update(round=round_no, last_design_id=design_id, last_penalty=penalty, status=status)
        r["history"] = [h for h in r["history"] if h["round"] != round_no]
        r["history"].append({"round": round_no, "penalty": penalty, "capacity": capacity_fraction,
                             "at": datetime.now(timezone.utc)})

    def save_design(self, run_id, site_id, round_no, plan, parent_design_id, lessons_used):
        did = f"{run_id}:r{round_no}"
        self.designs[did] = {"_id": did, "run_id": run_id, "site_id": site_id, "round": round_no,
                             "parent_design_id": parent_design_id, "plan": plan, "lessons_used": lessons_used}
        return did

    def get_design(self, design_id):
        return self.designs.get(design_id)

    def save_review(self, run_id, site_id, round_no, design_id, review, rejection_text):
        rid = f"{run_id}:r{round_no}"
        self.reviews[rid] = {"_id": rid, "run_id": run_id, "site_id": site_id, "round": round_no,
                             "design_id": design_id, "rejection_text": rejection_text, **review}
        return rid

    def get_review(self, run_id, round_no):
        return self.reviews.get(f"{run_id}:r{round_no}")

    def save_lessons(self, run_id, site_id, round_no, lessons):
        self.lessons = [l for l in self.lessons if not (l["run_id"] == run_id and l["round"] == round_no)]
        for l in lessons:
            self.lessons.append({**l, "run_id": run_id, "site_id": site_id, "round": round_no})

    def similar_lessons(self, emb, k=6, exclude_site=None):
        def cos(a, b):
            num = sum(x * y for x, y in zip(a, b))
            den = (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0
            return num / den
        cands = [l for l in self.lessons if l.get("site_id") != exclude_site] if exclude_site else list(self.lessons)
        cands.sort(key=lambda l: -cos(emb, l["embedding"]))
        return [{"text": l["text"], "rule": l["rule"], "site_id": l["site_id"], "context": l.get("context")}
                for l in cands[:k]]

    def search_rejections(self, text, k=3):
        return []

    def nearest_home(self, site_id, lon, lat):
        return None

    def lesson_count(self):
        return len(self.lessons)
