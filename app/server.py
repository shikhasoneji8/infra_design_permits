"""Permit Harness web app: add a real NJ parcel, run the harness, watch rounds land in Atlas,
kill it, resume it. Same engine as scripts/run.py; this is the control panel.

  uv run uvicorn app.server:app --port 8000 --reload        # Atlas
  PH_OFFLINE=1 uv run uvicorn app.server:app --port 8000    # in-memory store + synthetic sites (no network)
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from permit_harness import config as C
from permit_harness.loop import run as run_loop
from permit_harness.plan import Plan
from permit_harness.site import site_doc_from_geojson_bundle, site_from_doc
from permit_harness.store import MongoStore, MemoryStore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from report import site_layers, _poly_points  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
DATA = ROOT / "data" / "sites"
OFFLINE = os.getenv("PH_OFFLINE", "") not in {"", "0", "false"}

app = FastAPI(title="Permit Harness")
_store = None
_jobs: dict[str, dict] = {}  # run_id -> {status, error, started}
_lock = threading.Lock()


def store():
    global _store
    if _store is None:
        if OFFLINE:
            from permit_harness.synthetic import bundle
            _store = MemoryStore()
            _store.upsert_site(site_doc_from_geojson_bundle(bundle()))
            _store.upsert_site(site_doc_from_geojson_bundle(bundle(site_id="syn2", homes_side="W", wetland_side="S")))
        else:
            _store = MongoStore()
            _store.ensure()
    return _store


def _jsonable(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    return o


# ------------------------------------------------------------------ pages ----
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def api_config():
    return {"offline": OFFLINE, "db": C.MONGODB_DB, "designer_model": C.DESIGNER_MODEL, "reviewer_model": C.REVIEWER_MODEL,
            "atlas_auto_embed": C.ATLAS_AUTO_EMBED, "have_llm": bool(C.OPENROUTER_API_KEY), "target_mw": C.TARGET_IT_MW,
            "fast_designer_model": C.FAST_DESIGNER_MODEL}


# ------------------------------------------------------------------ sites ----
@app.get("/api/sites")
def api_sites():
    s = store()
    if OFFLINE:
        docs = list(s.sites.values())
    else:
        from permit_harness import db
        docs = list(db.db().sites.find({}, {"neighbors": 0, "wetlands": 0}))
    out = []
    for d in docs:
        full = s.get_site(d["_id"])
        site = site_from_doc(full)
        out.append({"site_id": d["_id"], "name": d.get("name"), "pin": d.get("pin"), "municipality": d.get("municipality"),
                    "acres": round(site.acres, 1), "homes": len(site.homes), "wetlands": len(site.wetlands),
                    "buildable_acres": site.summary_for_llm()["buildable_area_acres"]})
    return out


@app.get("/api/sites/{site_id}/layers")
def api_site_layers(site_id: str):
    doc = store().get_site(site_id)
    if not doc:
        raise HTTPException(404, "site not found")
    return site_layers(site_from_doc(doc))


class NewSite(BaseModel):
    pin: str
    site_id: str
    name: Optional[str] = None


@app.post("/api/sites")
def api_add_site(body: NewSite):
    """Fetch a real parcel from NJ GIS (scripts/fetch_site.py), then load it into Atlas."""
    if OFFLINE:
        raise HTTPException(400, "offline mode: cannot fetch from NJ GIS")
    site_id = body.site_id.strip().replace(" ", "_").lower()
    name = body.name or f"{body.pin} ({site_id})"
    cmd = [sys.executable, str(ROOT / "scripts" / "fetch_site.py"), "--pin", body.pin.strip(), "--site-id", site_id, "--name", name]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise HTTPException(500, f"fetch failed: {(r.stderr or r.stdout)[-600:]}")
    from permit_harness import db
    bundle = json.loads((DATA / f"{site_id}.json").read_text())
    doc = site_doc_from_geojson_bundle(bundle)
    db.upsert_site(doc)
    site = site_from_doc(doc)
    return {"site_id": site_id, "name": name, "acres": round(site.acres, 1), "homes": len(site.homes),
            "wetlands": len(site.wetlands), "log": r.stdout[-800:]}


# ------------------------------------------------------------------- runs ----
class NewRun(BaseModel):
    site_id: str
    run_id: str
    max_rounds: int = 12
    memory: bool = True
    crash_after: Optional[int] = None
    use_llm: bool = True
    resume: bool = False
    speed: str = "quality"  # quality | fast


def _worker(body: NewRun):
    rid = body.run_id
    try:
        speed = body.speed
        run_loop(body.site_id, rid, store=store(), max_rounds=body.max_rounds, resume=body.resume,
                 use_llm=body.use_llm and speed != "instant", use_llm_reviewer=body.use_llm and speed == "quality",
                 crash_after_round=body.crash_after, use_memory=body.memory,
                 designer_model=C.FAST_DESIGNER_MODEL if speed == "fast" else C.DESIGNER_MODEL,
                 reviewer_model=C.REVIEWER_MODEL)
        with _lock:
            _jobs[rid]["status"] = "finished"
    except SystemExit as e:
        with _lock:
            _jobs[rid]["status"] = "crashed" if e.code == 3 else "finished"
    except Exception as e:  # noqa: BLE001
        with _lock:
            _jobs[rid]["status"] = "error"
            _jobs[rid]["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}"


@app.post("/api/runs")
def api_start_run(body: NewRun):
    if not store().get_site(body.site_id):
        raise HTTPException(404, "site not found")
    with _lock:
        j = _jobs.get(body.run_id)
        if j and j["status"] == "running":
            raise HTTPException(409, "run already in progress")
        _jobs[body.run_id] = {"status": "running", "error": None, "started": time.time(), "site_id": body.site_id,
                              "memory": body.memory, "crash_after": body.crash_after, "speed": body.speed}
    threading.Thread(target=_worker, args=(body,), daemon=True).start()
    return {"run_id": body.run_id, "status": "running"}


@app.post("/api/runs/{run_id}/resume")
def api_resume(run_id: str):
    r = store().get_run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    body = NewRun(site_id=r["site_id"], run_id=run_id, max_rounds=r.get("max_rounds", 12), resume=True,
                  memory=_jobs.get(run_id, {}).get("memory", True), speed=_jobs.get(run_id, {}).get("speed", "quality"))
    return api_start_run(body)


@app.get("/api/runs")
def api_runs():
    s = store()
    if OFFLINE:
        docs = list(s.runs.values())
    else:
        from permit_harness import db
        docs = list(db.db().runs.find().sort("created_at", 1))
    out = []
    for d in docs:
        j = _jobs.get(d["_id"], {})
        out.append({"run_id": d["_id"], "site_id": d["site_id"], "status": d["status"], "round": d.get("round", 0),
                    "max_rounds": d.get("max_rounds"), "last_penalty": d.get("last_penalty"),
                    "penalties": [h["penalty"] for h in d.get("history", [])],
                    "job": j.get("status"), "error": j.get("error"), "memory": j.get("memory", True),
                    "created_at": _jsonable(d.get("created_at"))})
    return out


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    s = store()
    r = s.get_run(run_id)
    if not r:
        j = _jobs.get(run_id)
        if j:  # started seconds ago; the run document is not in Atlas yet
            return {"run_id": run_id, "site_id": j["site_id"], "status": "starting", "job": j["status"], "error": j.get("error"),
                    "memory": j.get("memory", True), "rounds": []}
        raise HTTPException(404, "run not found")
    rounds = []
    for h in r.get("history", []):
        n = h["round"]
        d = s.get_design(f"{run_id}:r{n}")
        rv = s.get_review(run_id, n)
        if not d or not rv:
            continue
        plan = Plan.model_validate(d["plan"])
        rounds.append({
            "round": n, "penalty": rv["penalty"], "passed": rv["passed"],
            "objects": [{"id": o.id, "kind": o.kind, "pts": _poly_points(o.footprint())[0], "x": o.x, "y": o.y, "w": o.w,
                         "l": o.l, "h": o.h, "rot": o.rotation_deg} for o in plan.objects],
            "violations": [{"rule": v["rule"], "title": v["title"], "measured": v["measured"], "penalty": v["penalty"],
                            "citation": v.get("citation", "")} for v in rv["violations"]],
            "rejection": (rv.get("rejection_text") or "").replace("**", ""),
            "lessons_used": d.get("lessons_used", []),
            "rationale": plan.rationale,
            "knobs": {k: getattr(plan, k) for k in ["it_mw", "cooling_type", "cooling_noise", "cooling_barrier", "generator_tier",
                                                   "generator_enclosure", "generator_screen", "bess_mw", "water_source"]},
            "measured": {k: v for k, v in rv["measured"].items() if k in
                         ["night_dba_worst_home", "day_dba_worst_home", "nox_pte_tpy", "water_gpd", "new_impervious_acres", "capacity_fraction"]},
        })
    j = _jobs.get(run_id, {})
    return {"run_id": run_id, "site_id": r["site_id"], "status": r["status"], "job": j.get("status"), "error": j.get("error"),
            "memory": j.get("memory", True), "speed": j.get("speed", "quality"), "rounds": rounds}


# ------------------------------------------------------------------ atlas ----
@app.get("/api/atlas")
def api_atlas():
    if OFFLINE:
        s = store()
        return {"offline": True, "collections": {"sites": len(s.sites), "designs": len(s.designs), "reviews": len(s.reviews),
                                                 "lessons": len(s.lessons), "runs": len(s.runs)}, "indexes": []}
    from permit_harness import db
    d = db.db()
    cols = {n: d[n].estimated_document_count() for n in ["sites", "site_features", "designs", "reviews", "lessons", "runs", "checkpoints"]}
    idx = []
    for coll in ["lessons", "reviews"]:
        try:
            for ix in d[coll].list_search_indexes():
                idx.append({"collection": coll, "name": ix["name"], "type": ix.get("type", "search"), "queryable": ix.get("queryable")})
        except Exception:  # noqa: BLE001
            pass
    return {"offline": False, "db": C.MONGODB_DB, "collections": cols, "indexes": idx, "auto_embed": C.ATLAS_AUTO_EMBED}


class Ask(BaseModel):
    q: str


@app.post("/api/atlas/lessons")
def api_ask_lessons(body: Ask):
    """Live $vectorSearch over lessons with a plain-text question."""
    s = store()
    if OFFLINE:
        from permit_harness.llm import embed
        res = s.similar_lessons(embed([body.q])[0], k=6)
    else:
        res = s.similar_lessons_text(body.q, k=6)
    return [{"rule": r.get("rule"), "text": r.get("text"), "site_id": r.get("site_id"), "score": r.get("score")} for r in res]


@app.post("/api/atlas/letters")
def api_search_letters(body: Ask):
    """Live Atlas Search over rejection letters."""
    return store().search_rejections(body.q, k=5)
